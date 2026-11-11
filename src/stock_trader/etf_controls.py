"""ADR-0073/0074: reviewed, idempotent controls under the account writer."""

from psycopg.types.json import Jsonb

from stock_trader.allocation import freeze_plan, plan_allocation
from stock_trader.clock import parse_time, utcnow
from stock_trader.etf_funding import ACTIVE, REGISTRY, job_key
from stock_trader.etf_ledger import SHARE_STEP, ZERO, fingerprint, number
from stock_trader.etf_pilot import binding
from stock_trader.operations import KEY as OPERATIONS_KEY
from stock_trader.operations import entry_allowed

LAST = "etf_controls"
GUARD_FIELDS = ("day", "day_start", "peak", "daily_latch", "drawdown_latch", "flow_latch", "external_flows")


class ControlBlocked(ValueError):
    pass


def approval(checkpoint, registry):
    return {
        "registration_sha256": fingerprint(registry),
        "ledger_sha256": fingerprint(checkpoint["ledger"]),
        "guard": {k: checkpoint["guard"][k] for k in GUARD_FIELDS},
        "owned": checkpoint["owned"],
    }


async def preview(store):
    registry = await store.get(REGISTRY)
    rows = await store.all("SELECT key,value FROM kv WHERE key LIKE %s", ("etf-account-checkpoint:%",))
    if not registry or len(rows) != 1:
        raise ControlBlocked("account_not_registered")
    cp = rows[0]["value"]
    body = approval(cp, registry)
    return {
        "expected_snapshot_sha256": fingerprint(body),
        "approval": body,
        "checkpoint_key": rows[0]["key"],
        "last_success_at": cp["last_success_at"],
        "manual_pause": await store.get("manual_pause", False),
        "active_cycle": await store.get(ACTIVE),
        "capital_limit": registry["capital_limit"],
        "guard": cp["guard"],
        "positions": cp["owned"],
        "flow_reset_supported": False,
    }


