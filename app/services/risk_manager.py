from typing import Tuple, Optional, Set, Dict
from datetime import date, datetime, timedelta
from collections import defaultdict
import logging
import pandas as pd

logger = logging.getLogger(__name__)

class RiskManager:
    """
    Production-grade risk management with dynamic stops and position tracking.
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
        self.blacklist: Set[str] = set()
        
        # Trade tracking
        self.daily_trades: Dict[date, int] = defaultdict(int)
        self.daily_pnl: Dict[date, float] = defaultdict(float)
        self.current_date = date.today()
        
        # Position tracking (in-memory cache)
        self.positions: Dict[str, Dict] = {}
        
        # Defense mechanisms
        self.position_entry_times: Dict[str, datetime] = {}  # In-memory cache
        self.symbol_cooldowns: Dict[str, datetime] = {}      # Redis-backed (future)
        self.min_hold_bars = 4                                # 60min (15m x 4 bars)
        self.min_profit_pct = 0.015                           # 1.5% (5x transaction cost)
        self.cooldown_bars = 4                                # 60min cooldown
        self.bars_per_cycle = 15                              # 15 minutes per bar

    def _reset_if_new_day(self):
        """Reset daily counters if new trading day."""
        today = date.today()
        if today != self.current_date:
            self.current_date = today
            self.daily_trades[today] = 0
            self.daily_pnl[today] = 0.0
            logger.info(f"New trading day: {today}")

    def apply_symbol_filters(self, symbol: str, price: float, volume: float) -> bool:
        """
        Pre-filter symbol before strategy execution.
        Returns True if symbol passes all filters.
        """
        # Blacklist
        if symbol in self.blacklist:
            logger.info(f"Filtered {symbol}: blacklisted")
            return False
        
        # Price range
        if price < self.min_price or price > self.max_price:
            logger.info(f"Filtered {symbol}: price ${price:.2f} out of range")
            return False
        
        # Volume
        if volume < self.min_volume:
            logger.info(f"Filtered {symbol}: volume {volume:.0f} too low")
            return False
        
        return True

    def can_trade_today(self) -> bool:
        """Check if we can place more trades today."""
        self._reset_if_new_day()
        today = self.current_date
        
        # Trade count limit
        if self.daily_trades[today] >= self.max_trades_per_day:
            logger.warning(f"Daily trade limit reached: {self.daily_trades[today]}")
            return False
        
        # Loss limit
        if self.daily_pnl[today] <= -self.daily_loss_limit:
            logger.warning(f"Daily loss limit reached: ${self.daily_pnl[today]:.2f}")
            return False
        
        return True

    def calculate_position_size(
        self, 
        symbol: str, 
        price: float, 
        buying_power: float,
        atr: Optional[float] = None,
        portfolio_value: Optional[float] = None
    ) -> Tuple[bool, int]:
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
            logger.warning(f"Insufficient funds for {symbol}: qty={quantity}")
            return False, 0
        
        logger.info(f"Position size for {symbol}: {quantity} shares @ ${price:.2f}")
        return True, quantity

    def calculate_exit_prices(
        self,
        entry_price: float,
        atr: Optional[float] = None
    ) -> Tuple[float, float, float]:
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
            f"Exit prices: SL=${stop_loss:.2f}, TP=${take_profit:.2f}, "
            f"Trail=${trailing_stop:.2f} (Entry=${entry_price:.2f})"
        )
        
        return stop_loss, take_profit, trailing_stop

    def update_trailing_stop(
        self,
        entry_price: float,
        current_price: float,
        current_trailing_stop: float,
        atr: Optional[float] = None
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
            logger.info(f"Trailing stop updated: ${current_trailing_stop:.2f} -> ${updated_stop:.2f}")
        
        return updated_stop

    def check_exit_conditions(
        self,
        entry_price: float,
        current_price: float,
        stop_loss: float,
        take_profit: float,
        trailing_stop: Optional[float] = None
    ) -> Tuple[bool, str]:
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
    ) -> Tuple[bool, float]:
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
            logger.info(f"Moving stop to breakeven: ${entry_price:.2f}")
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
            logger.info(f"Daily P&L: ${self.daily_pnl[today]:.2f} ({self.daily_trades[today]} trades)")

    def add_to_blacklist(self, symbol: str):
        """Add symbol to blacklist."""
        self.blacklist.add(symbol)
        logger.info(f"Added {symbol} to blacklist")

    def remove_from_blacklist(self, symbol: str):
        """Remove symbol from blacklist."""
        self.blacklist.discard(symbol)
        logger.info(f"Removed {symbol} from blacklist")
    
    def can_enter_position(self, symbol: str) -> Tuple[bool, str]:
        """
        Check if symbol is allowed to enter (cooldown period check).
        
        Defense Rule:
        - After exiting a position, enforce cooldown period (default 60 min)
        - Prevents rapid re-trading of same symbol (whipsaw protection)
        
        Args:
            symbol: Stock symbol
        
        Returns:
            (allowed, reason)
        """
        if symbol in self.symbol_cooldowns:
            cooldown_end = self.symbol_cooldowns[symbol]
            now = datetime.now()
            if now < cooldown_end:
                remaining_min = int((cooldown_end - now).total_seconds() / 60)
                return False, f"COOLDOWN: {remaining_min}min remaining (ends {cooldown_end.strftime('%H:%M')})"
            else:
                # Cooldown expired, remove from dict
                del self.symbol_cooldowns[symbol]
        
        return True, "OK"
    
    def can_exit_position(
        self, 
        symbol: str, 
        entry_price: float, 
        current_price: float,
        entry_time: datetime
    ) -> Tuple[bool, str]:
        """
        Check if position can be exited based on defense rules.
        
        Defense Rules:
        1. Minimum Holding Period: 60 minutes (4 bars)
        2. Minimum Profit Threshold: 1.5% (unless hold > 120 min)
        
        Exceptions:
        - Stop-loss signals should override these rules (handled by caller)
        
        Args:
            symbol: Stock symbol
            entry_price: Position entry price
            current_price: Current market price
            entry_time: Position entry timestamp
        
        Returns:
            (allowed, reason)
        """
        now = datetime.now()
        
        # Rule 1: Minimum holding period (60 minutes)
        hold_duration = now - entry_time
        min_hold_time = timedelta(minutes=self.min_hold_bars * self.bars_per_cycle)
        
        if hold_duration < min_hold_time:
            held_min = int(hold_duration.total_seconds() / 60)
            required_min = int(min_hold_time.total_seconds() / 60)
            return False, f"MIN_HOLD: {held_min}min < {required_min}min (entry: {entry_time.strftime('%H:%M')})"
        
        # Rule 2: Minimum profit threshold (1.5%)
        profit_pct = (current_price - entry_price) / entry_price
        
        if profit_pct < self.min_profit_pct:
            # Allow exit after 8 bars (120 minutes) even if unprofitable
            max_hold_time = timedelta(minutes=8 * self.bars_per_cycle)
            if hold_duration < max_hold_time:
                return False, f"MIN_PROFIT: {profit_pct:.2%} < 1.5% (hold {int(hold_duration.total_seconds()/60)}min)"
        
        return True, "OK"
    
    def record_position_entry(self, symbol: str, entry_time: datetime):
        """
        Record position entry for hold period tracking.
        
        Args:
            symbol: Stock symbol
            entry_time: Entry timestamp
        """
        self.position_entry_times[symbol] = entry_time
        logger.info(f"📍 Position entry recorded: {symbol} @ {entry_time.strftime('%H:%M:%S')}")
    
    def record_position_exit(self, symbol: str):
        """
        Record position exit and start cooldown period.
        
        Args:
            symbol: Stock symbol
        """
        # Remove from active positions
        if symbol in self.position_entry_times:
            del self.position_entry_times[symbol]
        
        # Start cooldown
        cooldown_duration = timedelta(minutes=self.cooldown_bars * self.bars_per_cycle)
        cooldown_end = datetime.now() + cooldown_duration
        self.symbol_cooldowns[symbol] = cooldown_end
        
        logger.info(f"🚫 {symbol} cooldown: {self.cooldown_bars * self.bars_per_cycle}min (until {cooldown_end.strftime('%H:%M')}")
    
    def get_position_entry_time(self, symbol: str) -> Optional[datetime]:
        """
        Get position entry time from memory cache.
        
        Note: This is in-memory only. For persistence, query DB.
        
        Args:
            symbol: Stock symbol
        
        Returns:
            Entry time or None if not found
        """
        return self.position_entry_times.get(symbol, None)
