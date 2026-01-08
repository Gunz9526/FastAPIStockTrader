from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

# Handle Pydantic types and fallbacks explicitly
broker_url = str(settings.CELERY_BROKER_URL) if settings.CELERY_BROKER_URL else str(settings.REDIS_URL)
result_backend = str(settings.CELERY_RESULT_BACKEND) if settings.CELERY_RESULT_BACKEND else str(settings.REDIS_URL)

print("REDIS_URL =", settings.REDIS_URL)
print("BROKER_URL =", broker_url)

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

# celery_app.set_default()
# celery_app.autodiscover_tasks([
#     "app.tasks",
# ])

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/New_York",
    enable_utc=False,
    broker_connection_retry_on_startup=True,
)


celery_app.conf.beat_schedule = {
    # PreMarket Analysis (8:30 AM EST, Monday-Friday)
    "premarket_analysis": {
        "task": "app.tasks.market_analysis.analyze_market",
        "schedule": crontab(minute="30", hour="8", day_of_week="1-5"),
    },
    
    # Market Scan (Every hour during trading hours)
    "market_scan_trading_hours": {
        "task": "app.tasks.trading.execute_market_scan",
        "schedule": crontab(minute="30", hour="9-15", day_of_week="1-5"),
    },
    
    # Trailing Stop Updates (Every 15 minutes during trading hours)
    "update_trailing_stops": {
        "task": "app.tasks.trading.update_trailing_stops",
        "schedule": crontab(minute="*/15", hour="9-16", day_of_week="1-5"),
    },
    
    # Real-time 15-minute OHLCV Collection (Every 15 minutes during market hours)
    "collect_15m_realtime": {
        "task": "app.tasks.realtime_data.collect_15m_realtime",
        "schedule": crontab(minute="0,15,30,45", hour="9-15", day_of_week="1-5"),
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
    
    # Hourly Sentiment Analysis Update (Every hour, 24/7)
    # Fetches news from API, analyzes with Gemini, caches in Redis
    "update_sentiment_scores": {
        "task": "app.tasks.sentiment.update_sentiment_scores",
        "schedule": crontab(minute="0", hour="9-15", day_of_week="1-5"),
    },
    
    # Daily Sentiment Cache Cleanup (Midnight EST)
    # Optional: Redis TTL handles expiration automatically
    "clear_stale_sentiment_cache": {
        "task": "app.tasks.sentiment.clear_stale_sentiment_cache",
        "schedule": crontab(minute="0", hour="0"),  # Daily at midnight
    },
    
    # Daily Data Collection (6:00 AM EST before market open)
    "daily_data_collection": {
        "task": "app.tasks.data_tasks.collect_fundamentals",
        "schedule": crontab(minute="0", hour="6", day_of_week="1-6"),
    },
    
    # Daily VIX Collection (6:30 AM EST before market open)
    # Fetch VIX (Volatility Index) for regime detection enhancement
    "collect_vix_data": {
        "task": "app.tasks.vix_data.collect_vix_data",
        "schedule": crontab(minute="30", hour="6", day_of_week="1-6"),
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
