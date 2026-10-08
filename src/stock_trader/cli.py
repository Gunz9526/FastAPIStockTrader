import argparse
import asyncio
import json
import signal
from datetime import datetime, timedelta
from pathlib import Path

from stock_trader.broker import Alpaca
from stock_trader.clock import utcnow
from stock_trader.config import Settings
from stock_trader.data import collect_history, read_history
from stock_trader.engine import Engine
from stock_trader.logging import configure_logging, event
from stock_trader.notifications import Notifier
from stock_trader.research import train_and_evaluate
from stock_trader.storage import Store
from stock_trader.training_policy import load_policy, record_result


async def train_job(settings, store):
    if not settings.credentials_present:
        await store.put(
            "training_status", {"state": "waiting_alpaca_keys", "updated_at": utcnow().isoformat()}
        )
        return
    broker = Alpaca(settings)
    try:
        async with store.leader("training-job") as training_leader:
            policy = await load_policy(store)
            await store.put("training_status", {"state": "collecting", "updated_at": utcnow().isoformat()})
            await collect_history(broker, settings)
            if policy["state"] == "research_required":
                await store.put(
                    "training_status",
                    {"state": "research_required", "policy": policy, "updated_at": utcnow().isoformat()},
                )
                await store.put("last_training_maintenance", utcnow().isoformat())
                return
            await store.put("training_status", {"state": "training", "updated_at": utcnow().isoformat()})
            bars = await asyncio.to_thread(read_history, settings)
            if policy.get("failed_holdout_end"):
                local = bars["timestamp"].dt.tz_convert("America/New_York")
                regular = (local.dt.hour * 60 + local.dt.minute - 570).between(0, 385)
                dates = sorted(local.loc[regular].dt.strftime("%Y-%m-%d").unique())
                if not dates:
                    raise ValueError("no regular-session bars")
                if dates[int(len(dates) * 0.85)] <= policy["failed_holdout_end"]:
                    await store.put(
                        "training_status",
                        {
                            "state": "waiting_fresh_holdout",
                            "excluded_through": policy["failed_holdout_end"],
                            "updated_at": utcnow().isoformat(),
                        },
                    )
                    await store.put("last_training_maintenance", utcnow().isoformat())
                    return
            loop = asyncio.get_running_loop()

            async def write_progress(value):
                await training_leader.execute("SELECT 1")
                await store.put("training_status", value)

            def progress(value):
                asyncio.run_coroutine_threadsafe(write_progress(value), loop).result(timeout=30)

            report = await asyncio.to_thread(train_and_evaluate, bars, settings, progress)
            policy = record_result(policy, report)
            await store.put("training_policy", policy)
            await store.put(
                "training_status",
                {
                    "state": "research_required"
                    if policy["state"] == "research_required"
                    else "accepted"
                    if report["accepted"]
                    else "rejected",
                    "updated_at": utcnow().isoformat(),
                    "report": report,
                },
            )
            await store.put("last_training_completed", utcnow().isoformat())
            await store.put("last_training_maintenance", utcnow().isoformat())
            await store.alert(
                f"training-{report['created_at']}",
                "모델 검증 완료: "
                + (
                    "검증 통과 모델 등록"
                    if report["accepted"]
                    else "통과 모델 없음; 기존 유효 모델 유지 또는 신규 매수 대기"
                ),
            )
            if policy["state"] == "research_required":
                await store.alert(
                    f"research-required-{report['created_at']}",
                    "모델 연구 재설계 필요: 같은 계약의 자동 학습을 보류합니다. 자료 수집과 기존 포지션 관리는 계속합니다. "
                    + f"연속 탈락 {policy['consecutive_rejections']}회; 원인 {policy['reason']}.",
                )
    except Exception as exc:
        await store.put(
            "training_status",
            {"state": "failed", "updated_at": utcnow().isoformat(), "error": type(exc).__name__},
        )
        await store.alert(
            f"training-error-{utcnow().date()}",
            "자료 수집/학습 작업 실패. 기존 유효 모델을 유지하고 다음 작업에서 복구합니다.",
        )
        raise
    finally:
        await broker.close()


