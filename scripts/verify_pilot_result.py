"""Offline, independent Decimal audit of the actual ADR-0067 Paper pilot."""

import hashlib
import json
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def main():
    source = ROOT / "verification/etf-0067/runtime-before.json"
    raw = json.loads(source.read_text())
    job = raw["pilot_job"]
    assert job["state"] == "complete_filled" and not job.get("review_required")
    assert raw["paper_endpoint"] == "https://paper-api.alpaca.markets"
    assert len(raw["orders"]) == len(raw["intents"]) == len(raw["checkpoints"]) == 1
    order, checkpoint = raw["orders"][0], raw["checkpoints"][0]
    payload = job["plan"]["orders"][0]
    assert all(order[k] == payload[k] for k in ["symbol", "side", "client_order_id", "qty", "limit_price"])
    assert order["status"] == "filled" and order["symbol"] == "SPY" and order["side"] == "buy"
    assert Decimal(order["filled_qty"]) == Decimal(job["filled_qty"]) == Decimal(payload["qty"])
    assert Decimal(order["filled_avg_price"]) <= Decimal(payload["limit_price"])
    assert Decimal(payload["qty"]) * Decimal(payload["limit_price"]) <= 100
    cash, qty, fees, buy_cost = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    for identity, row in checkpoint["rows"].items():
        assert checkpoint["ledger"]["events"][identity] == digest(row)
        if row["activity_type"] == "JNLC":
            assert checkpoint["ledger"]["reviewed_journals"][identity] == digest(row)
            cash += Decimal(row["net_amount"])
        elif row["activity_type"] == "FEE":
            cash += Decimal(row["net_amount"])
            fees -= Decimal(row["net_amount"])
        else:
            assert row["activity_type"] == "FILL" and row["order_id"] == order["id"]
            assert row["side"] == "buy" and row["symbol"] == "SPY"
            qty += Decimal(row["qty"])
            debit = Decimal(row["qty"]) * Decimal(row["price"])
            cash -= debit
            buy_cost += debit
    assert qty == Decimal(order["filled_qty"]) == Decimal(raw["positions"][0]["qty"])
    assert qty == Decimal(checkpoint["owned"]["SPY"])
    assert cash == Decimal(checkpoint["ledger"]["cash"])
    difference = Decimal(raw["account"]["cash"]) - cash
    assert abs(difference) <= Decimal("0.01")
    result = {
        "decision": "ADR-0067",
        "actual_fill_verified": True,
        "pilot_cycle": job["cycle"],
        "pilot_job_sha256": digest(job),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "filled_at": order["filled_at"],
        "filled_qty": str(qty),
        "filled_avg_price": order["filled_avg_price"],
        "fill_cash_debit": str(buy_cost),
        "posted_fees": str(fees),
        "reconstructed_cash": str(cash),
        "reported_cash": raw["account"]["cash"],
        "cash_difference": str(difference),
        "performance_approved": False,
        "new_broker_requests": 0,
    }
    folder = ROOT / "reports/paper-pilot-0067"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "validation.json"
    envelope = {"payload": result, "sha256": digest(result)}
    if target.exists():
        assert json.loads(target.read_text()) == envelope
    else:
        target.write_text(json.dumps(envelope, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
