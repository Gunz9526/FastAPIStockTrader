from typing import List
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_async_session
from app.repositories.stock_repo import StockRepository
from app.domain.schemas.stock import StockTickerCreate, StockTickerResponse
from app.domain.models.stock import StockTicker

router = APIRouter()

@router.post("/", response_model=StockTickerResponse)
async def create_ticker(
    ticker_in: StockTickerCreate,
    db: AsyncSession = Depends(get_async_session)
):
    """
    Create a new stock ticker.
    """
    repo = StockRepository(db)
    existing = await repo.get_ticker(ticker_in.symbol)
    if existing:
        raise HTTPException(status_code=400, detail="Ticker already exists")
    
    return await repo.create_ticker(ticker_in)

@router.get("/", response_model=List[StockTickerResponse])
async def read_tickers(
    db: AsyncSession = Depends(get_async_session)
):
    """
    Retrieve all stock tickers.
    """
    repo = StockRepository(db)
    return await repo.get_all_tickers()

@router.get("/{symbol}", response_model=StockTickerResponse)
async def read_ticker(
    symbol: str,
    db: AsyncSession = Depends(get_async_session)
):
    """
    Get a specific ticker by symbol.
    """
    repo = StockRepository(db)
    ticker = await repo.get_ticker(symbol)
    if not ticker:
        raise HTTPException(status_code=404, detail="Ticker not found")
    return ticker
