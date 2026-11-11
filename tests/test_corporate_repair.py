import copy
import json
from datetime import date, timedelta

import pytest

from stock_trader.corporate_repair import CONTRACT, ROOT, compare, parse_issuer, run
from stock_trader.recovery_bundle import fingerprint


def fixture():
    issuer, actions, html = (
        [],
        [],
        ["<table><tr><th>Declared</th><th>Record</th><th>Payable</th><th>Amount</th><th>Type</th></tr>"],
    )
    for year in range(2016, 2023):
        for month in (2, 5, 8, 11):
            declared = date(year, month, 1)
            record, payment, ex = (
                declared + timedelta(days=7),
                declared + timedelta(days=10),
                declared + timedelta(days=4),
            )
            issuer.append(
                {
                    "declared_date": declared.isoformat(),
                    "record_date": record.isoformat(),
                    "payable_date": payment.isoformat(),
                    "rate": "0.52",
                }
            )
            action = {
                "id": f"{year}-{month}",
                "symbol": "AAPL",
                "ex_date": ex.isoformat(),
                "process_date": ex.isoformat(),
                "rate": 0.52,
                "foreign": False,
                "special": False,
            }
            if len(actions) >= 17:
                action.update(record_date=record.isoformat(), payable_date=payment.isoformat())
            actions.append(action)
            html.append(
                "<tr>"
                + "".join(
                    f"<td><span>{v}</span></td>"
                    for v in [
                        declared.strftime("%b %d, %Y"),
                        record.strftime("%B %d, %Y"),
                        payment.strftime("%B %d, %Y"),
                        "$.52",
                        "Regular Cash",
                    ]
                )
                + "</tr>"
            )
    html.append("</table>")
    return issuer, {"corporate_actions": {"cash_dividends": actions}, "next_page_token": None}, "".join(html)


def test_parse_and_compare_fill_only_missing_fields_with_hindsight_limit():
    issuer, actions, html = fixture()
    assert parse_issuer(html) == issuer
    original = copy.deepcopy(actions)
    result = compare(issuer, actions)
    assert (
        result["state"] == "sample_repair_candidates"
        and result["matched_rows"] == 28
        and result["candidate_rows"] == 17
    )
    assert not result["approved_for_backtest"] and not result["automatic_repair"]
    assert actions == original and not CONTRACT["proves_historical_availability"]
    assert all(set(r["missing_fields_only"]) == {"record_date", "payable_date"} for r in result["candidates"])


@pytest.mark.parametrize(
    "fault",
    ["amount", "existing_pay_date", "existing_record_date", "duplicate_quarter", "ex_order", "special"],
)
def test_any_conflict_blocks_all_repair_candidates(fault):
    issuer, actions, _ = fixture()
    row = actions["corporate_actions"]["cash_dividends"][-1]
    if fault == "amount":
        row["rate"] /= 4
    elif fault == "existing_pay_date":
        row["payable_date"] = "2022-11-30"
    elif fault == "existing_record_date":
        row["record_date"] = "2022-11-30"
    elif fault == "duplicate_quarter":
        row["ex_date"] = "2022-08-05"
    elif fault == "ex_order":
        row["ex_date"] = "2022-11-20"
    else:
        row["special"] = True
    result = compare(issuer, actions)
    assert result["state"] == "blocked_conflict" and result["conflicts"]
    assert result["candidate_rows"] == 0 and result["candidates"] == []


def test_no_unique_complete_issuer_table_or_incomplete_action_source_is_accepted():
    issuer, actions, html = fixture()
    for value in [html + html, html.replace("Regular Cash", "Other", 1), html.replace("Declared", "Changed")]:
        with pytest.raises(ValueError):
            parse_issuer(value)
    actions["next_page_token"] = "more"
    with pytest.raises(ValueError, match="incomplete"):
        compare(issuer, actions)


def test_cached_original_and_assessment_are_immutable_and_restart_needs_no_network(tmp_path):
    _, actions, html = fixture()
    source = tmp_path / "download.html"
    source.write_text(html)
    data = tmp_path / "data"
    original = data / "research/equity-access-0080/actions.json"
    original.parent.mkdir(parents=True)
    payload = {"state": "received", "request": {"params": {"symbols": "AAPL,TWTR"}}, "response": actions}
    original.write_text(json.dumps({"payload": payload, "sha256": fingerprint(payload)}))
    before = original.read_bytes()
    first = run(data, source)
    assert run(data) == first and original.read_bytes() == before
    cached = data / ROOT / "apple-original.html"
    cached.write_text(html + "modified")
    with pytest.raises(ValueError, match="changed"):
        run(data)


def test_duplicate_actions_are_not_matched_to_one_quarter_twice():
    issuer, actions, _ = fixture()
    rows = actions["corporate_actions"]["cash_dividends"]
    rows[-1]["id"] = rows[0]["id"]
    with pytest.raises(ValueError, match="ambiguous"):
        compare(issuer, actions)
