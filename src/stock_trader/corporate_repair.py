"""ADR-0084: offline issuer comparison; proposed missing dates never alter raw data."""

import argparse
import hashlib
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path

from stock_trader.recovery_bundle import fingerprint, stable_copy, write_json

URL = "https://investor.apple.com/dividend-history/default.aspx"
ROOT = "research/corporate-repair-0084"
CONTRACT = {
    "decision": "ADR-0084",
    "source": URL,
    "symbol": "AAPL",
    "start": "2016-01-01",
    "end": "2022-12-31",
    "expected_cash_dividends": 28,
    "amount_basis": "not_split_adjusted",
    "new_broker_requests": 0,
    "strategy_evaluations": 0,
    "changes_raw_data": False,
    "proves_historical_availability": False,
    "full_market_point_in_time_ready": False,
}


class Tables(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tables, self.table, self.row, self.cell = [], None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            if self.table is not None:
                raise ValueError("nested issuer table")
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []
        elif tag == "br" and self.cell is not None:
            self.cell.append(" ")

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def issuer_date(value):
    value = re.sub(r"\s*,\s*", ", ", value.strip())
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    raise ValueError("invalid issuer date")


def parse_issuer(html):
    parser = Tables()
    parser.feed(html)
    matches = [
        table
        for table in parser.tables
        if table and table[0] == ["Declared", "Record", "Payable", "Amount", "Type"]
    ]
    if len(matches) != 1:
        raise ValueError("ambiguous issuer history table")
    rows = []
    for cells in matches[0][1:]:
        if len(cells) != 5:
            raise ValueError("invalid issuer history row")
        declared = issuer_date(cells[0])
        if cells[4] != "Regular Cash" or not 2016 <= declared.year <= 2022:
            continue
        record, payment = issuer_date(cells[1]), issuer_date(cells[2])
        amount = Decimal(cells[3].removeprefix("$").strip())
        if (
            not amount.is_finite()
            or amount <= 0
            or not declared < record <= payment
            or not 2016 <= payment.year <= 2022
        ):
            raise ValueError("invalid issuer cash dates or amount")
        rows.append(
            {
                "declared_date": declared.isoformat(),
                "record_date": record.isoformat(),
                "payable_date": payment.isoformat(),
                "rate": str(amount),
            }
        )
    keys = [(r["payable_date"][:4], (date.fromisoformat(r["payable_date"]).month - 1) // 3) for r in rows]
    if len(rows) != 28 or len(set(keys)) != 28 or {int(k[0]) for k in keys} != set(range(2016, 2023)):
        raise ValueError("incomplete or duplicate issuer cash history")
    return sorted(rows, key=lambda r: r["payable_date"])


def sealed(path):
    if path.is_symlink():
        raise ValueError("source link rejected")
    value = json.loads(path.read_text())
    if fingerprint(value["payload"]) != value["sha256"]:
        raise ValueError("source seal mismatch")
    return value["payload"]


def compare(issuer, actions):
    if actions.get("next_page_token"):
        raise ValueError("incomplete corporate action page")
    rows = actions["corporate_actions"]["cash_dividends"]
    if len(rows) != 28 or len({r["id"] for r in rows}) != 28 or any(r["symbol"] != "AAPL" for r in rows):
        raise ValueError("incomplete or ambiguous action sample")
    mapped = {
        (int(r["payable_date"][:4]), (date.fromisoformat(r["payable_date"]).month - 1) // 3): r
        for r in issuer
    }
    if len(mapped) != 28:
        raise ValueError("ambiguous issuer cash sample")
    candidates, conflicts, matched, used = [], [], [], set()
    for row in sorted(rows, key=lambda r: r["ex_date"]):
        ex = date.fromisoformat(row["ex_date"])
        key = ex.year, (ex.month - 1) // 3
        other = mapped.get(key)
        reasons = []
        if not other or key in used:
            reasons.append("missing_or_duplicate_quarter")
        else:
            used.add(key)
            declared = date.fromisoformat(other["declared_date"])
            record = date.fromisoformat(other["record_date"])
            if not declared < ex <= record or not 0 <= (record - ex).days <= 7:
                reasons.append("ex_record_date_order_or_distance")
            amount = Decimal(str(row["rate"]))
            if not amount.is_finite() or amount != Decimal(other["rate"]):
                reasons.append("unadjusted_amount_mismatch")
            if row.get("foreign") is not False or row.get("special") is not False:
                reasons.append("unreviewed_dividend_type")
            for field in ("record_date", "payable_date"):
                if row.get(field) and row[field] != other[field]:
                    reasons.append(field + "_conflict")
        if reasons:
            conflicts.append({"action_id": row["id"], "ex_date": row["ex_date"], "reasons": reasons})
            continue
        matched.append(row["id"])
        missing = {field: other[field] for field in ("record_date", "payable_date") if not row.get(field)}
        if missing:
            candidates.append(
                {
                    "action_id": row["id"],
                    "symbol": "AAPL",
                    "ex_date": row["ex_date"],
                    "rate": other["rate"],
                    "declared_date": other["declared_date"],
                    "missing_fields_only": missing,
                }
            )
    return {
        "contract": CONTRACT,
        "state": "sample_repair_candidates" if not conflicts else "blocked_conflict",
        "issuer_rows": len(issuer),
        "action_rows": len(rows),
        "matched_rows": len(matched),
        "conflicts": conflicts,
        "candidates": candidates if not conflicts else [],
        "candidate_rows": len(candidates) if not conflicts else 0,
        "approved_for_backtest": False,
        "automatic_repair": False,
    }


def run(data, html=None):
    root = data / ROOT
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    source, registration = root / "apple-original.html", root / "source.json"
    if registration.exists():
        registered = sealed(registration)
        if (
            registered["contract"] != CONTRACT
            or source.is_symlink()
            or hashlib.sha256(source.read_bytes()).hexdigest() != registered["raw_sha256"]
        ):
            raise ValueError("cached issuer source changed")
        if html is not None and hashlib.sha256(html.read_bytes()).hexdigest() != registered["raw_sha256"]:
            raise ValueError("new issuer input conflicts with registered source")
    else:
        if html is None or source.exists():
            raise ValueError("intact original issuer capture required")
        if not 0 < html.stat().st_size <= 5_000_000:
            raise ValueError("issuer source size limit")
        parse_issuer(html.read_text())
        stable_copy(html, source)
        registered = {
            "contract": CONTRACT,
            "raw_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "captured_at": datetime.fromtimestamp(html.stat().st_mtime, UTC).isoformat(),
            "registered_at": datetime.now(UTC).isoformat(),
        }
        write_json(registration, {"payload": registered, "sha256": fingerprint(registered)})
    actions_path = data / "research/equity-access-0080/actions.json"
    actions = sealed(actions_path)
    if actions["state"] != "received" or actions["request"]["params"]["symbols"] != "AAPL,TWTR":
        raise ValueError("unexpected action source")
    result = {
        **compare(parse_issuer(source.read_text()), actions["response"]),
        "issuer_source": registered,
        "action_source_sha256": fingerprint(actions),
    }
    target = root / "assessment.json"
    if target.exists():
        if sealed(target) != result:
            raise ValueError("immutable issuer assessment changed")
    else:
        write_json(target, {"payload": result, "sha256": fingerprint(result)})
    return {
        key: result[key]
        for key in (
            "state",
            "issuer_rows",
            "action_rows",
            "matched_rows",
            "candidate_rows",
            "conflicts",
            "approved_for_backtest",
        )
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("/app/data"))
    parser.add_argument("--html", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.data, args.html), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"state": "failed", "error": type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
