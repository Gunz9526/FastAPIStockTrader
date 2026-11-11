"""Quiet, atomic incident notices for the isolated research data worker."""

from psycopg.types.json import Jsonb

from stock_trader.clock import parse_time
from stock_trader.research_seal import fingerprint

NOTICE_KEY = "equity-forward-notices-0091"


def transition(previous, status):
    from stock_trader.equity_forward import CONTRACT

    if (
        status["contract_sha256"] != fingerprint(CONTRACT)
        or status["orders_submitted"] != 0
        or status["mode"] != "research_only"
        or status["automatic_promotion"] is not False
    ):
        raise ValueError("invalid forward research notice contract")
    observed = parse_time(status["heartbeat"])
    if previous and observed <= parse_time(previous["observed_at"]):
        return previous, []
    old = previous or {"transition": 0, "issue": None}
    issue = (
        fingerprint({"state": status["state"], "reason": status.get("reason")})
        if status["state"] in {"review_required", "waiting_keys"}
        else None
    )
    lines = []
    if issue != old["issue"]:
        lines.append(
            "연구 회계 자료: 자료 또는 설정 검토가 필요합니다. 기존 SPY Paper 운영은 별도로 유지됩니다."
            if issue
            else "연구 회계 자료: 수집 준비가 회복되었습니다."
        )
    return {
        "observed_at": observed.isoformat(),
        "transition": old["transition"] + bool(lines),
        "issue": issue,
    }, lines


async def publish(store, status, fence):
    await fence()
    async with store.pool.connection() as connection:
        await connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (NOTICE_KEY,))
        row = await (await connection.execute("SELECT value FROM kv WHERE key=%s", (NOTICE_KEY,))).fetchone()
        previous = row["value"] if row else None
        value, lines = transition(previous, status)
        if value == previous:
            return value
        if lines:
            await connection.execute(
                "INSERT INTO alerts(key,content) VALUES(%s,%s) ON CONFLICT DO NOTHING",
                (f"{NOTICE_KEY}:{value['transition']}", "\n".join(lines)),
            )
        await connection.execute(
            "INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=now()",
            (NOTICE_KEY, Jsonb(value)),
        )
        await fence()
    return value
