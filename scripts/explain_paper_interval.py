"""ADR-0099: explain verified posted NAVs; stdin JSON, no DB/network/secrets."""

import json
import re
import sys
from datetime import date
from decimal import Decimal, localcontext
from itertools import pairwise

ZERO = Decimal(0)
INITIAL = Decimal("100000")
CAPITAL = Decimal("10000")
CASH_TYPES = {"FILL", "FEE", "DIV", "DIVNRA", "DIVFT", "DIVTW", "DIVFEE"}
SHA = re.compile(r"[0-9a-f]{64}")


def amount(value):
    if not isinstance(value, str):
        raise ValueError("amount must be a decimal string")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("nonfinite amount")
    return result


def validate(row):
    if date.fromisoformat(row["date"]).isoformat() != row["date"]:
        raise ValueError("invalid observation date")
    for key in ("source_daily_sha256", "activity_prefix_sha256"):
        if not isinstance(row[key], str) or SHA.fullmatch(row[key]) is None:
            raise ValueError("invalid source fingerprint")
    values = {
        key: amount(row[key])
        for key in ("cash", "spy_qty", "spy_raw_close", "whole_account_nav", "sleeve_nav", "sleeve_cash")
    }
    cash = row["posted_cash_by_type"]
    if not isinstance(cash, dict) or set(cash) - CASH_TYPES:
        raise ValueError("unsupported posted cash type")
    values["cash_by_type"] = {key: amount(value) for key, value in cash.items()}
    if (
        values["cash"] < 0
        or values["spy_qty"] < 0
        or min(values[k] for k in ("spy_raw_close", "whole_account_nav", "sleeve_nav")) <= 0
    ):
        raise ValueError("invalid account amounts")
    if values["cash"] != INITIAL + sum(values["cash_by_type"].values(), ZERO):
        raise ValueError("posted cash does not reconcile")
    inventory = values["spy_qty"] * values["spy_raw_close"]
    if (
        values["whole_account_nav"] != values["cash"] + inventory
        or values["sleeve_cash"] != CAPITAL + values["cash"] - INITIAL
        or values["sleeve_nav"] != values["sleeve_cash"] + inventory
    ):
        raise ValueError("NAV does not reconcile")
    return values


def interval(start, end):
    with localcontext() as context:
        context.prec = 50
        first, last = validate(start), validate(end)
        if end["date"] <= start["date"]:
            raise ValueError("observation dates not increasing")
        cash_changes = {
            key: last["cash_by_type"].get(key, ZERO) - first["cash_by_type"].get(key, ZERO)
            for key in sorted(first["cash_by_type"].keys() | last["cash_by_type"].keys())
        }
        price_pnl = first["spy_qty"] * (last["spy_raw_close"] - first["spy_raw_close"])
        quantity_value = (last["spy_qty"] - first["spy_qty"]) * last["spy_raw_close"]
        cash_change = last["cash"] - first["cash"]
        nav_change = last["sleeve_nav"] - first["sleeve_nav"]
        if (
            cash_change != sum(cash_changes.values(), ZERO)
            or nav_change != price_pnl + quantity_value + cash_change
            or nav_change != last["whole_account_nav"] - first["whole_account_nav"]
        ):
            raise ValueError("interval attribution does not reconcile")
        spy_return = last["spy_raw_close"] / first["spy_raw_close"] - 1
        sleeve_return = nav_change / first["sleeve_nav"]
        no_activity = (
            start["activity_prefix_sha256"] == end["activity_prefix_sha256"]
            and quantity_value == 0
            and all(change == 0 for change in cash_changes.values())
        )
        exposure = first["spy_qty"] * first["spy_raw_close"] / first["sleeve_nav"]
        return {
            "start": start["date"],
            "end": end["date"],
            "nav_change_usd": str(nav_change),
            "start_inventory_price_effect_usd": str(price_pnl),
            "net_quantity_at_end_close_usd": str(quantity_value),
            "posted_cash_change_usd": str(cash_change),
            "posted_cash_changes_by_type_usd": {key: str(value) for key, value in cash_changes.items()},
            "net_quantity_and_fill_cash_effect_usd": str(quantity_value + cash_changes.get("FILL", ZERO)),
            "sleeve_return": str(sleeve_return),
            "whole_account_return": str(nav_change / first["whole_account_nav"]),
            "spy_price_return": str(spy_return),
            "sleeve_minus_spy_price_return": str(sleeve_return - spy_return),
            "starting_sleeve_exposure": str(exposure),
            "unchanged_inventory_and_activity_prefix": no_activity,
            "cash_effect_on_relative_return": str(sleeve_return - spy_return) if no_activity else None,
            "identity_residual_usd": "0",
        }


def explain(audit):
    if (
        audit["state"] != "passed"
        or audit["broker_requests"] != 0
        or audit["database_writes"] != 0
        or audit["performance_approved"] is not False
        or audit["economic_total_return"] != "not_calculated"
    ):
        raise ValueError("a passed read-only source audit is required")
    rows, proofs = audit["measurements"], audit["paper_observations"]
    if (
        len(rows) != len(proofs)
        or len(rows) != audit["paper_observation_count"]
        or max(0, len(rows) - 1) != audit["paper_return_intervals"]
    ):
        raise ValueError("source observation count mismatch")
    with localcontext() as context:
        context.prec = 50
        for row, proof in zip(rows, proofs, strict=True):
            validate(row)
            if row["date"] != proof["date"] or row["source_daily_sha256"] != proof["daily_sha256"]:
                raise ValueError("source observation identity mismatch")
        intervals = [interval(first, last) for first, last in pairwise(rows)]
        cumulative = interval(rows[0], rows[-1]) if len(rows) >= 2 else None
        if cumulative and sum((amount(row["nav_change_usd"]) for row in intervals), ZERO) != amount(
            cumulative["nav_change_usd"]
        ):
            raise ValueError("cumulative NAV change does not reconcile")
    return {
        "decision": "ADR-0099",
        "state": "passed",
        "source_checked_at": audit["checked_at"],
        "baseline_date": rows[0]["date"] if rows else None,
        "latest_session": rows[-1]["date"] if rows else None,
        "observations": len(rows),
        "return_intervals": len(intervals),
        "intervals": intervals,
        "cumulative": cumulative,
        "scope": "posted NAV since first observation; raw SPY close price benchmark",
        "economic_total_return": "not_calculated",
        "performance_approved": False,
        "annualized_statistics": "not_calculated",
        "broker_requests": 0,
        "database_writes": 0,
    }


if __name__ == "__main__":
    print(json.dumps(explain(json.load(sys.stdin)), indent=2))
