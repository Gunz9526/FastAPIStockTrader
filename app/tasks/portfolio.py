"""
Portfolio Management Celery Tasks (Phase I.2)

Automated daily tasks for:
- Portfolio parameter updates (correlation, VaR, Kelly)
- Portfolio rebalancing (MPT optimization)
"""

import logging
from app.worker import celery_app
from app.core.database import SessionLocal
from app.services.portfolio_optimizer import PortfolioOptimizer
from app.services.portfolio_rebalancer import PortfolioRebalancer
from app.repositories.portfolio_repo import PortfolioRepository
import alpaca_trade_api as tradeapi
from app.core.config import settings

logger = logging.getLogger(__name__)

# Symbol universe (can be expanded)
PORTFOLIO_SYMBOLS = [
    'AAPL', 'MSFT', 'GOOGL', 'NVDA', 'TSLA',
    'META', 'AMZN', 'AMD', 'NFLX', 'SPY'
]


@celery_app.task(name="app.tasks.portfolio.update_portfolio_parameters")
def update_portfolio_parameters():
    """
    Daily parameter update task (00:00 ET).
    
    Updates:
    - Correlation matrix (14-day rolling)
    - VaR (95% confidence)
    - Kelly Criterion sizes
    
    Auto-switches from backtest to live data when sufficient trades exist.
    """
    logger.info("Starting daily portfolio parameter update")
    
    session = SessionLocal()
    try:
        # Initialize services
        portfolio_repo = PortfolioRepository(session)
        optimizer = PortfolioOptimizer(lookback_days=14, min_live_trades=50)
        
        # Get portfolio value
        api = tradeapi.REST(
            settings.ALPACA_API_KEY,
            settings.ALPACA_SECRET_KEY,
            settings.ALPACA_TRADING_URL,
            api_version='v2'
        )
        account = api.get_account()
        portfolio_value = float(account.portfolio_value)
        
        logger.info(f"Portfolio value: ${portfolio_value:,.2f}")
        
        # 1. Update correlation matrix
        corr_matrix = optimizer.calculate_correlation_matrix(
            portfolio_repo,
            PORTFOLIO_SYMBOLS,
            use_live_data=True
        )
        logger.info(f"Correlation matrix updated ({corr_matrix.shape})")
        
        # 2. Update VaR
        var = optimizer.calculate_var(
            portfolio_repo,
            portfolio_value,
            confidence=0.95,
            use_live_data=True
        )
        logger.info(f"VaR (95%) updated: ${var:,.2f}")
        
        # 3. Update Kelly sizes
        for symbol in PORTFOLIO_SYMBOLS:
            kelly = optimizer.kelly_criterion(
                portfolio_repo,
                symbol,
                use_live_data=True
            )
            logger.info(f"{symbol} Kelly: {kelly:.2%}")
        
        logger.info("Portfolio parameters updated successfully")
        
    except Exception as e:
        logger.error(f"❌ Parameter update failed: {e}", exc_info=True)
    finally:
        session.close()


@celery_app.task(name="app.tasks.portfolio.rebalance_portfolio")
def rebalance_portfolio(force: bool = False):
    """
    Daily portfolio rebalancing task (15:45 ET, 15 min before close).
    
    Process:
    1. Calculate optimal weights (MPT)
    2. Check drift from current weights
    3. Rebalance if drift > 5% (or force=True)
    
    Args:
        force: If True, rebalance regardless of drift
    """
    logger.info("Starting daily portfolio rebalancing")
    
    session = SessionLocal()
    try:
        # Initialize services
        portfolio_repo = PortfolioRepository(session)
        optimizer = PortfolioOptimizer(lookback_days=14)
        
        api = tradeapi.REST(
            settings.ALPACA_API_KEY,
            settings.ALPACA_SECRET_KEY,
            settings.ALPACA_TRADING_URL,
            api_version='v2'
        )
        
        rebalancer = PortfolioRebalancer(api, portfolio_repo, optimizer)
        
        # Execute rebalancing
        rebalancer.rebalance(PORTFOLIO_SYMBOLS, force=force)
        
        logger.info("Portfolio rebalancing complete")
        
    except Exception as e:
        logger.error(f"❌ Rebalancing failed: {e}", exc_info=True)
    finally:
        session.close()
