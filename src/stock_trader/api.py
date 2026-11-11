import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.etf_ledger import number
from stock_trader.storage import Store


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    command: str
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,64}$")
    reason: str | None = Field(default=None, min_length=8, max_length=500)
    expected_snapshot_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    qty: str | None = Field(default=None, pattern=r"^[0-9]{1,9}(\.[0-9]{1,9})?$")


def create_app(settings: Settings | None = None, store=None):
    settings = settings or Settings()
    database = store or Store(settings)

    @asynccontextmanager
    async def lifespan(app):
        await database.open()
        app.state.store = database
        yield
        await database.close()

    app = FastAPI(
        title="Stock Trader",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    auth = HTTPBearer(auto_error=False)

    async def authorized(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(auth)]):
        token = settings.api_token.get_secret_value()
        if (
            not token
            or not credentials
            or credentials.scheme.lower() != "bearer"
            or not secrets.compare_digest(credentials.credentials, token)
        ):
            raise HTTPException(401, "인증이 필요합니다.")

    @app.get("/health/live")
    async def live():
        return {"alive": True}

    @app.get("/health/ready")
    async def ready():
        try:
            await database.one("SELECT 1")
            status = await database.get("engine_status", {})
            heartbeat = status.get("heartbeat")
            if not heartbeat or (utcnow() - parse_time(heartbeat)).total_seconds() > 90:
                raise HTTPException(503, "엔진 응답 대기")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(503, "저장소 응답 대기") from None
        return {"ready": True, "trading_ready": status["state"] == "monitoring", "state": status["state"]}

    @app.get("/status", dependencies=[Depends(authorized)])
    async def status():
        return {
            "engine": await database.get("engine_status", {}),
            "training": await database.get("training_status", {}),
            "training_policy": await database.get("training_policy", {}),
            "market_observation": await database.get("market_observation", {}),
            "market_quality": await database.get("market_quality", {}),
            "etf_shadow": await database.get("etf_shadow", {}),
            "etf_account": await database.get("etf_account", {}),
            "etf_paper_probe": await database.get("etf_paper_probe", {}),
            "etf_pilot": await database.get("etf_pilot", {}),
            "etf_auto": await database.get("etf_auto", {}),
            "etf_controls": await database.get("etf_controls", {}),
            "performance": await database.get("performance_status", {}),
            "operational_observation": await database.get("operational_observation", {}),
            "equity_research": await database.get("equity_shadow_0086", {}),
            "equity_forward": await database.get("equity_forward_0090", {}),
            "operations": await database.get("operations", {}),
            "risk": await database.get("risk_account", {}),
            "drawdown_latch": await database.get("drawdown_latch"),
            "manual_pause": await database.get("manual_pause", not settings.auto_start),
            "signals": await database.get("latest_signals", []),
            "settings": settings.public_contract(),
        }

    @app.get("/health/operations", dependencies=[Depends(authorized)])
    async def operational_health():
        from stock_trader.health import load_health

        result = await load_health(database)
        return JSONResponse(
            result,
            status_code=503 if result["state"] == "blocked" else 200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/progress", dependencies=[Depends(authorized)])
    async def work_progress():
        from stock_trader.work_progress import load_progress

        result = await load_progress(database, settings.data_dir)
        return JSONResponse(
            result,
            status_code=503 if result["state"] == "review_required" else 200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/research/equity/accounting", dependencies=[Depends(authorized)])
    async def equity_forward():
        result = await database.get(
            "equity_forward_0090",
            {"state": "not_registered", "orders_submitted": 0, "performance_calculated": False},
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/research/equity", dependencies=[Depends(authorized)])
    async def equity_research():
        # Read stored observations only; keep research imports and broker calls
        # out of the API request path and the existing trading health contract.
        result = await database.get(
            "equity_shadow_0086",
            {"state": "not_registered", "orders_submitted": 0, "performance_calculated": False},
        )
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/orders", dependencies=[Depends(authorized)])
    async def orders():
        return await database.all("SELECT * FROM intents ORDER BY created_at DESC LIMIT 100")

    @app.get("/alerts", dependencies=[Depends(authorized)])
    async def alerts():
        return await database.all(
            "SELECT key,content,state,attempts,created_at,sent_at FROM alerts "
            "ORDER BY created_at DESC LIMIT 100"
        )

    @app.get("/controls/{control_id}", dependencies=[Depends(authorized)])
    async def control_status(control_id: int):
        result = await database.one("SELECT * FROM controls WHERE id=%s", (control_id,))
        if not result:
            raise HTTPException(404, "제어 요청이 없습니다.")
        return result

    @app.get("/control-preview", dependencies=[Depends(authorized)])
    async def control_preview():
        from stock_trader.etf_controls import ControlBlocked, preview

        try:
            return await preview(database)
        except ControlBlocked as exc:
            raise HTTPException(503, str(exc)) from None

    @app.post("/controls", dependencies=[Depends(authorized)], status_code=202)
    async def control(command: Command, request: Request):
        if command.command not in {"pause", "resume", "flatten", "reset-risk", "reduce"}:
            raise HTTPException(422, "지원하지 않는 명령입니다.")
        if command.command in {"flatten", "reset-risk", "reduce"}:
            if not (command.request_id and command.reason and command.expected_snapshot_sha256):
                raise HTTPException(422, "검토 사유·요청 ID·예상 상태가 필요합니다.")
            if command.command == "reduce" and (not command.qty or number(command.qty) <= 0):
                raise HTTPException(422, "축소 수량이 필요합니다.")
            if command.command != "reduce" and command.qty is not None:
                raise HTTPException(422, "이 명령은 수량을 받지 않습니다.")
        if command.request_id:
            payload = command.model_dump(exclude={"request_id"}, exclude_none=True)
            async with database.pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "INSERT INTO controls(command,payload,request_key) VALUES(%s,%s,%s) "
                        "ON CONFLICT(request_key) DO NOTHING RETURNING id,state",
                        (command.command, Jsonb(payload), command.request_id),
                    )
                ).fetchone()
                if row:
                    await conn.execute(
                        "INSERT INTO audit(kind,payload) VALUES('control_requested',%s)",
                        (
                            Jsonb(
                                {
                                    "control_id": row["id"],
                                    "command": command.command,
                                    "request_id": command.request_id,
                                }
                            ),
                        ),
                    )
                else:
                    row = await (
                        await conn.execute(
                            "SELECT id,state,payload FROM controls WHERE request_key=%s",
                            (command.request_id,),
                        )
                    ).fetchone()
                    if row["payload"] != payload:
                        raise HTTPException(409, "같은 요청 ID의 내용이 다릅니다.")
            return {"id": row["id"], "state": row["state"]}
        row = await database.one("INSERT INTO controls(command) VALUES(%s) RETURNING id", (command.command,))
        await database.audit("control_requested", command=command.command, control_id=row["id"])
        return {"id": row["id"], "state": "pending"}

    return app


app = create_app()
