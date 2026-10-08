"""Read-only ADR-0041/0042 output/input/ledger audit; no broker or final prices."""

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify(folder):
    report = json.loads((folder / "report.json").read_text())
    if not report.get("completed_at") or report["holdout_evaluated"] or report["promoted"]:
        raise ValueError("completed unpromoted development report required")
    for path, expected in report["input_sha256"].items():
        if digest(Path(path)) != expected:
            raise ValueError("changed development input")
    for name, expected in report["code_sha256"].items():
        if digest(folder / "code" / name) != expected:
            raise ValueError("changed analysis snapshot")
    for name, expected in report["output_sha256"].items():
        if digest(folder / name) != expected:
            raise ValueError("changed trade output")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--orb", type=Path, required=True)
    parser.add_argument("--reversal", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--guard", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    orb, reversal = verify(args.orb), verify(args.reversal)
    expected_guard = "d4b630dbf77191269fa4da5b6e5925779ce75d752978f2265aeca6d975eb1129"
    if digest(args.guard) != expected_guard:
        raise ValueError("consumed final guard changed")
    cache, orb_audit = {}, []
    for candidate in orb["candidates"]:
        for cost in (20, 30):
            ledger = json.loads((args.orb / f"{candidate['rule']}-{cost}bps-trades.json").read_text())
            expected_net = candidate["metrics"][str(cost)]["net_return"] * orb["settings"]["capital_limit"]
            if abs(sum(t["net_pnl"] for t in ledger) - expected_net) > 1e-6:
                raise ValueError("ORB ledger disagreement")
            ambiguous = 0
            for trade in ledger:
                if trade["reason"] not in ("stop", "take"):
                    continue
                timestamp = pd.Timestamp(trade["exit_bar_start"])
                symbol, month = trade["symbol"], timestamp.strftime("%Y-%m")
                key = symbol, month
                if key not in cache:
                    cache[key] = pd.read_parquet(
                        args.native / "bars" / symbol / f"{month}.parquet"
                    ).set_index("timestamp")
                row = cache[key].loc[timestamp]
                ambiguous += int(row["low"] <= trade["stop_price"] and row["high"] >= trade["take_price"])
            orb_audit.append(
                {
                    "rule": candidate["rule"],
                    "cost_bps": cost,
                    "ledger_matches": True,
                    "both_barriers_exit_bars": ambiguous,
                }
            )
    reversal_audit = []
    for evaluation in reversal["evaluations"]:
        cost = int(evaluation["cost_bps"])
        ledger = json.loads((args.reversal / f"reversal_5d-{cost}bps-trades.json").read_text())
        diagnostics = evaluation["diagnostics"]
        net = sum(t["net_pnl"] for t in ledger) + diagnostics["unrealized_after_estimated_exit_cost"]
        expected = evaluation["metrics"]["net_return"] * reversal["settings"]["capital_limit"]
        if abs(net - expected) > 1e-6:
            raise ValueError("reversal marked capital disagreement")
        if len(ledger) != evaluation["metrics"]["trades"]:
            raise ValueError("censored trades counted as completed")
        reversal_audit.append(
            {
                "cost_bps": cost,
                "ledger_matches": True,
                "closed_trades": len(ledger),
                "open_positions": len(diagnostics["open_positions"]),
                "realized_pnl": sum(t["net_pnl"] for t in ledger),
                "unrealized_after_estimated_exit_cost": diagnostics["unrealized_after_estimated_exit_cost"],
            }
        )
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "script_sha256": digest(Path(__file__)),
        "input_and_source_snapshots_match": True,
        "output_checksums_match": True,
        "consumed_guard_sha256": expected_guard,
        "orb_report_sha256": digest(args.orb / "report.json"),
        "reversal_report_sha256": digest(args.reversal / "report.json"),
        "orb": orb_audit,
        "reversal": reversal_audit,
        "new_fits": 0,
        "market_api_requests": 0,
        "new_replays": 0,
    }
    args.output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
