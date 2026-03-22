from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

import logging
import warnings
warnings.filterwarnings("ignore", message="Timestamp.utcnow is deprecated")
warnings.filterwarnings("ignore", category=FutureWarning, module="yfinance")

logger = logging.getLogger(__name__)

# Handle Pydantic types and fallbacks explicitly
broker_url = str(settings.CELERY_BROKER_URL) if settings.CELERY_BROKER_URL else str(settings.REDIS_URL)
result_backend = str(settings.CELERY_RESULT_BACKEND) if settings.CELERY_RESULT_BACKEND else str(settings.REDIS_URL)

logger.debug("REDIS_URL = %s", settings.REDIS_URL)
logger.debug("BROKER_URL = %s", broker_url)

celery_app = Celery(
    "worker",
    broker=broker_url,
    backend=result_backend,
    include=[
        "app.tasks.training",
        "app.tasks.trading",
        "app.tasks.market_analysis",
        "app.tasks.data_tasks",
        "app.tasks.realtime_data",
        "app.tasks.portfolio",
        "app.tasks.sentiment",
        "app.tasks.vix_data",
    ]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/New_York",
    enable_utc=False,
    broker_connection_retry_on_startup=True,
    # Queue separation for priority-based task routing
    task_default_queue="data",
    task_queues={
        "trading": {"exchange": "trading", "routing_key": "trading"},
        "data": {"exchange": "data", "routing_key": "data"},
        "training": {"exchange": "training", "routing_key": "training"},
    },
    task_routes={
        # HIGH PRIORITY: Real-time trading operations (< 1s latency tolerance)
        "app.tasks.trading.execute_market_scan": {"queue": "trading"},
        "app.tasks.trading.update_trailing_stops": {"queue": "trading"},
        "app.tasks.trading.generate_daily_signals": {"queue": "trading"},
        "app.tasks.trading.execute_intraday_entries": {"queue": "trading"},  # Phase L.2c
        "app.tasks.trading.send_end_of_day_summary": {"queue": "trading"},  # Phase R.3
        "app.tasks.portfolio.rebalance_portfolio": {"queue": "trading"},
        "app.tasks.portfolio.update_portfolio_parameters": {"queue": "trading"},
        # MEDIUM PRIORITY: Data collection (< 60s latency tolerance)
        "app.tasks.realtime_data.collect_daily_ohlcv": {"queue": "data"},
        "app.tasks.realtime_data.collect_15min_ohlcv": {"queue": "data"},  # Phase L.2a
        "app.tasks.sentiment.update_sentiment_scores": {"queue": "data"},
        "app.tasks.sentiment.clear_stale_sentiment_cache": {"queue": "data"},
        "app.tasks.vix_data.collect_vix_data": {"queue": "data"},
        "app.tasks.data_tasks.collect_fundamentals": {"queue": "data"},
        "app.tasks.market_analysis.analyze_market": {"queue": "data"},
        "app.tasks.market_analysis.compute_momentum_scores": {"queue": "data"},
        # LOW PRIORITY: Long-running training jobs (no latency requirement)
        "app.tasks.training.train_models": {"queue": "training"},
        "app.tasks.training.tune_models": {"queue": "training"},
        "app.tasks.training.analyze_feature_importance": {"queue": "training"},
    },
)


