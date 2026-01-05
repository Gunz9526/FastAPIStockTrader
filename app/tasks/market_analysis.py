import logging
from app.worker import celery_app
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository
from datetime import datetime, timedelta
import pandas as pd

logger = logging.getLogger(__name__)

@celery_app.task(name="app.tasks.market_analysis.analyze_market")
def analyze_market():
    """
    Market-wide analysis task (SYNC VERSION).
    Calculates aggregate metrics across all active symbols.
    """
    logger.info("=" * 60)
    logger.info("MARKET ANALYSIS STARTED")
    logger.info("=" * 60)
    
    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)
        
        # Get active symbols
        symbols = repo.get_active_symbols()
        
        if not symbols:
            logger.warning("No active symbols for analysis")
            return
        
        logger.info(f"Analyzing {len(symbols)} symbols")
        
        # Time range: last 30 days
        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - timedelta(days=30)
        
        analysis_results = {
            'total_symbols': len(symbols),
            'analyzed_symbols': 0,
            'high_momentum': [],
            'low_volatility': [],
            'high_volume': [],
            'avg_return_pct': 0.0,
            'avg_volatility': 0.0
        }
        
        returns = []
        volatilities = []
        
        for symbol in symbols:
            try:
                # Get recent OHLCV data
                ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date)
                
                if len(ohlcv) < 10:
                    logger.debug(f"Insufficient data for {symbol}")
                    continue
                
                # Convert to DataFrame
                df = pd.DataFrame([{
                    'date_time': bar.date_time,
                    'close': bar.close,
                    'volume': bar.volume
                } for bar in ohlcv])
                df.set_index('date_time', inplace=True)
                df.sort_index(inplace=True)
                
                # Calculate metrics
                daily_returns = df['close'].pct_change().dropna()
                total_return = (df['close'].iloc[-1] / df['close'].iloc[0] - 1) * 100
                volatility = daily_returns.std() * (252 ** 0.5)  # Annualized
                avg_volume = df['volume'].mean()
                
                returns.append(total_return)
                volatilities.append(volatility)
                
                # Classify symbols
                if total_return > 5:  # >5% gain
                    analysis_results['high_momentum'].append(symbol)
                
                if volatility < 0.2:  # <20% annual volatility
                    analysis_results['low_volatility'].append(symbol)
                
                if avg_volume > 1_000_000:  # High liquidity
                    analysis_results['high_volume'].append(symbol)
                
                analysis_results['analyzed_symbols'] += 1
                
            except Exception as e:
                logger.error(f"Failed to analyze {symbol}: {e}")
                continue
        
        # Aggregate statistics
        if returns:
            analysis_results['avg_return_pct'] = round(sum(returns) / len(returns), 2)
        if volatilities:
            analysis_results['avg_volatility'] = round(sum(volatilities) / len(volatilities), 2)
        
        # Log results
        logger.info("=" * 60)
        logger.info("MARKET ANALYSIS RESULTS")
        logger.info("=" * 60)
        logger.info(f"Analyzed: {analysis_results['analyzed_symbols']}/{analysis_results['total_symbols']}")
        logger.info(f"Avg Return (30d): {analysis_results['avg_return_pct']}%")
        logger.info(f"Avg Volatility (annual): {analysis_results['avg_volatility']}")
        logger.info(f"High Momentum: {len(analysis_results['high_momentum'])} symbols")
        logger.info(f"Low Volatility: {len(analysis_results['low_volatility'])} symbols")
        logger.info(f"High Volume: {len(analysis_results['high_volume'])} symbols")
        
        if analysis_results['high_momentum']:
            logger.info(f"Top Gainers: {', '.join(analysis_results['high_momentum'][:5])}")
        
        session.commit()
        logger.info("=" * 60)
        logger.info("MARKET ANALYSIS COMPLETED")
        logger.info("=" * 60)
        
        return analysis_results
        
    except Exception as e:
        logger.error(f"Market analysis error: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()
