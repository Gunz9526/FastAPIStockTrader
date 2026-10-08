"""Persisted research budget; performance rejections differ from infrastructure errors."""

from stock_trader.clock import utcnow

REJECTION_LIMIT = 3


def record_result(previous, report):
    previous = previous or {"consecutive_rejections": 0, "state": "active"}
    if previous.get("last_report") == report["created_at"]:
        return previous
    accepted = report["accepted"]
    final_test_failed = not accepted and "test_metrics" in report
    count = 0 if accepted else previous["consecutive_rejections"] + 1
    blocked = not accepted and (final_test_failed or count >= REJECTION_LIMIT)
    return {
        **previous,
        "state": "research_required" if blocked else "active",
        "consecutive_rejections": count,
        "limit": REJECTION_LIMIT,
        "last_report": report["created_at"],
        "reason": "holdout_failed"
        if final_test_failed
        else "repeated_validation_rejection"
        if blocked
        else "accepted"
        if accepted
        else "validation_rejected",
        "updated_at": utcnow().isoformat(),
        **({"failed_holdout_end": report["data_end"]} if final_test_failed else {}),
    }


async def load_policy(store):
    current = await store.get("training_policy")
    if current is not None:
        return current
    status = await store.get("training_status", {})
    report = status.get("report")
    current = (
        record_result(None, report)
        if report
        else {"state": "active", "consecutive_rejections": 0, "limit": REJECTION_LIMIT}
    )
    await store.put("training_policy", current)
    return current