celery_app.conf.beat_schedule = {
    # PreMarket Analysis (8:30 AM EST, Monday-Friday)
    "premarket_analysis": {
        "task": "app.tasks.market_analysis.analyze_market",
        "schedule": crontab(minute="30", hour="8", day_of_week="1-5"),
    },

    # Market Scan (Once daily after open, 10:00 AM EST)
    "market_scan_trading_hours": {
        "task": "app.tasks.trading.execute_market_scan",
        "schedule": crontab(minute="0", hour="10", day_of_week="1-5"),
    },

    # Trailing Stop Updates (Twice daily: after open + before close)
    "update_trailing_stops": {
        "task": "app.tasks.trading.update_trailing_stops",
        "schedule": crontab(minute="0", hour="10,15", day_of_week="1-5"),
    },

    # Daily OHLCV Collection (Post-market, 5:00 PM EST)
    "collect_daily_ohlcv": {
        "task": "app.tasks.realtime_data.collect_daily_ohlcv",
        "schedule": crontab(minute="0", hour="17", day_of_week="1-5"),
    },

    # Phase L.2a: 15min OHLCV Collection (Market hours, every 15min)
    # Runs 9:45, 10:00, ..., 15:45 ET (Mon-Fri)
    # Feature flag DUAL_TIMEFRAME_ENABLED controls actual execution inside the task
    "collect_15min_ohlcv": {
        "task": "app.tasks.realtime_data.collect_15min_ohlcv",
        "schedule": crontab(minute="*/15", hour="9-15", day_of_week="1-5"),
    },

    # Phase L.2c: Intraday Entry/Exit Execution (Market hours, every 15min)
    # Runs at same cadence as 15min OHLCV collection
    # Feature flag DUAL_TIMEFRAME_ENABLED + market hours guard inside task
    "execute_intraday_entries": {
        "task": "app.tasks.trading.execute_intraday_entries",
        "schedule": crontab(minute="*/15", hour="9-15", day_of_week="1-5"),
    },

    # Daily ML Signal Generation (Post-market, 5:30 PM EST)
    # Runs after collect_daily_ohlcv to use fresh data
    "generate_daily_signals": {
        "task": "app.tasks.trading.generate_daily_signals",
        "schedule": crontab(minute="30", hour="17", day_of_week="1-5"),
    },

    # Cross-Sectional Momentum Scoring (Post-market, 5:15 PM EST)
    # Runs after OHLCV collection (17:00), before daily signals (17:30)
    "compute_momentum_scores": {
        "task": "app.tasks.market_analysis.compute_momentum_scores",
        "schedule": crontab(minute="15", hour="17", day_of_week="1-5"),
    },

    # Daily Portfolio Parameter Update (Midnight EST, every day)
    # Auto-calculates: correlation matrix, VaR, Kelly sizes
    # Uses live data if available (50+ trades), else backtest data
    "update_portfolio_parameters": {
        "task": "app.tasks.portfolio.update_portfolio_parameters",
        "schedule": crontab(minute="0", hour="0", day_of_week="1-5"),
    },

    # Daily Portfolio Rebalancing (3:45 PM EST, 15 min before close)
    # MPT optimization, only rebalances if drift > 5%
    "rebalance_portfolio": {
        "task": "app.tasks.portfolio.rebalance_portfolio",
        "schedule": crontab(minute="45", hour="15", day_of_week="1-5"),
    },

    # End-of-Day Discord Summary (4:05 PM EST, 5 min after close)
    # Sends portfolio value, daily P&L, positions, top/worst performer
    "send_end_of_day_summary": {
        "task": "app.tasks.trading.send_end_of_day_summary",
        "schedule": crontab(minute="5", hour="16", day_of_week="1-5"),
    },

    # Deactivated: sentiment weight=0 (Session 33). Uncomment to re-enable.
    # "update_sentiment_scores": {
    #     "task": "app.tasks.sentiment.update_sentiment_scores",
    #     "schedule": crontab(minute="0", hour="8,12", day_of_week="1-5"),
    # },

    # Deactivated: sentiment weight=0 (Session 33). Uncomment to re-enable.
    # "clear_stale_sentiment_cache": {
    #     "task": "app.tasks.sentiment.clear_stale_sentiment_cache",
    #     "schedule": crontab(minute="0", hour="0"),  # Daily at midnight
    # },

    # Daily Data Collection (6:00 AM EST before market open)
    "daily_data_collection": {
        "task": "app.tasks.data_tasks.collect_fundamentals",
        "schedule": crontab(minute="0", hour="6", day_of_week="1-6"),
    },

    # Daily VIX Collection (6:30 AM EST before market open)
    # Fetch VIX (Volatility Index) for regime detection enhancement
    "collect_vix_data": {
        "task": "app.tasks.vix_data.collect_vix_data",
        "schedule": crontab(minute="0", hour="6,18", day_of_week="1-6"),
    },

    # Weekly Model Tuning (Saturday 20:00 EST)
    # Optuna hyperparameter search with 100 trials
    "weekly_model_tuning": {
        "task": "app.tasks.training.tune_models",
        "schedule": crontab(minute="0", hour="20", day_of_week="6"),  # Saturday
    },

    # Weekly Model Training (Sunday 22:00 EST)
    # Full retrain with 2-year data after tuning
    "weekly_model_training": {
        "task": "app.tasks.training.train_models",
        "schedule": crontab(minute="0", hour="22", day_of_week="0"),  # Sunday
    },
}

# Auto-discover tasks
celery_app.autodiscover_tasks(['app.tasks'])
