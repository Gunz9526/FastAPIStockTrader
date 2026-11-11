"""ADR-0079: independent Decimal replay of actual fills; no broker requests."""

import hashlib
import json
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "verification/continuation-0079"


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main():
    source = FOLDER / "runtime-before.json"
    data = json.loads(source.read_text())
    assert data["paper_endpoint"] == "https://paper-api.alpaca.markets"
    assert not data["clock"]["is_open"] and len(data["orders"]) == 2
    assert data["counts"] == {"intents": 2, "broker_orders": 2, "controls": 2}
    values = data["values"]
    job = values["etf-auto-cycle:paper-auto-initial-0068"]
    assert job["state"] == "complete_filled" and not job.get("review_required")
    assert values["etf-auto-active-0068"] is None
    registry = values["etf-auto-registration-0072"]
    assert registry["capital_limit"] == "10000" and registry["paper"]
    assert len(data["checkpoints"]) == 1
    cp = data["checkpoints"][0]["value"]
    cash, qty, gross, fees = [Decimal(0) for _ in range(4)]
    fills = {}
    for identity, row in cp["rows"].items():
        assert cp["ledger"]["events"][identity] == fingerprint(row)
        if row["activity_type"] == "JNLC":
            assert cp["ledger"]["reviewed_journals"][identity] == fingerprint(row)
            assert Decimal(row["net_amount"]) == 100000
            cash += Decimal(row["net_amount"])
        elif row["activity_type"] == "FEE":
            assert Decimal(row["net_amount"]) < 0
            cash += Decimal(row["net_amount"])
            fees -= Decimal(row["net_amount"])
        else:
            assert row["activity_type"] == "FILL" and row["symbol"] == "SPY" and row["side"] == "buy"
            q, p = Decimal(row["qty"]), Decimal(row["price"])
            assert q > 0 and p > 0
            entry = fills.setdefault(row["order_id"], {"qty": Decimal(0), "debit": Decimal(0), "parts": 0})
            entry["qty"] += q
            entry["debit"] += q * p
            entry["parts"] += 1
            cash -= q * p
            gross += q * p
            qty += q
    assert set(fills) == {o["id"] for o in data["orders"]}
    for order in data["orders"]:
        assert order["status"] == "filled" and order["side"] == "buy"
        assert (
            order["type"] == "limit" and order["time_in_force"] == "day" and order["extended_hours"] is False
        )
        entry = fills[order["id"]]
        assert entry["qty"] == Decimal(order["filled_qty"]) == Decimal(order["qty"])
        assert abs(entry["debit"] / entry["qty"] - Decimal(order["filled_avg_price"])) <= Decimal("0.000001")
        assert entry["debit"] <= entry["qty"] * Decimal(order["limit_price"])
    payload = job["plan"]["orders"][0]
    order = next(o for o in data["orders"] if o["client_order_id"] == payload["client_order_id"])
    assert all(str(order[k]) == str(payload[k]) for k in ["symbol", "side", "qty", "client_order_id"])
    assert Decimal(order["limit_price"]) == Decimal(payload["limit_price"])
    assert Decimal(job["plan"]["starting_positions"]["SPY"]) + Decimal(order["filled_qty"]) == qty
    assert Decimal(data["positions"]["SPY"]) == Decimal(cp["owned"]["SPY"]) == qty
    assert cash == Decimal(cp["ledger"]["cash"])
    difference = Decimal(data["account"]["cash"]) - cash
    assert abs(difference) <= Decimal("0.01")
    used = max(gross + fees, 100000 - Decimal(data["account"]["cash"])).quantize(
        Decimal(".01"), rounding=ROUND_CEILING
    )
    assert used == Decimal(values["etf_account"]["funding"]["cash_used"]) <= 10000
    assert all(cp["reconciliation"][k] for k in ["matched", "strategy_ownership_verified", "all_fills_owned"])
    result = {
        "decision": "ADR-0079",
        "checked_account_asof": data["checked_at"],
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "actual_expanded_fill_verified": True,
        "filled_at": order["filled_at"],
        "new_qty": order["filled_qty"],
        "new_average_price": order["filled_avg_price"],
        "new_fill_parts": fills[order["id"]]["parts"],
        "new_fill_cash_debit": str(fills[order["id"]]["debit"]),
        "total_qty": str(qty),
        "gross_buy_cash": str(gross),
        "posted_negative_fees": str(fees),
        "reported_cash": data["account"]["cash"],
        "replayed_cash": str(cash),
        "cash_difference": str(difference),
        "cash_used": str(used),
        "remaining_capital": str(10000 - used),
        "ledger_sha256": fingerprint(cp["ledger"]),
        "job_sha256": fingerprint(job),
        "new_broker_requests": 0,
        "new_order_posts": 0,
        "performance_approved": False,
    }
    output = FOLDER / "expanded-fill.json"
    if output.exists():
        assert json.loads(output.read_text()) == result
    else:
        output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
