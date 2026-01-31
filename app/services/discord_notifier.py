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
            logger.error(f"Discord notification failed: {e}")
            return False

    def send_trade_alert(
        self,
        action: str,
        symbol: str,
        qty: int | float,
        price: float,
        extra_info: dict[str, Any] | None = None
    ) -> bool:
        """
        거래 알림 전송 (DISCORD_TRADING_URL 사용)

        Args:
            action: 'BUY' 또는 'SELL'
            symbol: 종목 심볼
            qty: 수량
            price: 가격
            extra_info: 추가 정보 (regime, signal, kelly 등)
        """
        if not self.trading_url:
            logger.debug("Trading webhook not configured, skipping trade alert")
            return False

        is_buy = action.upper() == "BUY"
        color = 0x00FF00 if is_buy else 0xFF6B6B  # Green for BUY, Red for SELL
        emoji = "📈" if is_buy else "📉"

        embed = {
            "title": f"{emoji} {action.upper()} 주문 체결",
            "color": color,
            "fields": [
                {"name": "종목", "value": f"**{symbol}**", "inline": True},
                {"name": "수량", "value": f"{qty}주", "inline": True},
                {"name": "가격", "value": f"${price:,.2f}", "inline": True},
                {"name": "총액", "value": f"${qty * price:,.2f}", "inline": True},
            ],
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        if extra_info:
            for key, value in extra_info.items():
                if isinstance(value, float):
                    display_value = f"{value:.4f}" if abs(value) < 1 else f"{value:.2f}"
                else:
                    display_value = str(value)
                embed["fields"].append({
                    "name": key,
                    "value": display_value,
                    "inline": True
                })

        return self._send({"embeds": [embed]}, url=self.trading_url)

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
