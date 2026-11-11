"""Explicit source coverage gate for ADR-0090. A seal proves integrity, not truth."""

import hashlib
from datetime import date
from pathlib import Path

from stock_trader.clock import parse_time
from stock_trader.equity_contract import EQUITIES, SYMBOLS
from stock_trader.etf_ledger import number
from stock_trader.research_seal import fingerprint, read


def normalized_actions(response, previous_day, day, resolutions=None):
    if response.get("next_page_token") or not isinstance(response.get("corporate_actions"), dict):
        raise ValueError("incomplete corporate actions response")
    output, identities = [], set()
    resolutions = resolutions or {}
    for kind, rows in response["corporate_actions"].items():
        if not isinstance(rows, list):
            raise ValueError("invalid corporate action array")
        for row in rows:
            row = dict(row)
            identity = row["id"]
            if not identity or identity in identities:
                raise ValueError("duplicate corporate action identity")
            identities.add(identity)
            for field, value in resolutions.get(identity, {}).get("fields", {}).items():
                if (
                    field not in {"currency", "effective_date", "payable_date"}
                    or (field in row and row[field] is not None and row[field] != value)
                    or (
                        field == "effective_date"
                        and row.get("ex_date") is not None
                        and row["ex_date"] != value
                    )
                ):
                    raise ValueError("unsupported or conflicting event resolution")
                row[field] = value
            effective = row.get("ex_date") or row.get("effective_date")
            # Missing dates cannot safely be assigned outside the interval.
            if not effective:
                raise ValueError("corporate action has no effective date")
            effective = date.fromisoformat(effective).isoformat()
            if not previous_day < effective <= day:
                continue
            symbol = row.get("symbol") or row.get("acquiree_symbol") or row.get("old_symbol")
            if symbol not in SYMBOLS:
                if (
                    kind in {"cash_mergers", "stock_mergers", "stock_and_cash_mergers"}
                    and row.get("acquirer_symbol") in SYMBOLS
                ):
                    continue
                raise ValueError("unexpected corporate action symbol")
            common = {"id": identity, "symbol": symbol, "ex_date": effective}
            if kind in {"forward_splits", "reverse_splits"}:
                if row.get("new_symbol", symbol) != symbol:
                    raise ValueError("split changes identity")
                old, new = number(row["old_rate"]), number(row["new_rate"])
                if old <= 0 or new <= 0:
                    raise ValueError("invalid split terms")
                output.append({**common, "kind": "split", "ratio": str(new / old)})
            elif kind in {"cash_dividends", "cash_mergers"}:
                if row.get("currency") != "USD":
                    raise ValueError("cash action currency not established")
                if kind == "cash_dividends" and (
                    row.get("special") is not False
                    or row.get("foreign") is not False
                    or row.get("sub_type") is not None
                ):
                    raise ValueError("unsupported dividend type")
                output.append(
                    {
                        **common,
                        "kind": "cash_dividend" if kind == "cash_dividends" else "cash_merger",
                        "rate": str(number(row["rate"])),
                        "pay_date": row["payable_date"],
                        "currency": "USD",
                        "special": False,
                    }
                )
            else:
                raise ValueError("unsupported economic corporate action")
    if not set(resolutions) <= identities:
        raise ValueError("resolution does not belong to input vintage")
    return sorted(output, key=lambda row: row["id"])


def target_weights(target, registration_sha256, session):
    if (
        target["state"] != "recorded"
        or target["execution_session"] != session
        or target["registration_sha256"] != registration_sha256
        or target["orders_submitted"] != 0
        or target["performance_calculated"] is not False
        or not parse_time(target["formation_start"])
        <= parse_time(target["recorded_at"])
        < parse_time(target["intended_open"])
    ):
        raise ValueError("missing or late monthly target")
    weights = target["target_weights"]
    if (
        not set(weights) <= set(EQUITIES)
        or len(weights) > 5
        or any(value != "0.20" for value in weights.values())
        or set(target["selected"]) != set(weights)
        or len(target["selected"]) != len(weights)
        or number(target["cash_weight"]) != 1 - number(".20") * len(weights)
    ):
        raise ValueError("monthly target contract changed")
    return weights


def coverage(root, session, manifest, registration_sha256, actions, now):
    """Require an explicit independent evidence review, never issue one ourselves."""
    path = root / "coverage" / f"{session}.json"
    if not path.exists():
        return None
    value = read(path)
    if (
        value["session"] != session
        or value["registration_sha256"] != registration_sha256
        or value["input_sha256"] != manifest["input_sha256"]
        or value["actions_sha256"] != fingerprint(actions)
        or set(value["symbols"]) != set(SYMBOLS)
        or len(value["symbols"]) != len(SYMBOLS)
        or value["no_gaps"] is not True
        or value["unsupported_actions"] != []
        or not value["reviewer"].strip()
        or value["scope"] != "conditional_fixed_cohort_economic_review"
        or not parse_time(manifest["observed_at"]) <= parse_time(value["reviewed_at"]) <= now
        or not value["evidence"]
    ):
        raise ValueError("coverage review does not bind complete inputs")
    evidence_root = (root / "coverage-evidence").resolve()
    for entry in value["evidence"]:
        relative = Path(entry["file"])
        source = root / "coverage-evidence" / relative
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or source.is_symlink()
            or not source.resolve().is_relative_to(evidence_root)
            or not entry["source_url"].startswith("https://")
            or entry["kind"] not in {"issuer_notice", "exchange_notice", "independent_reconciliation"}
            or hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]
        ):
            raise ValueError("coverage evidence missing or changed")
    evidence_files = {entry["file"] for entry in value["evidence"]}
    for resolution in value.get("resolutions", {}).values():
        if (
            not resolution["fields"]
            or not set(resolution["fields"]) <= {"currency", "effective_date", "payable_date"}
            or not resolution["evidence_files"]
            or not set(resolution["evidence_files"]) <= evidence_files
        ):
            raise ValueError("event resolution lacks independent evidence")
    return value
