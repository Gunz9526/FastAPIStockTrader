"""ADR-0075: future-only posted Paper NAV observations; no broker or model."""

import hashlib
from datetime import date

from psycopg.types.json import Jsonb

from stock_trader.account_ledger import AccountLedger
from stock_trader.clock import NEW_YORK, parse_time, utcnow
from stock_trader.etf_account import economic_time
from stock_trader.etf_funding import LIMIT, REGISTRY
from stock_trader.etf_ledger import ZERO, fingerprint, number
from stock_trader.etf_shadow import CONTRACT as DAILY_CONTRACT
from stock_trader.etf_shadow import eligible_through, read_sealed, session_times, validate_bars

START = date(2026, 10, 19)
REGISTRATION = "paper-study-registration-0075"
REVIEW = "paper-study-review-0075"
CONTRACT = {
    "decision": "ADR-0075",
    "start": START.isoformat(),
    "capital_limit": str(LIMIT),
    "baseline": "first completed eligible session close",
    "minimum_return_intervals": 126,
    "maximum_return_intervals": 252,
    "benchmark": "SPY raw close price return",
    "economic_total_return": "not_calculated",
    "automatic_promotion": False,
    "dated_costs": "business date when transaction time absent",
    "daily_contract_sha256": fingerprint(DAILY_CONTRACT),
}


def measurement(checkpoint, daily, source_sha256, approved_orders):
    session = daily["session"]
    day = date.fromisoformat(daily["date"])
    if day < START or session["date"] != day.isoformat():
        raise ValueError("session outside study")
    validate_bars({"bars": daily["bars"]}, session)
    _, closing = session_times(session)
    rows = sorted(checkpoint["rows"].values(), key=economic_time)
    prefix = [row for row in rows if parse_time(economic_time(row)[0]) <= closing]
    if any(row["activity_type"] in {"CSD", "CSW"} for row in prefix):
        raise ValueError("external cash flow requires study review")
    if any(row["activity_type"] == "FILL" and row["order_id"] not in approved_orders for row in prefix):
        raise ValueError("unowned study activity")
    ledger = AccountLedger(0, reviewed_journals=checkpoint["ledger"]["reviewed_journals"])
    ledger.apply(prefix)
    if set(s for s, q in ledger.positions.items() if q) - {"SPY"}:
        raise ValueError("study universe changed")
    initial = number(ledger.cash_by_type.get("JNLC", 0))
    if initial != 100000:
        raise ValueError("study funding changed")
    price = number(daily["bars"]["SPY"][0]["c"])
    qty = ledger.positions.get("SPY", ZERO)
    whole = ledger.cash + qty * price
    sleeve = LIMIT + ledger.cash - initial + qty * price
    if min(whole, sleeve, price) <= 0:
        raise ValueError("nonpositive study NAV")
    return {
        "date": day.isoformat(),
        "session_close": closing.isoformat(),
        "source_daily_sha256": source_sha256,
        "activity_prefix_sha256": fingerprint(prefix),
        "cash": str(ledger.cash),
        "spy_qty": str(qty),
        "spy_raw_close": str(price),
        "whole_account_nav": str(whole),
        "sleeve_nav": str(sleeve),
        "sleeve_cash": str(LIMIT + ledger.cash - initial),
        "sleeve_exposure": str(qty * price / sleeve),
        "posted_cash_by_type": {k: str(v) for k, v in ledger.cash_by_type.items() if k != "JNLC"},
    }


def metrics(observations):
    if not observations:
        return {"return_intervals": 0}
    result = {"return_intervals": len(observations) - 1}
    for field, label in [
        ("whole_account_nav", "whole_account"),
        ("sleeve_nav", "sleeve"),
        ("spy_raw_close", "spy_price"),
    ]:
        base = number(observations[0][field])
        peak = base
        drawdown = ZERO
        for obs in observations:
            nav = number(obs[field])
            peak = max(peak, nav)
            drawdown = max(drawdown, 1 - nav / peak)
        result[label + "_return"] = str(number(observations[-1][field]) / base - 1)
        result[label + "_max_drawdown"] = str(drawdown)
    result["last_sleeve_exposure"] = observations[-1]["sleeve_exposure"]
    return result


