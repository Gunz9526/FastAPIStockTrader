"""ADR-0085/0086 fixed, conditional future experiment. No trading authorization."""

from datetime import date

from stock_trader.etf_ledger import number

EQUITIES = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AVGO",
    "AMD",
    "NFLX",
    "COST",
    "WMT",
    "JPM",
    "BAC",
    "GS",
    "XOM",
    "CVX",
    "UNH",
    "JNJ",
    "ABBV",
    "LLY",
    "PG",
    "HD",
    "CAT",
    "GE",
    "CRM",
    "ORCL",
    "ADBE",
)
SYMBOLS = (*EQUITIES, "SPY")
FIRST = date(2026, 11, 1)
LAST = date(2027, 10, 1)
KEY = "equity_shadow_0086"
REGISTRY = "equity-study-registration-0086"
LEADER = "equity-shadow-0086"
ROOT = "research/equity-shadow-0086"
CONTRACT = {
    "notification_policy": "ADR-0087 meaningful changes only, atomic shared queue",
    "decision": "ADR-0085/ADR-0086",
    "version": 86,
    "scope": "prospective_fixed_cohort_only",
    "equities": list(EQUITIES),
    "benchmark": "SPY",
    "first_month": FIRST.isoformat(),
    "last_month": LAST.isoformat(),
    "signal": "split_adjusted_price_momentum_12_2",
    "numerator_month_offset": -2,
    "denominator_month_offset": -13,
    "slots": 5,
    "slot_weight": "0.20",
    "positive_only": True,
    "tie_break": "symbol_ascending",
    "unfilled_slots": "cash",
    "formation": "last prior session next New York date 06:00 until first regular open",
    "exit_hypothesis": "monthly target reduction only",
    "feed": "sip",
    "timeframe": "1Day",
    "anchor_adjustments": ["raw", "split"],
    "symbol_asof": "cohort_registration_New_York_date",
    "missing_or_changed_identity": "review_required_no_substitution",
    "anchor_revisions": "freeze_first_response_per_cycle",
    "max_logical_gets_per_tick": 4,
    "max_transport_attempts_per_tick": 16,
    "poll_seconds": 900,
    "orders_enabled": False,
    "account_controls_enabled": False,
    "performance_calculated": False,
    "full_market_history_proven": False,
    "automatic_promotion": False,
    "future_accounting": {
        "one_way_cost_bps": "10",
        "stress_one_way_cost_bps": "25",
        "raw_share_ledger_required": True,
        "cash_events_and_payment_dates_required": True,
        "minimum_return_intervals": 126,
        "minimum_monthly_decisions": 6,
    },
}


def shift(month, delta):
    index = month.year * 12 + month.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def rank_targets(earlier, later, eligible):
    """Prices already validated against exact anchors; no actual account or cash."""
    if set(earlier) != set(SYMBOLS) or set(later) != set(SYMBOLS) or set(eligible) != set(EQUITIES):
        raise ValueError("research signal universe changed")
    scores = {}
    for symbol in EQUITIES:
        a, b = number(earlier[symbol]), number(later[symbol])
        if a <= 0 or b <= 0 or type(eligible[symbol]) is not bool:
            raise ValueError("invalid signal input")
        scores[symbol] = b / a - 1
    order = sorted((s for s in EQUITIES if eligible[s] and scores[s] > 0), key=lambda s: (-scores[s], s))
    selected = order[: CONTRACT["slots"]]
    return {
        "scores": {s: str(scores[s]) for s in sorted(scores)},
        "selected": selected,
        "target_weights": {s: "0.20" for s in selected},
        "cash_weight": str(1 - number(".20") * len(selected)),
        "orders_submitted": 0,
        "performance_calculated": False,
    }
