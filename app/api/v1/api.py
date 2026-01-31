from fastapi import APIRouter, Depends

from app.api.v1.endpoints import alpaca, model, operations, rag, stocks
from app.core.security import get_api_key

api_router = APIRouter()
api_router.include_router(
    stocks.router,
    prefix="/stocks",
    tags=["stocks"],
    dependencies=[Depends(get_api_key)],
)
api_router.include_router(
    operations.router,
    prefix="/operations",
    tags=["operations"],
    dependencies=[Depends(get_api_key)],
)
api_router.include_router(
    rag.router,
    prefix="/rag",
    tags=["rag"],
    dependencies=[Depends(get_api_key)],
)
api_router.include_router(
    model.router,
    prefix="/model",
    tags=["model"],
    dependencies=[Depends(get_api_key)],
)
api_router.include_router(
    alpaca.router,
    prefix="/alpaca",
    tags=["alpaca"],
    dependencies=[Depends(get_api_key)],
)
