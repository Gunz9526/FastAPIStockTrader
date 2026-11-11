"""ADR-0057: audit a single explicitly reviewed initial journal, with no network."""

import hashlib
import json
from pathlib import Path

from stock_trader.account_ledger import AccountLedger
from stock_trader.clock import utcnow
from stock_trader.etf_ledger import fingerprint
from stock_trader.etf_shadow import read_sealed, seal

source = Path("/app/reports/account-audit-0055/result.json")
expected_source = "312d7c2f88106082fbb812cfd975abc8d54277ab5b6dd687d72a16646aa88b32"
expected_event = "31de6f12fa105206688b4195a968e7f3dc9120493a8cf2d84742516efecddc37"
if hashlib.sha256(source.read_bytes()).hexdigest() != expected_source:
    raise ValueError("source changed")
report = read_sealed(source)
if len(report["activities"]) != 1:
    raise ValueError("bootstrap is not the reviewed single activity")
event = report["activities"][0]
if fingerprint(event) != expected_event:
    raise ValueError("initial journal changed")
ledger = AccountLedger(0, reviewed_journals={event["id"]: expected_event})
ledger.apply([event])
result = ledger.reconcile(cash=report["account"]["cash"], positions=report["positions"])
result["snapshot_stable"] = report["reconciliation"]["snapshot_stable"]
result["audit_passed"] = result["matched"] and result["snapshot_stable"] and not report["open_orders"]
if not result["audit_passed"]:
    raise ValueError("account mismatch remains")
output = {
    "decision": "ADR-0057",
    "checked_at": utcnow().isoformat(),
    "source_checked_at": report["checked_at"],
    "source_sha256": expected_source,
    "reviewed_activity_sha256": expected_event,
    "opening_cash": "0",
    "reconciliation": result,
    "ledger": ledger.snapshot(),
    "new_requests": 0,
    "actual_dividend_observed": False,
    "paper_simulates_dividends": False,
    "orders_submitted": 0,
}
seal(source.with_name("corrected.json"), output)
print(json.dumps({k: output[k] for k in ("checked_at", "reconciliation", "new_requests")}, indent=2))
