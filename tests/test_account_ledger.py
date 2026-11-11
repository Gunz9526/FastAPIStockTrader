import copy

import pytest

from stock_trader.account_ledger import AccountLedger, read_activities
from stock_trader.etf_ledger import fingerprint


def fill(identity="one", qty="2", side="buy"):
    return {
        "id": identity,
        "activity_type": "FILL",
        "symbol": "SPY",
        "side": side,
        "qty": qty,
        "cum_qty": "999",
        "price": "100",
        "type": "partial_fill",
        "order_id": "order",
    }


def cash(identity, kind, amount):
    return {"id": identity, "activity_type": kind, "net_amount": amount}


def test_posted_dividend_and_tax_no_gross_or_cumulative_double_count():
    rows = [
        fill(),
        {**cash("div", "DIV", "4"), "qty": "2", "per_share_amount": "2"},
        cash("tax", "DIVNRA", "-1.2"),
        cash("fee", "FEE", "-.01"),
    ]
    ledger = AccountLedger(1000)
    ledger.apply(rows)
    assert ledger.reconcile(cash="802.79", positions={"SPY": "2"})["matched"]
    before = ledger.snapshot()
    ledger = AccountLedger.restore(before)
    ledger.apply(rows)
    assert ledger.snapshot() == before
    assert not ledger.reconcile(cash="804", positions={"SPY": "2"})["matched"]
    assert not ledger.reconcile(cash="802.79", positions={"SPY": "3"})["matched"]


def test_changed_event_and_unsupported_batch_are_atomic():
    ledger = AccountLedger(1000)
    ledger.apply([fill()])
    before = ledger.snapshot()
    with pytest.raises(ValueError, match="changed"):
        ledger.apply([fill(qty="3")])
    with pytest.raises(ValueError, match="unsupported"):
        ledger.apply([cash("fee", "FEE", "-1"), cash("split", "SSP", "0")])
    assert ledger.snapshot() == before


@pytest.mark.parametrize(
    "event",
    [
        fill(qty="NaN"),
        fill(qty=".0000000001"),
        fill(side="sell"),
        cash("x", "CSD", "-1"),
        cash("x", "CSW", "1"),
    ],
)
def test_invalid_values_cannot_change_ledger(event):
    ledger = AccountLedger(1000)
    before = ledger.snapshot()
    with pytest.raises(ValueError):
        ledger.apply([event])
    assert ledger.snapshot() == before


def test_initial_deposit_not_silently_removed_to_match_account():
    ledger = AccountLedger(100000)
    ledger.apply([cash("deposit", "CSD", "100000")])
    assert not ledger.reconcile(cash="100000", positions={})["matched"]
    assert ledger.snapshot()["cash_by_type"] == {"CSD": "100000"}


async def test_pagination_and_private_fields():
    class Broker:
        def __init__(self):
            self.calls = []

        async def request(self, method, path, params):
            self.calls.append(params)
            assert method == "GET" and path == "/v2/account/activities"
            if len(self.calls) == 1:
                return [
                    {**cash(str(i), "DIV", "1"), "description": "omit", "account_id": "omit"}
                    for i in range(100)
                ]
            assert params["page_token"] == "99"
            return [cash("100", "DIV", "1")]

    broker = Broker()
    rows, count = await read_activities(broker)
    assert count == 2 and len(rows) == 101
    assert not any("description" in r or "account_id" in r for r in rows)


async def test_repeated_page_fails_instead_of_publishing_partial_collection():
    class Broker:
        async def request(self, *args, **kwargs):
            return [cash(str(i), "DIV", "1") for i in range(100)]

    with pytest.raises(ValueError, match="repeated"):
        await read_activities(Broker())


async def test_page_limit_never_reports_partial_history_as_complete():
    class Broker:
        calls = 0

        async def request(self, *args, **kwargs):
            start = self.calls * 100
            self.calls += 1
            return [cash(str(i), "DIV", "1") for i in range(start, start + 100)]

    broker = Broker()
    with pytest.raises(ValueError, match="incomplete"):
        await read_activities(broker)
    assert broker.calls == 20


def test_net_dividend_only_and_separate_correction():
    ledger = AccountLedger(10)
    ledger.apply([cash("net", "DIV", "2.8")])
    assert ledger.reconcile(cash="12.8", positions={})["matched"]
    snapshot = copy.deepcopy(ledger.snapshot())
    assert snapshot["cash_by_type"] == {"DIV": "2.8"}


def test_only_reviewed_initial_journal_is_applied_once_and_restored():
    event = cash("initial", "JNLC", "100000")
    ledger = AccountLedger(0, reviewed_journals={"initial": fingerprint(event)})
    ledger.apply([event])
    restored = AccountLedger.restore(ledger.snapshot())
    restored.apply([event])
    assert restored.reconcile(cash="100000", positions={})["matched"]
    before = restored.snapshot()
    with pytest.raises(ValueError):
        restored.apply([cash("other", "JNLC", "100000")])
    with pytest.raises(ValueError):
        restored.apply([cash("initial", "JNLC", "100001")])
    assert restored.snapshot() == before
