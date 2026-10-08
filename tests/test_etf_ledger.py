import copy
import json
from decimal import Decimal

import pytest

from stock_trader.etf_ledger import CashLedger, number


def dividend(**changes):
    return {
        "id": "div-1",
        "kind": "cash_dividend",
        "symbol": "SPY",
        "ex_date": "2025-03-21",
        "pay_date": "2025-04-30",
        "rate": "2",
        "currency": "USD",
        "special": False,
        **changes,
    }


def funded():
    book = CashLedger(1000)
    book.open_day("2025-03-20")
    book.fill("buy-1", "SPY", "buy", "5", "100")
    return book


def test_entitlement_survives_sale_ex_day_buyer_excluded_and_no_early_cash():
    book = funded()
    book.open_day("2025-03-21", [dividend()])
    assert book.cash == 500
    assert book.equity({"SPY": "98"}) == 1000
    book.fill("sell-1", "SPY", "sell", "5", "98")
    book.fill("buy-on-ex", "SPY", "buy", "6", "98")
    assert book.receivables["div-1"]["amount"] == "10"
    book.open_day("2025-04-29")
    assert book.cash == 402
    book.open_day("2025-04-30")
    assert book.cash == 412 and not book.receivables


def test_receivable_cannot_fund_buy_and_failure_is_atomic():
    book = CashLedger(500)
    book.open_day("2025-03-20")
    book.fill("buy", "SPY", "buy", 5, 100)
    book.open_day("2025-03-21", [dividend()])
    before = book.snapshot()
    with pytest.raises(ValueError, match="borrowing"):
        book.fill("bad", "SPY", "buy", "0.1", 98)
    assert book.snapshot() == before


def test_restart_duplicate_fill_and_payment_exactly_once_changed_event_rejected():
    book = funded()
    book.open_day("2025-03-21", [dividend()])
    restarted = CashLedger.restore(json.loads(json.dumps(book.snapshot())))
    restarted.fill("buy-1", "SPY", "buy", 5, 100)
    assert restarted.snapshot() == book.snapshot()
    restarted.open_day("2025-04-30")
    restarted.open_day("2025-04-30")
    assert restarted.cash == 510
    assert len([e for e in restarted.journal if e["type"] == "dividend_payment"]) == 1
    with pytest.raises(ValueError, match="changed event"):
        restarted.fill("buy-1", "SPY", "buy", 6, 100)
    with pytest.raises(ValueError, match="late or changed"):
        restarted.open_day("2025-03-21", [dividend(rate="3")])


def test_weekend_payable_available_next_observed_session():
    book = funded()
    book.open_day("2025-03-21", [dividend(pay_date="2025-03-23")])
    assert book.cash == 500
    book.open_day("2025-03-24")
    assert book.cash == 510


def test_split_preserves_equity_without_creating_cash():
    book = funded()
    book.open_day(
        "2025-03-21",
        [{"id": "split", "kind": "split", "symbol": "SPY", "ratio": "0.5", "ex_date": "2025-03-21"}],
    )
    assert book.positions["SPY"] == Decimal("2.5") and book.cash == 500
    assert book.equity({"SPY": 200}) == 1000


@pytest.mark.parametrize(
    "change",
    [
        {"pay_date": ""},
        {"pay_date": "2025-03-20"},
        {"special": True},
        {"currency": "EUR"},
        {"rate": "NaN"},
        {"kind": "merger"},
    ],
)
def test_invalid_actions_never_partially_mutate_state(change):
    book = funded()
    before = book.snapshot()
    with pytest.raises((ValueError, KeyError)):
        book.open_day("2025-03-21", [dividend(**change)])
    assert book.snapshot() == before


def test_same_day_late_action_cannot_capture_post_ex_purchase():
    book = funded()
    book.open_day("2025-03-21")
    book.fill("ex-buy", "SPY", "buy", 1, 100)
    with pytest.raises(ValueError, match="changed session"):
        book.open_day("2025-03-21", [dividend()])


def test_fees_and_fractional_quantities_survive_json_and_reject_short():
    book = CashLedger(1000)
    book.open_day("2025-03-20")
    book.fill("small", "SPY", "buy", "1.123456789", "100.01", "0.25")
    assert book.cash == number(1000) - number("1.123456789") * number("100.01") - number("0.25")
    assert CashLedger.restore(json.loads(json.dumps(book.snapshot()))).snapshot() == book.snapshot()
    snapshot = copy.deepcopy(book.snapshot())
    with pytest.raises(ValueError):
        book.fill("over", "SPY", "sell", "2", "100")
    assert book.snapshot() == snapshot
