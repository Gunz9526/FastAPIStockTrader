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
    새로운 주식 티커를 생성합니다.
    
    Parameters:
        - ticker_in: 티커 생성 정보 (symbol, name, sector, is_active)
    
    Returns:
        - 생성된 티커 정보
    
    Raises:
        - 400: 티커가 이미 존재하는 경우
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
    모든 주식 티커 목록을 조회합니다.
    
    Returns:
        - 전체 티커 목록 (활성/비활성 포함)
    """
    repo = StockRepository(db)
    return await repo.get_all_tickers()

@router.get("/{symbol}", response_model=StockTickerResponse)
async def read_ticker(
    symbol: str,
    db: AsyncSession = Depends(get_async_session)
):
    """
    특정 심볼의 티커 정보를 조회합니다.
    
    Parameters:
        - symbol: 주식 심볼 (예: AAPL, TSLA)
    
    Returns:
        - 티커 상세 정보
    
    Raises:
        - 404: 티커를 찾을 수 없는 경우
    """
    repo = StockRepository(db)
    ticker = await repo.get_ticker(symbol)
    if not ticker:
        raise HTTPException(status_code=404, detail="Ticker not found")
    return ticker
