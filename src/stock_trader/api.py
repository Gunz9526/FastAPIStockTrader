import secrets
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from stock_trader.clock import parse_time, utcnow
from stock_trader.config import Settings
from stock_trader.storage import Store


class Command(BaseModel):
    command: str


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
            "risk": await database.get("risk_account", {}),
            "drawdown_latch": await database.get("drawdown_latch"),
            "manual_pause": await database.get("manual_pause", not settings.auto_start),
            "signals": await database.get("latest_signals", []),
            "settings": settings.public_contract(),
        }

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

    @app.post("/controls", dependencies=[Depends(authorized)], status_code=202)
    async def control(command: Command, request: Request):
        if command.command not in {"pause", "resume", "flatten", "reset-risk"}:
            raise HTTPException(422, "지원하지 않는 명령입니다.")
        row = await database.one("INSERT INTO controls(command) VALUES(%s) RETURNING id", (command.command,))
        await database.audit("control_requested", command=command.command, control_id=row["id"])
        return {"id": row["id"], "state": "pending"}

    return app


app = create_app()
