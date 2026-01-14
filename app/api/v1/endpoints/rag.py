from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from typing import List, Optional
from datetime import datetime, timedelta
import logging
import json
import os
from pathlib import Path

from app.core.database import get_async_session
from app.core.security import get_api_key
from app.domain.models.stock import (
    StockOHLCV, StockFundamentals, PortfolioStatus, 
    Position, TradeLog, StockTicker
)

router = APIRouter()
logger = logging.getLogger(__name__)

# ==================== OHLCV Data ====================

@router.get("/ohlcv/{symbol}")
async def get_ohlcv_for_rag(
    symbol: str,
    days: int = Query(default=7, le=365),
    db: AsyncSession = Depends(get_async_session)
):
    """
    Get historical OHLCV data for RAG service.
    Returns price history for LLM context.
    """
    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        stmt = select(StockOHLCV).where(
            StockOHLCV.symbol == symbol,
            StockOHLCV.date_time >= start_date
        ).order_by(desc(StockOHLCV.date_time)).limit(500)
        
        result = await db.execute(stmt)
        bars = result.scalars().all()
        
        if not bars:
            raise HTTPException(status_code=404, detail=f"No data for {symbol}")
        
        return {
            "symbol": symbol,
            "period_days": days,
            "bars_count": len(bars),
            "data": [
                {
                    "date": bar.date_time.isoformat(),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume
                }
                for bar in bars
            ],
            "summary": {
                "latest_price": bars[0].close if bars else None,
                "highest": max(b.high for b in bars) if bars else None,
                "lowest": min(b.low for b in bars) if bars else None,
                "total_volume": sum(b.volume for b in bars) if bars else None
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching OHLCV for RAG: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Fundamentals ====================

@router.get("/fundamentals/{symbol}")
async def get_fundamentals_for_rag(
    symbol: str,
    db: AsyncSession = Depends(get_async_session)
):
    """
    Get fundamental data for RAG service.
    Returns PER, PBR, ROE, market cap, sector.
    """
    try:
        # Get latest fundamentals
        stmt = select(StockFundamentals).where(
            StockFundamentals.symbol == symbol
        ).order_by(desc(StockFundamentals.date)).limit(1)
        
        result = await db.execute(stmt)
        fund = result.scalar_one_or_none()
        
        if not fund:
            raise HTTPException(status_code=404, detail=f"No fundamentals for {symbol}")
        
        return {
            "symbol": symbol,
            "date": fund.date.isoformat(),
            "per": fund.per,
            "pbr": fund.pbr,
            "roe": fund.roe,
            "market_cap": fund.market_cap,
            "sector": fund.sector,
            "valuation_summary": {
                "undervalued": fund.per is not None and fund.per < 15,
                "growth_stock": fund.roe is not None and fund.roe > 0.15
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching fundamentals for RAG: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Portfolio ====================

@router.get("/portfolio/{user_id}")
async def get_portfolio_for_rag(
    user_id: str,
    db: AsyncSession = Depends(get_async_session)
):
    """
    Get user portfolio status for RAG service.
    Returns holdings with P&L information.
    """
    try:
        stmt = select(PortfolioStatus).where(
            PortfolioStatus.user_id == user_id
        )
        
        result = await db.execute(stmt)
        holdings = result.scalars().all()
        
        if not holdings:
            return {
                "user_id": user_id,
                "holdings": [],
                "total_value": 0,
                "message": "No holdings found"
            }
        
        portfolio_data = []
        total_value = 0
        total_pl = 0
        
        for holding in holdings:
            current_value = (holding.current_price or 0) * holding.quantity
            cost_basis = holding.avg_price * holding.quantity
            unrealized_pl = current_value - cost_basis
            pl_pct = (unrealized_pl / cost_basis * 100) if cost_basis > 0 else 0
            
            portfolio_data.append({
                "symbol": holding.symbol,
                "quantity": holding.quantity,
                "avg_price": holding.avg_price,
                "current_price": holding.current_price,
                "cost_basis": cost_basis,
                "current_value": current_value,
                "unrealized_pl": unrealized_pl,
                "pl_percentage": round(pl_pct, 2)
            })
            
            total_value += current_value
            total_pl += unrealized_pl
        
        return {
            "user_id": user_id,
            "holdings_count": len(holdings),
            "total_value": round(total_value, 2),
            "total_unrealized_pl": round(total_pl, 2),
            "holdings": portfolio_data
        }
        
    except Exception as e:
        logger.error(f"Error fetching portfolio for RAG: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Trade Decisions (Logs) ====================

@router.get("/trade-decisions/{symbol}")
async def get_trade_decisions_for_rag(
    symbol: str,
    days: int = Query(default=7, le=30)
):
    """
    Get recent trade decision logs for RAG service.
    Returns why trading bot bought/sold this symbol.
    """
    try:
        logs_dir = Path("logs/trade_decisions")
        
        if not logs_dir.exists():
            return {
                "symbol": symbol,
                "decisions": [],
                "message": "No trade logs found"
            }
        
        cutoff_date = datetime.now() - timedelta(days=days)
        decisions = []
        
        # Read JSON log files
        for log_file in logs_dir.glob(f"*_{symbol}_decision.json"):
            try:
                with open(log_file, 'r') as f:
                    decision = json.load(f)
                    
                    # Check date
                    decision_date = datetime.fromisoformat(decision.get("timestamp", "1970-01-01"))
                    if decision_date >= cutoff_date:
                        decisions.append(decision)
                        
            except Exception as e:
                logger.warning(f"Failed to read {log_file}: {e}")
        
        # Sort by timestamp descending
        decisions.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        return {
            "symbol": symbol,
            "period_days": days,
            "decisions_count": len(decisions),
            "decisions": decisions
        }
        
    except Exception as e:
        logger.error(f"Error fetching trade decisions for RAG: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Positions ====================

@router.get("/positions")
async def get_current_positions_for_rag(
    status: str | None = Query(default="OPEN"),
    db: AsyncSession = Depends(get_async_session)
):
    """
    Get current trading positions for RAG service.
    Returns open/closed positions with P&L.
    """
    try:
        stmt = select(Position)
        
        if status:
            stmt = stmt.where(Position.status == status.upper())
        
        stmt = stmt.order_by(desc(Position.entry_time))
        
        result = await db.execute(stmt)
        positions = result.scalars().all()
        
        return {
            "status_filter": status,
            "positions_count": len(positions),
            "positions": [
                {
                    "symbol": pos.symbol,
                    "status": pos.status,
                    "entry_price": pos.entry_price,
                    "entry_time": pos.entry_time.isoformat(),
                    "current_qty": pos.current_qty,
                    "initial_qty": pos.initial_qty,
                    "current_price": pos.current_price,
                    "stop_loss": pos.stop_loss_price,
                    "take_profit": pos.take_profit_price,
                    "unrealized_pl": pos.unrealized_pl,
                    "realized_pl": pos.realized_pl,
                    "exit_price": pos.exit_price,
                    "exit_time": pos.exit_time.isoformat() if pos.exit_time else None
                }
                for pos in positions
            ]
        }
        
    except Exception as e:
        logger.error(f"Error fetching positions for RAG: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Strategies ====================

@router.get("/strategies")
async def get_strategies_info_for_rag():
    """
    Get information about current trading strategies.
    Returns strategy names and descriptions for RAG to explain.
    """
    return {
        "strategies": [
            {
                "name": "Momentum",
                "description": "Follows trends using SMA crossovers and MACD",
                "best_for": "Strong trending markets (ADX > 25)",
                "signals": ["Golden Cross (SMA20 > SMA50)", "MACD bullish crossover"]
            },
            {
                "name": "MeanReversion",
                "description": "Buys oversold and sells overbought using RSI and Bollinger Bands",
                "best_for": "Range-bound markets (ADX < 20)",
                "signals": ["RSI < 30 (oversold)", "Price near lower Bollinger Band"]
            },
            {
                "name": "Breakout",
                "description": "Catches strong movements after consolidation",
                "best_for": "High volume breakouts",
                "signals": ["Price breaks 20-day high/low", "Volume > 1.5x average"]
            },
            {
                "name": "MLEnsemble",
                "description": "Machine learning ensemble (CatBoost + LightGBM + XGBoost)",
                "best_for": "All market conditions",
                "signals": ["Model prediction > 0.7 (BUY)", "Model prediction < 0.3 (SELL)"]
            }
        ],
        "voting_system": {
            "description": "All 4 strategies vote on each symbol",
            "consensus_required": "50% agreement (2 out of 4)",
            "final_decision": "Weighted average of signal strengths"
        }
    }

# ==================== Trade History ====================

@router.get("/trade-history")
async def get_trade_history_for_rag(
    symbol: str | None = None,
    days: int = Query(default=30, le=365),
    db: AsyncSession = Depends(get_async_session)
):
    """
    Get trade execution history for RAG service.
    Returns audit trail of all trades.
    """
    try:
        cutoff_date = datetime.now() - timedelta(days=days)
        
        stmt = select(TradeLog).where(
            TradeLog.execution_time >= cutoff_date
        )
        
        if symbol:
            stmt = stmt.where(TradeLog.symbol == symbol)
        
        stmt = stmt.order_by(desc(TradeLog.execution_time))
        
        result = await db.execute(stmt)
        trades = result.scalars().all()
        
        return {
            "symbol_filter": symbol,
            "period_days": days,
            "trades_count": len(trades),
            "total_realized_pl": sum(t.realized_pl or 0 for t in trades),
            "trades": [
                {
                    "symbol": trade.symbol,
                    "action": trade.action,
                    "quantity": trade.quantity,
                    "price": trade.price,
                    "execution_time": trade.execution_time.isoformat(),
                    "strategy": trade.strategy_name,
                    "signal_strength": trade.signal_strength,
                    "realized_pl": trade.realized_pl,
                    "order_id": trade.order_id
                }
                for trade in trades
            ]
        }
        
    except Exception as e:
        logger.error(f"Error fetching trade history for RAG: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/available-symbols")
async def get_available_symbols(
    db: AsyncSession = Depends(get_async_session),
    status: bool = Query(default=True)
    ):
    """
    Get list of available stock symbols for trading.
    """
    stmt = select(StockTicker).where(StockTicker.is_active == status).order_by(StockTicker.symbol)
    result = await db.execute(stmt)
    tickers = result.scalars().all()

    return {
        "status_filter": status,
        "symbols_count": len(tickers),
        "symbols": [ticker.symbol for ticker in tickers]
    }
