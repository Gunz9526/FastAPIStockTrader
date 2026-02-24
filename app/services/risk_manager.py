import logging
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta

from app.core.cache import cache

logger = logging.getLogger(__name__)

class RiskManager:
    """
    운영 수준의 리스크 관리: 동적 스탑과 포지션 추적을 제공합니다.
    """

    def __init__(
        self,
        max_position_size_pct: float = 0.10,
        stop_loss_atr_multiplier: float = 2.0,
        take_profit_atr_multiplier: float = 3.0,
        trailing_stop_atr_multiplier: float = 1.5,
        min_price: float = 5.0,
        max_price: float = 1000.0,
        min_volume: float = 100000,
        max_trades_per_day: int = 10,
        daily_loss_limit: float = 1000.0,
        max_portfolio_risk_pct: float = 0.02  # 2% max portfolio risk per trade
    ):
        # Position sizing
        self.max_position_size_pct = max_position_size_pct

        # Dynamic stops (ATR-based)
        self.stop_loss_atr_mult = stop_loss_atr_multiplier
        self.take_profit_atr_mult = take_profit_atr_multiplier
        self.trailing_stop_atr_mult = trailing_stop_atr_multiplier

        # Filters
        self.min_price = min_price
        self.max_price = max_price
        self.min_volume = min_volume

        # Daily limits
        self.max_trades_per_day = max_trades_per_day
        self.daily_loss_limit = daily_loss_limit
        self.max_portfolio_risk_pct = max_portfolio_risk_pct

        # Blacklist
        self.blacklist: set[str] = set()

        # Trade tracking
        self.daily_trades: dict[date, int] = defaultdict(int)
        self.daily_pnl: dict[date, float] = defaultdict(float)
        self.current_date = date.today()

        # Position tracking (in-memory cache)
        self.positions: dict[str, dict] = {}

        # Defense mechanisms
        self.position_entry_times: dict[str, datetime] = {}  # In-memory fallback
        self.symbol_cooldowns: dict[str, datetime] = {}      # In-memory fallback
        self.min_hold_bars = 2                                # 2 trading days (daily bars)
        self.min_profit_pct = 0.015                           # 1.5% (5x transaction cost)
        self.cooldown_bars = 1                                # 1 day cooldown
        self.bars_per_cycle = 1440                            # 1440 minutes per daily bar

        # Redis key prefixes & TTLs
        self._cooldown_ttl_seconds: int = self.cooldown_bars * self.bars_per_cycle * 60
        self._entry_time_ttl_seconds: int = 86400 * 7  # 7 days (daily trading)

    def _reset_if_new_day(self):
        """Reset daily counters if new trading day."""
        today = date.today()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades[today] = 0
            self.daily_pnl[today] = 0.0
            logger.info(f"새 거래일 시작: {today}")

    def apply_symbol_filters(self, symbol: str, price: float, volume: float) -> bool:
        """
        Pre-filter symbol before strategy execution.
        Returns True if symbol passes all filters.
        """
        # Blacklist
        if symbol in self.blacklist:
            logger.info(f"필터됨 {symbol}: 블랙리스트")
            return False

        # Price range
        if price < self.min_price or price > self.max_price:
            logger.info(f"필터됨 {symbol}: 가격 ${price:.2f} 범위 외")
            return False

        # Volume
        if volume < self.min_volume:
            logger.info(f"필터됨 {symbol}: 거래량 {volume:.0f} 부족")
            return False

        return True

    def can_trade_today(self) -> bool:
        """Check if we can place more trades today."""
        self._reset_if_new_day()
        today = self.current_date

        # Trade count limit
        if self.daily_trades[today] >= self.max_trades_per_day:
            logger.warning(f"일일 거래 한도 도달: {self.daily_trades[today]}")
            return False

        # Loss limit
        if self.daily_pnl[today] <= -self.daily_loss_limit:
            logger.warning(f"일일 손실 한도 도달: ${self.daily_pnl[today]:.2f}")
            return False

        return True

    def calculate_position_size(
        self,
        symbol: str,
        price: float,
        buying_power: float,
        atr: float | None = None,
        portfolio_value: float | None = None
    ) -> tuple[bool, int]:
        """
        Calculate optimal position size using multiple methods.
        
        Args:
            symbol: Stock symbol
            price: Current price
            buying_power: Available funds
            atr: Average True Range (for volatility-based sizing)
            portfolio_value: Total portfolio value (for risk-based sizing)
        
        Returns:
            (allowed, quantity)
        """
        if price <= 0:
            return False, 0

        # Method 1: Simple percentage of buying power
        max_cost_simple = buying_power * self.max_position_size_pct
        qty_simple = int(max_cost_simple / price)

        # Method 2: ATR-based position sizing (if available)
        if atr and atr > 0 and portfolio_value:
            # Risk per trade = 2% of portfolio
            risk_amount = portfolio_value * self.max_portfolio_risk_pct
            # Position size = Risk / (ATR × multiplier)
            risk_per_share = atr * self.stop_loss_atr_mult
            qty_atr = int(risk_amount / risk_per_share) if risk_per_share > 0 else 0

            # Use the more conservative
            quantity = min(qty_simple, qty_atr) if qty_atr > 0 else qty_simple
        else:
            quantity = qty_simple

        if quantity < 1:
            logger.warning(f"{symbol}: 자금 부족 — 수량={quantity}")
            return False, 0

        logger.info(f"{symbol} 포지션 사이즈: {quantity}주 @ ${price:.2f}")
        return True, quantity

    def calculate_exit_prices(
        self,
        entry_price: float,
        atr: float | None = None
    ) -> tuple[float, float, float]:
        """
        Calculate dynamic Stop Loss, Take Profit, and Trailing Stop.
        
        Args:
            entry_price: Entry price
            atr: Average True Range
        
        Returns:
            (stop_loss, take_profit, trailing_stop)
        """
        if atr and atr > 0:
            # ATR-based dynamic stops
            stop_loss = entry_price - (atr * self.stop_loss_atr_mult)
            take_profit = entry_price + (atr * self.take_profit_atr_mult)
            trailing_stop = entry_price - (atr * self.trailing_stop_atr_mult)
        else:
            # Fallback to fixed percentages
            stop_loss = entry_price * 0.98  # 2%
            take_profit = entry_price * 1.05  # 5%
            trailing_stop = entry_price * 0.985  # 1.5%

        logger.info(
            f"종료 가격: SL=${stop_loss:.2f}, TP=${take_profit:.2f}, "
            f"후행스탑=${trailing_stop:.2f} (진입=${entry_price:.2f})"
        )

        return stop_loss, take_profit, trailing_stop

    def update_trailing_stop(
        self,
        entry_price: float,
        current_price: float,
        current_trailing_stop: float,
        atr: float | None = None
    ) -> float:
        """
        Update trailing stop as price moves in our favor.
        
        Args:
            entry_price: Original entry price
            current_price: Current market price
            current_trailing_stop: Current trailing stop price
            atr: Average True Range
        
        Returns:
            New trailing stop price
        """
        if current_price <= entry_price:
            # No profit yet, don't update
            return current_trailing_stop

        if atr and atr > 0:
            # ATR-based trailing
            new_trailing = current_price - (atr * self.trailing_stop_atr_mult)
        else:
            # Fixed percentage trailing (1.5%)
            new_trailing = current_price  * 0.985

        # Trailing stop can only move up, never down
        updated_stop = max(current_trailing_stop, new_trailing)

        if updated_stop > current_trailing_stop:
            logger.info(f"후행스탑 업데이트: ${current_trailing_stop:.2f} -> ${updated_stop:.2f}")

        return updated_stop

    def check_exit_conditions(
        self,
        entry_price: float,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        trailing_stop: float | None = None
    ) -> tuple[bool, str]:
        """
        Check if any exit condition is met.
        
        Returns:
            (should_exit, reason)
        """
        # Stop loss hit
        if current_price <= stop_loss:
            return True, f"STOP_LOSS: ${current_price:.2f} <= ${stop_loss:.2f}"

        # Take profit hit
        if current_price >= take_profit:
            return True, f"TAKE_PROFIT: ${current_price:.2f} >= ${take_profit:.2f}"

        # Trailing stop hit
        if trailing_stop and current_price <= trailing_stop:
            return True, f"TRAILING_STOP: ${current_price:.2f} <= ${trailing_stop:.2f}"

        return False, ""

    def should_scale_out(
        self,
        entry_price: float,
        current_price: float,
        take_profit: float,
        partial_exit_threshold: float = 0.5
    ) -> tuple[bool, float]:
        """
        Determine if we should partially exit the position.
        
        Args:
            entry_price: Entry price
            current_price: Current price
            take_profit: Take profit target
            partial_exit_threshold: Percentage of TP to trigger partial exit (default 50%)
        
        Returns:
            (should_exit, exit_percentage)
        """
        profit_target = take_profit - entry_price
        current_profit = current_price - entry_price

        if current_profit <= 0:
            return False, 0.0

        profit_pct = current_profit / profit_target

        # Partial exit at 50% of target
        if profit_pct >= partial_exit_threshold and profit_pct < 1.0:
            return True, 0.5  # Exit 50% of position

        return False, 0.0

    def move_stop_to_breakeven(
        self,
        entry_price: float,
        current_price: float,
        take_profit: float,
        current_stop_loss: float,
        breakeven_threshold: float = 0.5
    ) -> float:
        """
        Move stop loss to break-even when profit target is partially reached.
        
        Returns:
            Updated stop loss price
        """
        profit_target = take_profit - entry_price
        current_profit = current_price - entry_price

        if current_profit <= 0:
            return current_stop_loss

        profit_pct = current_profit / profit_target

        # Move to breakeven at 50% of profit target
        if profit_pct >= breakeven_threshold:
            logger.info(f"스탑을 브레이크이븐으로 이동: ${entry_price:.2f}")
            return entry_price

        return current_stop_loss

    def record_trade(
        self,
        symbol: str,
        action: str,
        price: float,
        quantity: int,
        realized_pl: float = 0.0
    ):
        """Record a trade for daily tracking."""
        self._reset_if_new_day()
        today = self.current_date

        self.daily_trades[today] += 1

        if action == 'SELL' and realized_pl != 0:
            self.daily_pnl[today] += realized_pl
            logger.info(f"일일 손익: ${self.daily_pnl[today]:.2f} ({self.daily_trades[today]} 건)")

    def add_to_blacklist(self, symbol: str):
        """Add symbol to blacklist."""
        self.blacklist.add(symbol)
        logger.info(f"{symbol}을(를) 블랙리스트에 추가함")

    def remove_from_blacklist(self, symbol: str):
        """Remove symbol from blacklist."""
        self.blacklist.discard(symbol)
        logger.info(f"{symbol}을(를) 블랙리스트에서 제거함")

    def can_enter_position(self, symbol: str) -> tuple[bool, str]:
        """
        Check if symbol is allowed to enter (cooldown period check).

        Defense Rule:
        - After exiting a position, enforce cooldown period (default 60 min)
        - Prevents rapid re-trading of same symbol (whipsaw protection)
        - Cooldown state is persisted in Redis (survives restarts)

        Args:
            symbol: Stock symbol

        Returns:
            Tuple of (allowed, reason).
        """
        cooldown_end: datetime | None = None

        # 1) Try Redis first
        try:
            if cache.enabled:
                raw: str | None = cache.get(f"risk:cooldown:{symbol}")
                if raw is not None:
                    cooldown_end = datetime.fromisoformat(str(raw))
        except Exception as e:
            logger.warning(f"Redis read failed for cooldown:{symbol}: {e}")

        # 2) Fallback to in-memory dict
        if cooldown_end is None and symbol in self.symbol_cooldowns:
            cooldown_end = self.symbol_cooldowns[symbol]

        if cooldown_end is not None:
            now = datetime.now(UTC)
            if now < cooldown_end:
                remaining_min = int((cooldown_end - now).total_seconds() / 60)
                return False, f"COOLDOWN: {remaining_min}분 남음 (종료 {cooldown_end.strftime('%H:%M')})"
            else:
                # Cooldown expired — clean up both stores
                self.symbol_cooldowns.pop(symbol, None)
                try:
                    if cache.enabled:
                        cache.delete(f"risk:cooldown:{symbol}")
                except Exception as e:
                    logger.warning(f"Redis delete failed for cooldown:{symbol}: {e}")

        return True, "OK"

    def can_exit_position(
        self,
        symbol: str,
        entry_price: float,
        current_price: float,
        entry_time: datetime,
        hold_multiplier: float = 1.0
    ) -> tuple[bool, str]:
        """
        Check if position can be exited based on defense rules.
        
        Defense Rules:
        1. Minimum Holding Period: 2 trading days * hold_multiplier
        2. Minimum Profit Threshold: 1.5% (unless hold > 4 days)
        
        CRITICAL: 손절 시(-3% 이하)에는 방어 규칙 무시해야 함!
        이 함수는 일반적인 방어 규칙만 체크. 손절 로직은 caller에서 처리.
        
        Args:
            symbol: Stock symbol
            entry_price: Position entry price
            current_price: Current market price
            entry_time: Position entry timestamp
            hold_multiplier: Multiplier for minimum hold time (default 1.0)
        
        Returns:
            (allowed, reason)
        """
        now = datetime.now(UTC)  # timezone-aware

        # 손익 계산
        profit_pct = (current_price - entry_price) / entry_price

        # 손절 시나리오는 항상 허용해야 함 (caller에서 처리)
        # 여기서는 일반 규칙만 체크

        # Rule 1: Minimum holding period (60 minutes * hold_multiplier)
        hold_duration = now - entry_time
        min_hold_time = timedelta(minutes=self.min_hold_bars * self.bars_per_cycle * hold_multiplier)

        if hold_duration < min_hold_time:
            held_min = int(hold_duration.total_seconds() / 60)
            required_min = int(min_hold_time.total_seconds() / 60)
            # 손절 예외: -3% 이하면 즉시 허용
            if profit_pct <= -0.03:
                return True, f"STOP_LOSS_OVERRIDE: {profit_pct:.2%} (hold: {held_min}min)"
            return False, f"MIN_HOLD: {held_min}min < {required_min}min (entry: {entry_time.strftime('%H:%M')})"

        # Rule 2: Minimum profit threshold (1.5%)
        if profit_pct < self.min_profit_pct:
            # 손절 예외: -3% 이하면 즉시 허용
            if profit_pct <= -0.03:
                return True, f"STOP_LOSS: {profit_pct:.2%}"

            # Allow exit after 4 bars (4 trading days) even if unprofitable
            max_hold_time = timedelta(minutes=4 * self.bars_per_cycle)
            if hold_duration < max_hold_time:
                return False, f"MIN_PROFIT: {profit_pct:.2%} < 1.5% (hold {int(hold_duration.total_seconds()/60)}min)"

        return True, f"OK (profit: {profit_pct:.2%}, hold: {int(hold_duration.total_seconds()/60)}min)"

    def record_position_entry(self, symbol: str, entry_time: datetime) -> None:
        """
        Record position entry for hold period tracking.

        Persists entry time to Redis (TTL 24h) with in-memory fallback.

        Args:
            symbol: Stock symbol
            entry_time: Entry timestamp
        """
        # In-memory fallback always kept in sync
        self.position_entry_times[symbol] = entry_time

        # Persist to Redis
        try:
            if cache.enabled:
                cache.set(
                    f"risk:entry_time:{symbol}",
                    entry_time.isoformat(),
                    ttl_seconds=self._entry_time_ttl_seconds,
                )
        except Exception as e:
            logger.warning(f"Redis set failed for entry_time:{symbol}: {e}")

        logger.info(f"📍 Position entry recorded: {symbol} @ {entry_time.strftime('%H:%M:%S')}")

    def record_position_exit(self, symbol: str) -> None:
        """
        Record position exit and start cooldown period.

        Removes entry time and sets cooldown in both Redis and in-memory.

        Args:
            symbol: Stock symbol
        """
        # Remove entry time from both stores
        self.position_entry_times.pop(symbol, None)
        try:
            if cache.enabled:
                cache.delete(f"risk:entry_time:{symbol}")
        except Exception as e:
            logger.warning(f"Redis delete failed for entry_time:{symbol}: {e}")

        # Start cooldown
        cooldown_duration = timedelta(minutes=self.cooldown_bars * self.bars_per_cycle)
        cooldown_end = datetime.now(UTC) + cooldown_duration  # timezone-aware

        # In-memory fallback
        self.symbol_cooldowns[symbol] = cooldown_end

        # Persist to Redis with auto-expiry
        try:
            if cache.enabled:
                cache.set(
                    f"risk:cooldown:{symbol}",
                    cooldown_end.isoformat(),
                    ttl_seconds=self._cooldown_ttl_seconds,
                )
        except Exception as e:
            logger.warning(f"Redis set failed for cooldown:{symbol}: {e}")

        logger.info(f"🚫 {symbol} cooldown: {self.cooldown_bars * self.bars_per_cycle}min (until {cooldown_end.strftime('%H:%M')}")

    def get_position_entry_time(self, symbol: str) -> datetime | None:
        """
        Get position entry time from Redis (with in-memory fallback).

        Args:
            symbol: Stock symbol

        Returns:
            Entry time or None if not found.
        """
        # 1) Try Redis first
        try:
            if cache.enabled:
                raw: str | None = cache.get(f"risk:entry_time:{symbol}")
                if raw is not None:
                    entry_time = datetime.fromisoformat(str(raw))
                    # Sync back to in-memory for fast subsequent reads
                    self.position_entry_times[symbol] = entry_time
                    return entry_time
        except Exception as e:
            logger.warning(f"Redis read failed for entry_time:{symbol}: {e}")

        # 2) Fallback to in-memory dict
        return self.position_entry_times.get(symbol, None)
