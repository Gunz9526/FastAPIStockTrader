"""
Discord Webhook Notification Service
Task 실패 및 중요 이벤트 알림
"""
import logging
import os
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from functools import wraps
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Discord Webhook을 통한 알림 서비스"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
        self.trading_url = os.getenv("DISCORD_TRADING_URL")  # 거래 전용
        if not self.webhook_url:
            logger.warning("DISCORD_WEBHOOK_URL not configured")
        if not self.trading_url:
            logger.debug("DISCORD_TRADING_URL not configured (trading alerts disabled)")

    def _send(self, payload: dict, url: str | None = None) -> bool:
        """Send message to Discord webhook"""
        target_url = url or self.webhook_url
        if not target_url:
            logger.debug("Discord webhook not configured, skipping notification")
            return False

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(target_url, json=payload)
                response.raise_for_status()
                return True
        except Exception as e:
            logger.error("Discord notification failed: %s", e)
            return False

    _CLASS_NAMES: dict[int, str] = {0: "DOWN ⬇️", 1: "NEUTRAL ➡️", 2: "UP ⬆️"}

    def send_trade_alert(
        self,
        action: str,
        symbol: str,
        qty: int | float,
        price: float,
        extra_info: dict[str, Any] | None = None,
        *,
        confidence: float | None = None,
        predicted_class: int | None = None,
        regime: str | None = None,
        kelly_fraction: float | None = None,
        pnl_amount: float | None = None,
        pnl_pct: float | None = None,
        hold_duration_hours: float | None = None,
        portfolio_value: float | None = None,
        position_count: str | None = None,
    ) -> bool:
        """거래 알림 전송 (DISCORD_TRADING_URL 사용).

        Structured embed with optional ML analysis, portfolio, and P&L sections.
        All keyword parameters are optional for backward compatibility.

        Args:
            action: 'BUY' 또는 'SELL'.
            symbol: 종목 심볼.
            qty: 수량.
            price: 가격.
            extra_info: 추가 정보 dict (기존 호환).
            confidence: ML 모델 신뢰도 (0-1).
            predicted_class: ML 예측 클래스 (0=DOWN, 1=NEUTRAL, 2=UP).
            regime: 현재 시장 레짐.
            kelly_fraction: Kelly 비율.
            pnl_amount: 실현 손익 금액 (SELL 시).
            pnl_pct: 실현 수익률 (SELL 시).
            hold_duration_hours: 보유 기간(시간) (SELL 시).
            portfolio_value: 포트폴리오 가치.
            position_count: 포지션 현황 문자열.
        """
        if not self.trading_url:
            logger.debug("Trading webhook not configured, skipping trade alert")
            return False

        is_buy = action.upper() == "BUY"
        color = 0x00FF00 if is_buy else 0xFF6B6B  # Green for BUY, Red for SELL
        emoji = "📈" if is_buy else "📉"

        fields: list[dict[str, Any]] = [
            {"name": "종목", "value": f"**{symbol}**", "inline": True},
            {"name": "수량", "value": f"{qty}주", "inline": True},
            {"name": "가격", "value": f"${price:,.2f}", "inline": True},
            {"name": "총액", "value": f"${qty * price:,.2f}", "inline": True},
        ]

        # --- ML Analysis section ---
        has_ml = any(v is not None for v in (predicted_class, confidence, regime, kelly_fraction))
        if has_ml:
            fields.append({"name": "\u200b", "value": "**🤖 ML 분석**", "inline": False})
            if predicted_class is not None:
                label = self._CLASS_NAMES.get(predicted_class, f"UNKNOWN({predicted_class})")
                fields.append({"name": "예측", "value": label, "inline": True})
            if confidence is not None:
                fields.append({"name": "신뢰도", "value": f"{confidence:.1%}", "inline": True})
            if regime is not None:
                fields.append({"name": "레짐", "value": regime, "inline": True})
            if kelly_fraction is not None:
                fields.append({"name": "Kelly", "value": f"{kelly_fraction:.4f}", "inline": True})

        # --- Portfolio section ---
        has_portfolio = any(v is not None for v in (portfolio_value, position_count))
        if has_portfolio:
            fields.append({"name": "\u200b", "value": "**💼 포트폴리오**", "inline": False})
            if portfolio_value is not None:
                fields.append({"name": "포트폴리오 가치", "value": f"${portfolio_value:,.2f}", "inline": True})
            if position_count is not None:
                fields.append({"name": "포지션 현황", "value": position_count, "inline": True})

        # --- P&L section (SELL only) ---
        if not is_buy:
            has_pnl = any(v is not None for v in (pnl_amount, pnl_pct, hold_duration_hours))
            if has_pnl:
                fields.append({"name": "\u200b", "value": "**💰 실현 손익**", "inline": False})
                if pnl_amount is not None:
                    pnl_emoji = "📈" if pnl_amount >= 0 else "📉"
                    fields.append({"name": "실현 P&L", "value": f"{pnl_emoji} ${pnl_amount:+,.2f}", "inline": True})
                if pnl_pct is not None:
                    fields.append({"name": "수익률", "value": f"{pnl_pct:+.2%}", "inline": True})
                if hold_duration_hours is not None:
                    fields.append({"name": "보유기간", "value": f"{hold_duration_hours:.1f}시간", "inline": True})

        # --- Extra info (existing behavior, shown last) ---
        if extra_info:
            for key, value in extra_info.items():
                if isinstance(value, float):
                    display_value = f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"
                else:
                    display_value = str(value)
                fields.append({
                    "name": key,
                    "value": display_value,
                    "inline": True,
                })

        embed: dict[str, Any] = {
            "title": f"{emoji} {action.upper()} 주문 체결",
            "color": color,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Compact footer summary
        footer_parts: list[str] = []
        if regime is not None:
            footer_parts.append(f"Regime: {regime}")
        if confidence is not None:
            footer_parts.append(f"Confidence: {confidence:.1%}")
        if footer_parts:
            embed["footer"] = {"text": " | ".join(footer_parts)}

        return self._send({"embeds": [embed]}, url=self.trading_url)

    def send_daily_summary(
        self,
        portfolio_value: float,
        daily_pnl: float,
        daily_pnl_pct: float,
        total_positions: int,
        trades_today: int,
        regime: str | None = None,
        top_performer: str | None = None,
        worst_performer: str | None = None,
    ) -> bool:
        """일일 거래 요약 전송 (DISCORD_WEBHOOK_URL 사용).

        Args:
            portfolio_value: 포트폴리오 총 가치.
            daily_pnl: 일일 실현 손익.
            daily_pnl_pct: 일일 수익률 (0-1 비율).
            total_positions: 보유 포지션 수.
            trades_today: 오늘 거래 수.
            regime: 현재 시장 레짐.
            top_performer: 최고 수익 종목.
            worst_performer: 최저 수익 종목.
        """
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        fields: list[dict[str, Any]] = [
            {"name": "포트폴리오 가치", "value": f"${portfolio_value:,.2f}", "inline": True},
            {"name": "일일 P&L", "value": f"{pnl_emoji} ${daily_pnl:+,.2f}", "inline": True},
            {"name": "수익률", "value": f"{daily_pnl_pct:+.2%}", "inline": True},
            {"name": "보유 포지션 수", "value": str(total_positions), "inline": True},
            {"name": "오늘 거래 수", "value": str(trades_today), "inline": True},
        ]
        if regime is not None:
            fields.append({"name": "시장 레짐", "value": regime, "inline": True})
        if top_performer is not None:
            fields.append({"name": "최고 종목", "value": top_performer, "inline": True})
        if worst_performer is not None:
            fields.append({"name": "최저 종목", "value": worst_performer, "inline": True})

        embed: dict[str, Any] = {
            "title": "📊 일일 거래 요약",
            "color": 0x9B59B6,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._send({"embeds": [embed]})

    _REGIME_EMOJI: dict[str, str] = {
        "bull_trending": "🐂",
        "bear_trending": "🐻",
        "sideways_volatile": "🌊",
        "sideways_calm": "😴",
    }

    def send_regime_change(
        self,
        old_regime: str,
        new_regime: str,
        extra_info: dict[str, Any] | None = None,
    ) -> bool:
        """시장 레짐 변경 알림 전송 (DISCORD_WEBHOOK_URL 사용).

        Args:
            old_regime: 이전 시장 레짐.
            new_regime: 새 시장 레짐.
            extra_info: 추가 정보 dict (선택).
        """
        old_emoji = self._REGIME_EMOJI.get(old_regime, "❓")
        new_emoji = self._REGIME_EMOJI.get(new_regime, "❓")

        fields: list[dict[str, Any]] = [
            {"name": "이전 레짐", "value": f"{old_emoji} {old_regime}", "inline": True},
            {"name": "새 레짐", "value": f"{new_emoji} {new_regime}", "inline": True},
            {"name": "변경 시간", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"), "inline": True},
        ]

        if extra_info:
            for key, value in extra_info.items():
                fields.append({
                    "name": key,
                    "value": str(value),
                    "inline": True,
                })

        embed: dict[str, Any] = {
            "title": "🔄 시장 레짐 변경",
            "color": 0xE67E22,
            "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        return self._send({"embeds": [embed]})

    def send_error(
        self,
        task_name: str,
        error: Exception,
        extra_info: dict[str, Any] | None = None
    ) -> bool:
        """
        Task 실패 알림 전송
        
        Args:
            task_name: 실패한 태스크 이름
            error: 발생한 예외
            extra_info: 추가 정보 (선택)
        """
        # Truncate traceback to avoid Discord message limit (2000 chars)
        tb = traceback.format_exc()
        if len(tb) > 1000:
            tb = tb[:500] + "\n...(truncated)...\n" + tb[-500:]
        
        embed = {
            "title": "🚨 Task 실패",
            "color": 0xFF0000,  # Red
            "fields": [
                {"name": "Task", "value": f"`{task_name}`", "inline": True},
                {"name": "시간", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
                {"name": "에러 타입", "value": f"`{type(error).__name__}`", "inline": True},
                {"name": "에러 메시지", "value": f"```{str(error)[:500]}```", "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        # Add traceback as separate field
        if tb:
            embed["fields"].append({
                "name": "Traceback",
                "value": f"```python\n{tb}```",
                "inline": False
            })
        
        # Add extra info if provided
        if extra_info:
            for key, value in extra_info.items():
                embed["fields"].append({
                    "name": key,
                    "value": f"`{value}`" if len(str(value)) < 100 else f"```{str(value)[:200]}```",
                    "inline": True
                })
        
        return self._send({"embeds": [embed]})
    
    def send_success(
        self,
        task_name: str,
        message: str,
        extra_info: dict[str, Any] | None = None
    ) -> bool:
        """
        Task 성공 알림 전송
        
        Args:
            task_name: 완료된 태스크 이름
            message: 성공 메시지
            extra_info: 추가 정보 (선택)
        """
        embed = {
            "title": "✅ Task 성공",
            "color": 0x00FF00,  # Green
            "fields": [
                {"name": "Task", "value": f"`{task_name}`", "inline": True},
                {"name": "시간", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
                {"name": "결과", "value": message[:1000], "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        if extra_info:
            for key, value in extra_info.items():
                embed["fields"].append({
                    "name": key,
                    "value": f"`{value}`" if len(str(value)) < 100 else str(value)[:200],
                    "inline": True
                })

        return self._send({"embeds": [embed]})

    def send_warning(
        self,
        task_name: str,
        message: str,
        extra_info: dict[str, Any] | None = None
    ) -> bool:
        """
        경고 알림 전송
        """
        embed = {
            "title": "⚠️ 경고",
            "color": 0xFFFF00,  # Yellow
            "fields": [
                {"name": "Task", "value": f"`{task_name}`", "inline": True},
                {"name": "시간", "value": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "inline": True},
                {"name": "내용", "value": message[:1000], "inline": False},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        if extra_info:
            for key, value in extra_info.items():
                embed["fields"].append({
                    "name": key,
                    "value": f"`{value}`",
                    "inline": True
                })

        return self._send({"embeds": [embed]})


# Singleton instance
discord_notifier = DiscordNotifier()


def notify_on_failure(task_name: str | None = None):
    """
    Decorator for Celery tasks to send Discord notification on failure.
    
    Usage:
        @celery_app.task(bind=True)
        @notify_on_failure("train_models")
        def train_models(self):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            name = task_name or func.__name__
            try:
                result = func(*args, **kwargs)
                return result
            except Exception as e:
                # Send Discord notification
                discord_notifier.send_error(
                    task_name=name,
                    error=e,
                    extra_info={
                        "args": str(args[1:])[:100] if args else "None",  # Skip 'self'
                        "kwargs": str(kwargs)[:100] if kwargs else "None"
                    }
                )
                # Re-raise the exception for Celery to handle
                raise
        return wrapper
    return decorator
