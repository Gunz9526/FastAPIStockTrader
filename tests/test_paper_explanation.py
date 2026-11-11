"""ADR-0099: economic boundary cases for the operator-only explanation tool."""

import copy
import runpy
from decimal import Decimal
from pathlib import Path

import pytest

TOOL = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/explain_paper_interval.py"))
interval, explain = TOOL["interval"], TOOL["explain"]


def observation(day, price="100", quantity="90", cash_types=None, activity="a"):
    types = {"FILL": "-9000"} if cash_types is None else cash_types
    cash = Decimal("100000") + sum(map(Decimal, types.values()))
    holding = Decimal(price) * Decimal(quantity)
    return {
        "date": day,
        "cash": str(cash),
        "spy_qty": quantity,
        "spy_raw_close": price,
        "whole_account_nav": str(cash + holding),
        "sleeve_cash": str(cash - 90000),
        "sleeve_nav": str(cash - 90000 + holding),
        "posted_cash_by_type": types,
        "activity_prefix_sha256": activity * 64,
        "source_daily_sha256": "b" * 64,
    }


def audit(rows):
    return {
        "state": "passed",
        "checked_at": "2026-10-21T12:30:00+00:00",
        "broker_requests": 0,
        "database_writes": 0,
        "performance_approved": False,
        "economic_total_return": "not_calculated",
        "measurements": rows,
        "paper_observations": [
            {"date": row["date"], "daily_sha256": row["source_daily_sha256"]} for row in rows
        ],
        "paper_observation_count": len(rows),
        "paper_return_intervals": max(0, len(rows) - 1),
    }


def test_passive_gain_and_cash_effect():
    result = interval(observation("2026-10-19"), observation("2026-10-20", "110"))
    assert Decimal(result["nav_change_usd"]) == 900
    assert Decimal(result["sleeve_return"]) == Decimal(".09")
    assert Decimal(result["whole_account_return"]) == Decimal(".009")
    assert Decimal(result["spy_price_return"]) == Decimal(".10")
    assert Decimal(result["cash_effect_on_relative_return"]) == Decimal("-.01")


def test_passive_loss_cash_reduces_loss():
    result = interval(observation("2026-10-19"), observation("2026-10-20", "90"))
    assert Decimal(result["nav_change_usd"]) == -900
    assert Decimal(result["cash_effect_on_relative_return"]) == Decimal(".01")


@pytest.mark.parametrize(
    "types,expected",
    [({"FILL": "-9000", "FEE": "-.02"}, "-.02"), ({"FILL": "-9000", "DIV": "10", "DIVNRA": "-3"}, "7")],
)
def test_posted_fee_and_net_dividend_are_not_cash_drag(types, expected):
    result = interval(observation("2026-10-19"), observation("2026-10-20", cash_types=types, activity="c"))
    assert Decimal(result["nav_change_usd"]) == Decimal(expected)
    assert result["cash_effect_on_relative_return"] is None
    assert result["identity_residual_usd"] == "0"


def test_buy_explains_new_inventory_and_fill_cash_together():
    result = interval(
        observation("2026-10-19"), observation("2026-10-20", "110", "95", {"FILL": "-9525"}, "c")
    )
    assert Decimal(result["start_inventory_price_effect_usd"]) == 900
    assert Decimal(result["net_quantity_at_end_close_usd"]) == 550
    assert Decimal(result["net_quantity_and_fill_cash_effect_usd"]) == 25
    assert Decimal(result["nav_change_usd"]) == 925
    assert result["cash_effect_on_relative_return"] is None


def test_sell_explains_fill_cash_instead_of_treating_it_as_profit():
    result = interval(
        observation("2026-10-19"), observation("2026-10-20", "110", "85", {"FILL": "-8460"}, "c")
    )
    assert Decimal(result["net_quantity_and_fill_cash_effect_usd"]) == -10
    assert Decimal(result["nav_change_usd"]) == 890


def test_same_quantity_round_trip_cash_is_trading_effect():
    result = interval(
        observation("2026-10-19"), observation("2026-10-20", cash_types={"FILL": "-8990"}, activity="c")
    )
    assert Decimal(result["net_quantity_and_fill_cash_effect_usd"]) == 10
    assert result["cash_effect_on_relative_return"] is None


def test_changed_prefix_even_with_zero_net_cash_is_not_assumed_passive():
    result = interval(observation("2026-10-19"), observation("2026-10-20", activity="c"))
    assert result["cash_effect_on_relative_return"] is None


def test_first_observation_is_baseline_not_inception_profit():
    result = explain(audit([observation("2026-10-19", "110")]))
    assert result["observations"] == 1 and result["return_intervals"] == 0
    assert result["cumulative"] is None
    assert result["performance_approved"] is False


def test_multiple_intervals_use_endpoint_return_not_sum():
    rows = [observation("2026-10-19"), observation("2026-10-20", "110"), observation("2026-10-21", "100")]
    result = explain(audit(rows))
    assert Decimal(result["cumulative"]["sleeve_return"]) == 0
    assert sum(Decimal(row["sleeve_return"]) for row in result["intervals"]) != 0


def test_all_cash_portfolio_and_no_observations():
    result = interval(
        observation("2026-10-19", quantity="0", cash_types={}), observation("2026-10-20", "110", "0", {})
    )
    assert Decimal(result["sleeve_return"]) == 0
    assert Decimal(result["cash_effect_on_relative_return"]) == Decimal("-.1")
    assert explain(audit([]))["cumulative"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("sleeve_nav", "10001"),
        ("cash", "91001"),
        ("spy_qty", "-1"),
        ("spy_raw_close", "0"),
        ("cash", "NaN"),
        ("cash", "Infinity"),
        ("cash", True),
    ],
)
def test_invalid_or_unreconciled_amounts_fail_closed(field, value):
    first, last = observation("2026-10-19"), observation("2026-10-20")
    last[field] = value
    with pytest.raises(ValueError):
        interval(first, last)


def test_external_flow_is_not_reported_as_return():
    last = observation("2026-10-20", cash_types={"FILL": "-9000", "CSD": "100"})
    with pytest.raises(ValueError, match="unsupported posted cash"):
        interval(observation("2026-10-19"), last)


def test_duplicate_or_reversed_dates_fail_closed():
    for day in ("2026-10-19", "2026-10-18"):
        with pytest.raises(ValueError):
            interval(observation("2026-10-19"), observation(day))


def test_report_requires_matching_verified_sources_and_counts():
    valid = audit([observation("2026-10-19"), observation("2026-10-20")])
    for path, value in [("state", "failed"), ("paper_return_intervals", 0), ("performance_approved", True)]:
        changed = copy.deepcopy(valid)
        changed[path] = value
        with pytest.raises(ValueError):
            explain(changed)
    valid["paper_observations"][1]["daily_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="identity mismatch"):
        explain(valid)