class Controls:
    def __init__(self, driver):
        self.driver, self.store = driver, driver.store

    async def complete(self, row, result, *, pause=None):
        async with self.store.pool.connection() as conn:
            changed = await (
                await conn.execute(
                    "UPDATE controls SET state='completed',completed_at=now(),result=%s "
                    "WHERE id=%s AND state='pending' RETURNING id",
                    (Jsonb(result), row["id"]),
                )
            ).fetchone()
            if not changed:
                return
            if pause is not None:
                await conn.execute(
                    "INSERT INTO kv(key,value) VALUES('manual_pause',%s) ON CONFLICT(key) "
                    "DO UPDATE SET value=excluded.value,updated_at=now()",
                    (Jsonb(pause),),
                )
            await conn.execute(
                "INSERT INTO audit(kind,payload) VALUES('reviewed_paper_control',%s)",
                (Jsonb({"control_id": row["id"], "command": row["command"], "result": result}),),
            )
            await conn.execute(
                "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value,updated_at=now()",
                (LAST, Jsonb({"control_id": row["id"], "command": row["command"], **result})),
            )

    async def checked(self, row, snapshot):
        payload = row["payload"]
        if not row.get("request_key") or len(payload.get("reason", "").strip()) < 8:
            raise ControlBlocked("review_details_required")
        if not await self.store.get("manual_pause", False):
            raise ControlBlocked("manual_pause_required")
        if await self.store.get(ACTIVE) or await self.store.unresolved() or snapshot["open_orders"]:
            raise ControlBlocked("orders_must_be_idle")
        if not snapshot["reconciled"] or set(snapshot["positions"]) - {"SPY"}:
            raise ControlBlocked("account_not_reconciled")
        if binding(snapshot["account"]) != self.driver.account_binding:
            raise ControlBlocked("account_binding_changed")
        account = snapshot["account"]
        if (
            account.get("status") != "ACTIVE"
            or account.get("trading_blocked") is not False
            or account.get("account_blocked") is not False
        ):
            raise ControlBlocked("account_not_active")
        if not entry_allowed(await self.store.get(OPERATIONS_KEY, {}), utcnow()):
            raise ControlBlocked("operations_not_ready")
        state = await preview(self.store)
        if state["expected_snapshot_sha256"] != payload.get("expected_snapshot_sha256"):
            raise ControlBlocked("approval_snapshot_changed")
        if not 0 <= (utcnow() - parse_time(snapshot["asof"])).total_seconds() <= 30:
            raise ControlBlocked("account_snapshot_expired")
        return state

    async def reset(self, row):
        snapshot = await self.driver.monitor.sync()
        state = await self.checked(row, snapshot)
        if snapshot["guard"]["flow_latch"]:
            raise ControlBlocked("external_flow_review_required")
        if not (snapshot["guard"]["daily_latch"] or snapshot["guard"]["drawdown_latch"]):
            raise ControlBlocked("risk_is_not_latched")
        key = state["checkpoint_key"]
        async with self.store.pool.connection() as conn:
            await conn.execute("SET LOCAL lock_timeout='15s'")
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("etf-sync:" + key,))
            cp = (await (await conn.execute("SELECT value FROM kv WHERE key=%s", (key,))).fetchone())["value"]
            registry = (
                await (await conn.execute("SELECT value FROM kv WHERE key=%s", (REGISTRY,))).fetchone()
            )["value"]
            if fingerprint(approval(cp, registry)) != row["payload"]["expected_snapshot_sha256"]:
                raise ControlBlocked("approval_snapshot_changed")
            previous = fingerprint(cp["guard"])
            equity = str(number(snapshot["account"]["equity"]))
            cp["guard"].update(
                day_start=equity,
                peak=equity,
                equity=equity,
                daily_latch=False,
                drawdown_latch=False,
                additions_allowed=True,
            )
            result = {
                "applied": "reset-risk",
                "manual_pause": True,
                "risk_reset": True,
                "previous_guard_sha256": previous,
                "guard_sha256": fingerprint(cp["guard"]),
                "reason": row["payload"]["reason"],
            }
            await conn.execute("UPDATE kv SET value=%s,updated_at=now() WHERE key=%s", (Jsonb(cp), key))
            await conn.execute(
                "UPDATE controls SET state='completed',completed_at=now(),result=%s WHERE id=%s",
                (Jsonb(result), row["id"]),
            )
            await conn.execute(
                "INSERT INTO audit(kind,payload) VALUES('reviewed_risk_reset',%s)",
                (Jsonb({"control_id": row["id"], **result}),),
            )
            await conn.execute(
                "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value,updated_at=now()",
                (LAST, Jsonb({"control_id": row["id"], **result})),
            )

    async def exit(self, row):
        snapshot = await self.driver.fresh(allow_paused=True)
        state = await self.checked(row, snapshot)
        owned = number(snapshot["owned"].get("SPY", 0))
        qty = owned if row["command"] == "flatten" else number(row["payload"].get("qty", 0))
        if not ZERO < qty <= owned or qty != qty.quantize(SHARE_STEP):
            raise ControlBlocked("invalid_owned_quantity")
        cycle = f"paper-control-0074-{row['id']}"
        key = f"etf-plan:{cycle}:sell_first"
        plan = await self.store.get(key)
        if not plan:
            plan = plan_allocation(
                cycle=cycle,
                weights={"SPY": "0"},
                positions=snapshot["positions"],
                owned=snapshot["owned"],
                cash=snapshot["account"]["cash"],
                quotes=snapshot["quotes"],
                assets=snapshot["assets"],
                now=utcnow(),
                market_open=snapshot["market_open"],
                open_orders=snapshot["open_orders"],
            )
            if len(plan.get("orders", [])) != 1 or plan["state"] != "sell_first":
                raise ControlBlocked("no_owned_sale")
            plan["orders"][0]["qty"] = str(qty)
            plan.update(
                starting_positions=dict(snapshot["positions"]),
                control_id=row["id"],
                approval_sha256=state["expected_snapshot_sha256"],
            )
            await freeze_plan(self.store, plan)
        job = {"cycle": cycle, "state": "planned", "plan": plan, "control_id": row["id"]}
        async with self.store.pool.connection() as conn:
            await conn.execute("INSERT INTO kv(key,value) VALUES(%s,%s)", (job_key(cycle), Jsonb(job)))
            await conn.execute(
                "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) "
                "DO UPDATE SET value=excluded.value,updated_at=now()",
                (ACTIVE, Jsonb(cycle)),
            )
            await conn.execute(
                "UPDATE controls SET state='executing',result=%s WHERE id=%s",
                (Jsonb({"cycle": cycle, "manual_pause": True}), row["id"]),
            )

    async def process(self):
        rows = await self.store.all("SELECT * FROM controls WHERE state='pending' ORDER BY id LIMIT 10")
        for row in rows:
            await self.driver.executor.fence()
            command = row["command"]
            if command in {"pause", "resume"}:
                await self.complete(row, {"applied": command, "risk_reset": False}, pause=command == "pause")
                continue
            try:
                if command == "reset-risk":
                    await self.reset(row)
                elif command in {"reduce", "flatten"}:
                    await self.exit(row)
                else:
                    raise ControlBlocked("unsupported_command")
            except ControlBlocked as exc:
                await self.complete(row, {"error": str(exc), "applied": False})
            except ValueError:
                await self.complete(row, {"error": "validation_failed", "applied": False})
        return bool(rows)
