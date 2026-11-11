"""ADR-0078: metadata-only feasibility audit; never reads prices or evaluates returns."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    sources = {}
    for name in [
        "native-iex-5m-development",
        "swing-daily-development",
        "etf-daily-0046",
        "etf-actions-0048",
    ]:
        folder = ROOT / "data/research" / name
        manifest, collected = [
            json.loads((folder / f).read_text()) for f in ["manifest.json", "collection.json"]
        ]
        assert collected["contract"] == manifest, name
        sources[name] = {
            "contract": manifest,
            "manifest_sha256": sha(folder / "manifest.json"),
            "collection_sha256": sha(folder / "collection.json"),
            "completed_at": collected["completed_at"],
            "listed_files": len(collected["file_sha256"]),
            "historical_collection_counts": {
                k: collected[k]
                for k in [
                    "pages",
                    "requests",
                    "sessions",
                    "rows_per_adjustment",
                    "alpaca_gets",
                    "alpaca_events",
                    "corporate_actions_available",
                ]
                if k in collected
            },
        }
    stocks = set(sources["swing-daily-development"]["contract"]["symbols"]) - {"SPY", "QQQ"}
    assert len(stocks) == 28
    guard = ROOT / "reports/final-test-consumed-0025.json"
    assert sha(guard) == "d4b630dbf77191269fa4da5b6e5925779ce75d752978f2265aeca6d975eb1129"
    result = {
        "decision": "ADR-0078",
        "checked_at": datetime.now(UTC).isoformat(),
        "state": "metadata_audited_full_market_research_not_ready",
        "sources": sources,
        "fixed_cohort_individual_stocks": len(stocks),
        "findings": {
            "fixed_cohort_development_possible": True,
            "cohort_is_point_in_time_membership": "not_established_by_metadata",
            "delisted_security_coverage": "not_established_by_metadata",
            "full_equity_action_and_payment_coverage": "not_established_by_metadata",
            "eight_year_equity_history": False,
            "eight_year_etf_history": True,
            "independent_final_window_for_new_strategy": "not_registered",
        },
        "price_files_opened": 0,
        "broker_requests": 0,
        "new_collection": 0,
        "model_training": 0,
        "strategy_evaluations": 0,
        "final_guard_sha256": sha(guard),
        "production_strategy_changed": False,
    }
    output = ROOT / "verification/controls-0072/equity-data-audit.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                k: result[k]
                for k in [
                    "state",
                    "fixed_cohort_individual_stocks",
                    "price_files_opened",
                    "broker_requests",
                    "strategy_evaluations",
                ]
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
