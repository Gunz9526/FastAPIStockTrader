"""ADR-0087: atomic research notices in the existing queue; no webhook access."""

from psycopg.types.json import Jsonb

from stock_trader.clock import parse_time
from stock_trader.equity_contract import CONTRACT
from stock_trader.research_seal import fingerprint

NOTICE_KEY = "equity-shadow-notices-0087"


def transition(previous, status):
    observed = parse_time(status["heartbeat"])
    if (
        status.get("contract_sha256") != fingerprint(CONTRACT)
        or status.get("orders_submitted") != 0
        or status.get("performance_calculated") is not False
        or status.get("mode") != "research_only"
    ):
        raise ValueError("invalid research notification contract")
    if previous and observed <= parse_time(previous["observed_at"]):
        return previous, []
    old = previous or {"transition": 0, "issue": None, "targets": 0, "missed": 0}
    issue = (
        fingerprint({"state": status["state"], "reason": status.get("reason")})
        if status["state"] in {"review_required", "waiting_keys", "failed"}
        else None
    )
    targets, missed = (
        status.get("targets_recorded", old["targets"]),
        status.get("missed_targets", old["missed"]),
    )
    if any(type(value) is not int or value < 0 for value in (targets, missed)):
        raise ValueError("invalid research notification counts")
    targets, missed = max(targets, old["targets"]), max(missed, old["missed"])
    lines = []
    if issue != old["issue"]:
        if issue:
            lines.append(
                "개별주 연구: 자료 또는 설정 검토가 필요합니다. 기존 SPY Paper 매매는 별도로 유지됩니다."
            )
        elif old["issue"]:
            lines.append("개별주 연구: 자료 준비가 회복되었습니다.")
    if targets > old["targets"]:
        lines.append(f"연구용 월초 목표 기록 {targets}건. 실제 개별주 주문은 보내지 않았습니다.")
    if missed > old["missed"]:
        lines.append(f"연구용 월초 기록 누락 {missed}건. 과거 기록을 소급 생성하지 않습니다.")
    return {
        "observed_at": observed.isoformat(),
        "transition": old["transition"] + bool(lines),
        "issue": issue,
        "targets": targets,
        "missed": missed,
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
