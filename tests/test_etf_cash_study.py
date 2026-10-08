import json
from zipfile import ZipFile

import numpy as np
import pandas as pd
import pytest

from stock_trader.etf_cash_study import CONTRACT, replay, seal_sources, source_audit, ssga_spy
from stock_trader.modeling import checksum


def workbook(path, cusip="78462F103", payable="04/30/2025"):
    rows = [
        {"B": "TICKER", "C": "CUSIP", "D": "EX-DATE", "F": "PAYABLE DATE", "G": "DIVIDEND ($)"},
        {"B": "SPY", "C": cusip, "D": "03/21/2025", "F": payable, "G": "2.0", "H": "0.1"},
        {"B": "SPY", "C": cusip, "D": "09/18/2026", "F": "10/30/2026", "G": "2.0"},
    ]
    body = "".join(
        "<row>"
        + "".join(f'<c r="{k}{i}" t="inlineStr"><is><t>{v}</t></is></c>' for k, v in row.items())
        + "</row>"
        for i, row in enumerate(rows, 1)
    )
    with ZipFile(path, "w") as z:
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + body
            + "</sheetData></worksheet>",
        )


def test_official_us_file_parser_filters_dates_and_rejects_wrong_security(tmp_path):
    path = tmp_path / "fund.xlsx"
    workbook(path)
    rows = ssga_spy(path)
    assert len(rows) == 1 and rows[0]["rate"] == "2.1"
    assert rows[0]["pay_date"] == "2025-04-30"
    workbook(path, cusip="AU000000SPY3")
    with pytest.raises(ValueError, match="wrong US"):
        ssga_spy(path)
    workbook(path, payable="")
    with pytest.raises(ValueError):
        ssga_spy(path)


def test_completed_actions_cache_rejects_mutated_official_file(tmp_path):
    (tmp_path / "manifest.json").write_text(json.dumps(CONTRACT))
    sample = tmp_path / "data"
    sample.write_text("original")
    (tmp_path / "collection.json").write_text(
        json.dumps({"contract": CONTRACT, "file_sha256": {"data": checksum(sample)}})
    )
    assert seal_sources(tmp_path)["contract"] == CONTRACT
    sample.write_text("changed")
    with pytest.raises(ValueError, match="checksum"):
        seal_sources(tmp_path)


def test_unexplained_adjustment_or_missing_distribution_blocks_replay():
    dates = pd.bdate_range("2016-01-01", "2025-12-31")
    ex_dates = dates.to_series().groupby(dates.to_period("Q")).last().dt.strftime("%Y-%m-%d").tolist()
    days = dates.strftime("%Y-%m-%d")
    raw = pd.DataFrame({"date": days, "symbol": "SPY", "close": 100.0})
    adjusted = raw.copy()
    adjusted["close"] = 100 * np.cumprod(np.where(np.isin(days, ex_dates), 1 / 0.99, 1))
    actions = [{"ex_date": d, "rate": "1"} for d in ex_dates]
    source = {"corporate_actions": {"cash_dividends": []}}
    assert source_audit(raw, adjusted, actions, source)["raw_replay_permitted"]
    failed = source_audit(raw, adjusted, actions[1:], source)
    assert not failed["raw_replay_permitted"]
    assert failed["unexpected_adjustment_dates"] == [ex_dates[0]]

    rounded = adjusted.copy()
    rounded["close"] = rounded.close.round(2)
    assert source_audit(raw, rounded, actions, source)["raw_replay_permitted"]
    # Official cash events can repair a known provider omission without claiming
    # that the provider's original adjusted-price series passed its audit.
    omitted = adjusted.copy()
    omitted.loc[omitted.date >= ex_dates[0], "close"] *= 0.99
    repaired = source_audit(raw, omitted, actions, source)
    assert repaired["raw_replay_permitted"]
    assert not repaired["checks"]["adjustment_amounts_match"]
    assert repaired["distribution_without_adjustment"] == [ex_dates[0]]


def test_ambiguous_dividend_batch_fails_atomically():
    from stock_trader.etf_ledger import CashLedger

    book = CashLedger(100)
    snapshot = book.snapshot()
    action = {
        "id": "d",
        "kind": "cash_dividend",
        "symbol": "SPY",
        "ex_date": "2025-03-21",
        "pay_date": "2025-04-30",
        "rate": "1",
        "currency": "USD",
        "special": False,
    }
    with pytest.raises(ValueError, match="multiple same-day"):
        book.open_day("2025-03-21", [action, {**action, "id": "other"}])
    assert book.snapshot() == snapshot


def test_raw_dividend_reinvestment_preserves_value_and_cannot_spend_receivable():
    raw = pd.DataFrame(
        {
            "date": ["2018-01-02", "2018-01-03", "2018-01-05", "2018-02-01"],
            "symbol": "SPY",
            "open": [100, 99, 99, 99],
            "close": [100, 99, 99, 99],
        }
    )
    action = {
        "id": "d1",
        "kind": "cash_dividend",
        "symbol": "SPY",
        "ex_date": "2018-01-03",
        "pay_date": "2018-01-05",
        "rate": "1",
        "currency": "USD",
        "special": False,
    }
    metrics, curve, snapshot = replay(raw, [action], 0)
    assert curve.equity.to_list() == pytest.approx([100000] * 4)
    assert curve.loc["2018-01-03", "cash"] == "0E-9"
    assert float(curve.loc["2018-01-03", "receivable"]) == 1000
    assert float(curve.loc["2018-01-05", "cash"]) == 1000
    assert float(curve.loc["2018-02-01", "cash"]) < 1e-6
    assert metrics["net_return"] == pytest.approx(0)
    assert len([e for e in snapshot["journal"] if e["type"] == "dividend_payment"]) == 1
