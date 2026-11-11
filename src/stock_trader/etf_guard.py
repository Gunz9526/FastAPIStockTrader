"""ADR-0058: persistent additions guard; never a liquidation signal."""

import copy

from stock_trader.etf_ledger import number

CONTRACT = {"daily_additions_stop": "0.015", "drawdown_additions_stop": "0.10", "forced_exit": False}


def observe_guard(previous, *, equity, day, external_flows="0"):
    equity, flows = number(equity), number(external_flows)
    if equity <= 0:
        raise ValueError("invalid guarded equity")
    state = (
        copy.deepcopy(previous)
        if previous
        else {
            "day": day,
            "day_start": str(equity),
            "peak": str(equity),
            "external_flows": str(flows),
            "drawdown_latch": False,
            "flow_latch": False,
            "daily_latch": False,
        }
    )
    if state["day"] > day:
        raise ValueError("risk clock regressed")
    if flows != number(state["external_flows"]):
        state["flow_latch"] = True
    if state["day"] != day:
        state.update(day=day, day_start=str(equity), daily_latch=False)
    state["peak"] = str(max(number(state["peak"]), equity))
    if equity <= number(state["peak"]) * number("0.90"):
        state["drawdown_latch"] = True
    if equity <= number(state["day_start"]) * number("0.985"):
        state["daily_latch"] = True
    state["equity"] = str(equity)
    state["additions_allowed"] = not any(state[k] for k in ("daily_latch", "drawdown_latch", "flow_latch"))
    state["forced_exit"] = False
    return state