async def worker(settings, store, stop):
    await load_policy(store)
    if not settings.credentials_present:
        await store.put(
            "training_status", {"state": "waiting_alpaca_keys", "updated_at": utcnow().isoformat()}
        )
    while not stop.is_set():
        last = await store.get("last_training_maintenance") or await store.get("last_training_completed")
        # First bootstrap immediately; subsequent tasks weekly, outside regular trading sessions.
        due = last is None or (utcnow() - datetime.fromisoformat(last)) > timedelta(days=7)
        if due and settings.credentials_present:
            broker = Alpaca(settings)
            try:
                clock = await broker.clock()
                if not clock["is_open"]:
                    await train_job(settings, store)
            except Exception as exc:
                event("training_worker_error", error=type(exc).__name__)
            finally:
                await broker.close()
        try:
            await asyncio.wait_for(stop.wait(), timeout=3600)
        except TimeoutError:
            pass


async def resume_research(store, decision, evaluation_plan):
    # Require a reviewable local record; this never changes risk controls or model pointers.
    path = Path(decision).resolve()
    docs = (Path.cwd() / "docs").resolve()
    if not path.is_relative_to(docs) or not path.is_file() or path.stat().st_size == 0:
        raise ValueError("decision must reference a nonempty document under docs/")
    if not evaluation_plan or len(evaluation_plan.strip()) < 20:
        raise ValueError("provide a concrete evaluation period plan (at least 20 characters)")
    async with store.leader("training-job"):
        previous = await load_policy(store)
        value = {
            "state": "active",
            "consecutive_rejections": 0,
            "limit": previous["limit"],
            "decision": str(path.relative_to(Path.cwd().resolve())),
            "evaluation_plan": evaluation_plan,
            "updated_at": utcnow().isoformat(),
        }
        if previous.get("failed_holdout_end"):
            value["failed_holdout_end"] = previous["failed_holdout_end"]
        await store.audit("research_resumed", previous=previous, policy=value)
        await store.put("training_policy", value)
    return value


async def dispatch(command: str, decision=None, evaluation_plan=None):
    settings = Settings()
    store = Store(settings)
    await store.open()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        if command == "engine":
            await Engine(settings, store).run(stop)
        elif command == "trainer":
            await worker(settings, store, stop)
        elif command == "train":
            await train_job(settings, store)
        elif command == "collect":
            if not settings.credentials_present:
                raise ValueError("provide Alpaca credentials in .secrets first")
            broker = Alpaca(settings)
            try:
                await collect_history(broker, settings)
            finally:
                await broker.close()
        elif command == "status":
            print(json.dumps(await store.get("engine_status", {}), ensure_ascii=False, indent=2))
        elif command == "resume-research":
            print(json.dumps(await resume_research(store, decision, evaluation_plan), ensure_ascii=False))
        elif command == "notify-test":
            await store.alert(
                f"notification-test-{utcnow().isoformat()}", "Stock Trader Discord 알림 연결 테스트입니다."
            )
            notifier = Notifier(settings, store)
            try:
                await notifier.flush()
            finally:
                await notifier.close()
    finally:
        await store.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["engine", "trainer", "train", "collect", "status", "notify-test", "resume-research"],
    )
    parser.add_argument("--decision")
    parser.add_argument("--evaluation-plan")
    args = parser.parse_args()
    if args.command == "resume-research" and (not args.decision or not args.evaluation_plan):
        parser.error("resume-research requires --decision and --evaluation-plan")
    configure_logging()
    try:
        asyncio.run(dispatch(args.command, args.decision, args.evaluation_plan))
    except Exception as exc:
        event("process_failed", error=type(exc).__name__)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
