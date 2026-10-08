import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from stock_trader.config import Settings


@dataclass(frozen=True)
class RiskState:
    capital: float
    equity: float
    day_start_equity: float
    peak_equity: float
    exposure: float
    cash: float
    position_count: int


def loss_reason(state: RiskState, settings: Settings) -> str | None:
    values = (state.capital, state.equity, state.day_start_equity, state.peak_equity)
    if not all(math.isfinite(v) and v > 0 for v in values):
        return "invalid_equity"
    if state.equity <= state.peak_equity * (1 - settings.max_drawdown):
        return "max_drawdown"
    if state.equity <= state.day_start_equity * (1 - settings.daily_loss_limit):
        return "daily_loss"
    return None


def cents(value: float, *, up=False) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_CEILING if up else ROUND_FLOOR))


def entry_plan(
    *,
    symbol: str,
    bid: float,
    ask: float,
    atr: float,
    state: RiskState,
    settings: Settings,
    expected_return: float,
) -> dict | None:
    values = (bid, ask, atr, expected_return, state.exposure, state.cash)
    if not all(math.isfinite(v) for v in values) or bid <= 1 or ask < bid or atr <= 0:
        return None
    if loss_reason(state, settings) or state.position_count >= settings.max_positions:
        return None
    spread = (ask - bid) / ((bid + ask) / 2)
    if spread > settings.max_spread:
        return None
    cost = settings.round_trip_cost_bps / 10_000
    if expected_return <= cost + spread:
        return None
    entry = float(cents(ask * 1.0005, up=True))
    distance = max(settings.stop_atr * atr, entry * 0.003)
    stop = float(cents(entry - distance))
    if stop <= 1:
        return None
    take = float(cents(entry + settings.reward_risk * distance, up=True))
    budget = min(state.capital, state.equity)
    max_notional = min(
        state.cash * 0.98,
        budget * settings.symbol_exposure_limit,
        max(0, budget * settings.gross_exposure_limit - state.exposure),
    )
    qty = math.floor(min(budget * settings.trade_risk / (entry - stop + entry * cost), max_notional / entry))
    if qty < 1:
        return None
    return {
        "symbol": symbol,
        "side": "buy",
        "type": "limit",
        "time_in_force": "day",
        "qty": str(qty),
        "limit_price": cents(entry),
        "order_class": "bracket",
        "take_profit": {"limit_price": cents(take)},
        "stop_loss": {"stop_price": cents(stop)},
        "extended_hours": False,
    }
