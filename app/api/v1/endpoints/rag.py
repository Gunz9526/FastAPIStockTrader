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

# ==================== Position Report for RAG ====================

@router.get("/positions/report")
async def get_position_report_for_rag(
    days: int = Query(default=30, le=365, description="조회 기간 (일)"),
    db: AsyncSession = Depends(get_async_session)
):
    """
    RAG용 포지션 보고서 조회
    
    기능:
    - 매수/매도 포지션 이력 (PositionTracking 테이블)
    - 수익률, 보유 기간 분석
    - Regime별 포지션 통계
    - 신호 강도 및 성과 분석
    
    Returns:
        - summary: 전체 통계 (총 거래, 승률, 평균 수익률)
        - positions: 개별 포지션 상세 내역
        - by_regime: Regime별 분석
        - top_performers: 상위 수익 종목
        - worst_performers: 하위 수익 종목
    """
    try:
        from app.domain.models.stock import PositionTracking
        from sqlalchemy import and_, func
        
        cutoff_date = datetime.now() - timedelta(days=days)
        
        # 1. 종료된 포지션 조회 (exit_time이 있는 것만)
        stmt = select(PositionTracking).where(
            and_(
                PositionTracking.exit_time >= cutoff_date,
                PositionTracking.exit_time.isnot(None)
            )
        ).order_by(desc(PositionTracking.exit_time))
        
        result = await db.execute(stmt)
        positions = result.scalars().all()
        
        if not positions:
            return {
                "period_days": days,
                "summary": {
                    "total_positions": 0,
                    "win_rate": 0.0,
                    "avg_profit_pct": 0.0,
                    "total_pnl": 0.0
                },
                "positions": [],
                "by_regime": {},
                "top_performers": [],
                "worst_performers": []
            }
        
        # 2. 개별 포지션 분석
        position_list = []
        total_pnl = 0.0
        wins = 0
        
        for pos in positions:
            holding_duration = (pos.exit_time - pos.entry_time).total_seconds() / 60  # 분
            profit_pct = ((pos.exit_price - pos.entry_price) / pos.entry_price) * 100
            pnl = pos.pnl if pos.pnl else (pos.exit_price - pos.entry_price) * pos.quantity
            
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            
            position_list.append({
                "symbol": pos.symbol,
                "entry_time": pos.entry_time.isoformat(),
                "exit_time": pos.exit_time.isoformat(),
                "entry_price": pos.entry_price,
                "exit_price": pos.exit_price,
                "quantity": pos.quantity,
                "holding_duration_minutes": round(holding_duration, 2),
                "profit_pct": round(profit_pct, 4),
                "pnl": round(pnl, 2)
            })
        
        # 3. 요약 통계
        win_rate = (wins / len(positions)) * 100 if positions else 0.0
        avg_profit_pct = sum(p["profit_pct"] for p in position_list) / len(position_list) if position_list else 0.0
        
        # 4. 상위/하위 수익 종목
        sorted_by_pnl = sorted(position_list, key=lambda x: x["pnl"], reverse=True)
        top_performers = sorted_by_pnl[:5]
        worst_performers = sorted_by_pnl[-5:]
        
        # 5. Regime별 분석 (현재는 DB에 regime 저장 안함 - 향후 추가 가능)
        # 임시로 비워둔 dict 반환
        by_regime = {
            "note": "Regime 정보는 향후 PositionTracking 테이블에 regime 커럼 추가 시 제공"
        }
        
        return {
            "period_days": days,
            "summary": {
                "total_positions": len(positions),
                "win_rate": round(win_rate, 2),
                "avg_profit_pct": round(avg_profit_pct, 4),
                "total_pnl": round(total_pnl, 2)
            },
            "positions": position_list,
            "by_regime": by_regime,
            "top_performers": top_performers,
            "worst_performers": worst_performers
        }
        
    except Exception as e:
        logger.error("RAG 포지션 보고서 조회 오류: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

# ==================== Stock Recommendation for RAG ====================

@router.get("/recommendations")
async def get_stock_recommendations_for_rag(
    limit: int = Query(default=10, le=50, description="추천 종목 수"),
    db: AsyncSession = Depends(get_async_session)
):
    """
    RAG용 종목 추천 정보 조회
    
    기능:
    - ML 예측 신호 (현재 모델 기반)
    - Sentiment 점수 (Redis 캐시)
    - Fundamentals (PE, PB, ROE)
    - 상관관계 행렬
    - 섹터 분산 정보
    
    Returns:
        - recommendations: 추천 종목 목록
        - market_regime: 현재 시장 레짐
        - correlation_matrix: 상관관계 행렬
        - sector_distribution: 섹터별 분포
    """
    try:
        from app.core.database import SessionLocal
        from app.repositories.stock_repo_sync import SyncStockRepository
        from app.ml.predictor import PredictorService
        from app.ml.features import FeatureEngineer
        from app.ml.models import CLASS_NAMES
        from app.services.regime import RegimeDetector, MarketRegime
        from app.services.sentiment_analyzer import get_sentiment_analyzer
        from app.services.fundamental_provider import get_fundamental_provider
        import pandas as pd
        
        # 1. 활성 심볼 가져오기
        with SessionLocal() as sync_db:
            repo = SyncStockRepository(sync_db)
            symbols = repo.get_active_symbols()
        
        if not symbols:
            raise HTTPException(status_code=404, detail="활성 심볼 없음")
        
        # 2. 시장 레짐 감지 (SPY 기반)
        regime_detector = RegimeDetector()
        current_regime = MarketRegime.SIDEWAYS_CALM  # 기본값
        
        try:
            with SessionLocal() as sync_db:
                repo = SyncStockRepository(sync_db)
                feature_engineer = FeatureEngineer()
                
                end_date = pd.Timestamp.now(tz='UTC')
                start_date = end_date - pd.Timedelta(days=90)
                
                spy_data = repo.get_ohlcv_range('SPY', start_date, end_date, timeframe='1d')
                
                if len(spy_data) >= 100:
                    spy_df = pd.DataFrame([{
                        'date_time': bar.date_time,
                        'open': bar.open,
                        'high': bar.high,
                        'low': bar.low,
                        'close': bar.close,
                        'volume': bar.volume,
                        'symbol': 'SPY'
                    } for bar in spy_data])
                    spy_df.set_index('date_time', inplace=True)
                    spy_features = feature_engineer.create_features(spy_df)
                    
                    if not spy_features.empty:
                        current_regime = regime_detector.detect_regime(spy_features)
        except Exception as e:
            logger.warning("레짐 감지 실패: %s", e)
        
        # 3. 종목별 분석
        predictor = PredictorService()
        sentiment_analyzer = get_sentiment_analyzer()
        fundamental_provider = get_fundamental_provider()
        feature_engineer = FeatureEngineer()
        
        recommendations = []
        
        with SessionLocal() as sync_db:
            repo = SyncStockRepository(sync_db)
            
            for symbol in symbols[:limit]:
                try:
                    # ML 예측
                    end_date = pd.Timestamp.now(tz='UTC')
                    start_date = end_date - pd.Timedelta(days=30)
                    
                    ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe='1d')
                    if len(ohlcv) < 50:
                        continue
                    
                    df = pd.DataFrame([{
                        'date_time': bar.date_time,
                        'open': bar.open,
                        'high': bar.high,
                        'low': bar.low,
                        'close': bar.close,
                        'volume': bar.volume,
                        'symbol': symbol
                    } for bar in ohlcv])
                    df.set_index('date_time', inplace=True)
                    
                    features_df = feature_engineer.create_features(df)
                    if features_df.empty:
                        continue
                    
                    latest_features = features_df.iloc[[-1]]
                    regime_suffix = current_regime.value if current_regime else 'sideways_calm'
                    X_norm = feature_engineer.extract_feature_vector(
                        latest_features, fit_scaler=False, feature_set="base",
                        scaler_suffix=regime_suffix
                    )
                    
                    predicted_class, confidence, probabilities = predictor.predict_class(
                        X_norm, regime=current_regime
                    )
                    class_name = CLASS_NAMES[predicted_class]
                    current_price = df['close'].iloc[-1]
                    
                    # Sentiment
                    sentiment_score = sentiment_analyzer.get_cached_sentiment(symbol)
                    if sentiment_score is None:
                        sentiment_score = 0.0
                    
                    # Fundamentals
                    fundamentals = fundamental_provider.get_fundamentals(symbol)
                    
                    # 종목 정보 가져오기
                    ticker_info = repo.get_ticker(symbol)
                    
                    # Classification-based recommendation score:
                    # UP(2) boosts score, DOWN(0) lowers, confidence scales
                    direction_score = {2: 1.0, 1: 0.0, 0: -1.0}.get(predicted_class, 0.0)
                    fundamentals_bonus = (
                        0.10 if fundamentals and fundamentals.get('pe_ratio', 40) < 30
                        else 0.0
                    )
                    recommendation_score = (
                        direction_score * confidence * 0.75
                        + sentiment_score * 0.15
                        + fundamentals_bonus
                    )

                    recommendations.append({
                        "symbol": symbol,
                        "name": ticker_info.name if ticker_info else symbol,
                        "sector": ticker_info.sector if ticker_info else "Unknown",
                        "current_price": round(current_price, 2),
                        "ml_class": class_name,
                        "ml_confidence": round(confidence, 4),
                        "ml_probabilities": {
                            k: round(v, 4) for k, v in probabilities.items()
                        },
                        "sentiment_score": round(sentiment_score, 3),
                        "fundamentals": {
                            "pe_ratio": fundamentals.get('pe_ratio') if fundamentals else None,
                            "pb_ratio": fundamentals.get('pb_ratio') if fundamentals else None,
                            "roe": fundamentals.get('roe') if fundamentals else None,
                            "market_cap": fundamentals.get('market_cap') if fundamentals else None
                        },
                        "recommendation_score": round(recommendation_score, 5)
                    })
                    
                except Exception as e:
                    logger.debug("%s 분석 실패: %s", symbol, e)
                    continue
        
        # 4. 상관관계 행렬 (간략하게 표시)
        correlation_matrix = {"note": "상관관계 행렬은 portfolio_optimizer.calculate_correlation_matrix()로 계산 가능"}
        
        # 5. 섹터 분포
        sector_distribution = {}
        for rec in recommendations:
            sector = rec.get('sector', 'Unknown')
            sector_distribution[sector] = sector_distribution.get(sector, 0) + 1
        
        # 6. 추천 점수로 정렬
        recommendations.sort(key=lambda x: x['recommendation_score'], reverse=True)
        
        return {
            "market_regime": current_regime.value,
            "total_symbols_analyzed": len(recommendations),
            "recommendations": recommendations,
            "correlation_matrix": correlation_matrix,
            "sector_distribution": sector_distribution
        }
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error("RAG 종목 추천 조회 오류: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

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