class Study:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store

    async def status(self, state, now, **details):
        result = {
            "state": state,
            "heartbeat": now.isoformat(),
            "contract": CONTRACT,
            "paper_only": True,
            "performance_approved": False,
            "broker_requests": 0,
            **details,
        }
        await self.store.put("performance_status", result)
        return result

    async def tick(self, now=None):
        now = now or utcnow()
        funding = await self.store.get(REGISTRY)
        if not funding:
            return await self.status("waiting_funding_registration", now)
        contract = {"contract": CONTRACT, "funding_registration_sha256": fingerprint(funding)}
        registered = await self.store.get(REGISTRATION)
        if not registered:
            if now.astimezone(NEW_YORK).date() >= START:
                return await self.status("review_required", now, reason="late_registration")
            registered = {**contract, "registered_at": now.isoformat()}
            await self.store.put(REGISTRATION, registered)
        if any(registered.get(k) != v for k, v in contract.items()) or funding.get("capital_limit") != str(
            LIMIT
        ):
            return await self.status("review_required", now, reason="study_contract_changed")
        if await self.store.get(REVIEW):
            return await self.status("review_required", now, reason="historical_activity_changed")
        cutoff = eligible_through(now)
        if cutoff < START:
            return await self.status("not_started", now, observations=0, return_intervals=0)
        root = self.settings.data_dir / "etf-shadow"
        try:
            if read_sealed(root / "registration.json")["contract"] != DAILY_CONTRACT:
                raise ValueError("daily contract changed")
            sessions = {}
            for path in sorted((root / "calendar").glob("*.json")):
                for row in read_sealed(path)["sessions"]:
                    if START <= date.fromisoformat(row["date"]) <= cutoff:
                        sessions[row["date"]] = row
            if not (root / "calendar" / f"{cutoff:%Y-%m}.json").exists():
                return await self.status("waiting_calendar", now)
            cps = await self.store.all(
                "SELECT value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",)
            )
            if len(cps) != 1 or not cps[0]["value"]["reconciliation"]["strategy_ownership_verified"]:
                return await self.status("waiting_account", now)
            cp = cps[0]["value"]
            if (
                not cp["reconciliation"]["matched"]
                or not 0 <= (now - parse_time(cp["last_success_at"])).total_seconds() <= 1200
            ):
                return await self.status("waiting_account", now)
            orders = await self.store.all(
                "SELECT o.id FROM broker_orders o JOIN intents i ON i.client_id=o.client_id "
                "WHERE i.action IN ('etf_buy','etf_sell')"
            )
            approved = {row["id"] for row in orders}
            observations = []
            missing = None
            for day in sorted(sessions)[: CONTRACT["maximum_return_intervals"] + 1]:
                path = root / "daily" / (day + ".json")
                if not path.exists():
                    missing = day
                    break
                daily = read_sealed(path)
                if daily["date"] != day or daily["session"] != sessions[day]:
                    raise ValueError("daily session changed")
                value = measurement(cp, daily, hashlib.sha256(path.read_bytes()).hexdigest(), approved)
                key = "paper-study-observation-0075:" + day
                prior = await self.store.get(key)
                if prior and prior["measurement"] != value:
                    await self.store.put(
                        REVIEW,
                        {
                            "date": day,
                            "prior_sha256": fingerprint(prior["measurement"]),
                            "new_sha256": fingerprint(value),
                        },
                    )
                    return await self.status("review_required", now, reason="historical_activity_changed")
                if not prior:
                    async with self.store.pool.connection() as conn:
                        await conn.execute(
                            "INSERT INTO kv(key,value) VALUES(%s,%s)",
                            (key, Jsonb({"measurement": value, "observed_at": now.isoformat()})),
                        )
                observations.append(value)
            summary = metrics(observations)
            ready = summary["return_intervals"] >= CONTRACT["minimum_return_intervals"]
            return await self.status(
                "waiting_daily" if missing else "collecting" if not ready else "review_ready",
                now,
                observations=len(observations),
                missing_session=missing,
                baseline_date=observations[0]["date"] if observations else None,
                latest_session=observations[-1]["date"] if observations else None,
                economic_total_return="not_calculated",
                **summary,
            )
        except OSError, ValueError, KeyError, TypeError, ArithmeticError:
            return await self.status("review_required", now, reason="input_validation_failed")
