from typing import Any

from pydantic import AnyHttpUrl, PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "FastAPI Stock Trader"
    API_V1_STR: str = "/api/v1"
    ENV_STATE: str = "dev"

    # Security
    SECRET_KEY: str
    API_SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Database
    DATABASE_URL: PostgresDsn

    # Redis
    REDIS_URL: RedisDsn
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # Celery
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None

    @field_validator("CELERY_BROKER_URL", mode="before")
    def assemble_celery_broker(cls, v: str | None, values: dict[str, Any]) -> Any:
        if isinstance(v, str):
            return v
        # Fallback to REDIS_URL if not explicitly set
        redis_url = values.data.get("REDIS_URL")
        return str(redis_url) if redis_url else None

    @field_validator("CELERY_RESULT_BACKEND", mode="before")
    def assemble_celery_backend(cls, v: str | None, values: dict[str, Any]) -> Any:
        if isinstance(v, str):
            return v
        # Fallback to REDIS_URL if not explicitly set
        redis_url = values.data.get("REDIS_URL")
        return str(redis_url) if redis_url else None


    # CORS
    BACKEND_CORS_ORIGINS: list[AnyHttpUrl] = []

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    def assemble_cors_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    # API Keys & External Services
    ALPACA_API_KEY: str | None = None
    ALPACA_SECRET_KEY: str | None = None
    ALPACA_TRADING_URL: str = "https://paper-api.alpaca.markets"
    ALPACA_DATA_URL: str = "https://data.alpaca.markets"
    
    # Discord Webhook
    DISCORD_WEBHOOK_URL: str | None = None
    DISCORD_TRADING_URL: str | None = None  # 거래 전용 알림

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )


# Regime-Specific Trading Configuration
# Regime-Specific Trading Thresholds (Daily Bars + Ternary Classification)
#
# KEY INSIGHT: Classification model outputs (class, confidence) instead of raw float.
# confidence_threshold: minimum confidence to act on prediction (0.0-1.0)
# Predictions: 0=DOWN, 1=NEUTRAL, 2=UP
#
# BULL_TRENDING NOTE: Model accuracy is improving with classification.
# Use fallback_to_regime to use sideways_calm model instead when bull is detected.
#
REGIME_TRADING_CONFIG = {
    'bull_trending': {
        'confidence_threshold': 0.45,  # 45% confidence to act
        'position_scale': 0.5,     # 50% position (balanced risk)
        'min_hold_days': 2,        # Minimum 2 trading days hold
        'min_profit_required': 0.02,  # Require 2% profit before considering sell
        'enabled': True,
        'fallback_to_regime': 'sideways_calm',  # Use sideways_calm model (better performance)
        'description': 'Bull markets need patience - extend hold times, avoid early sells',
    },
    'bear_trending': {
        'confidence_threshold': 0.55,  # 55% confidence (more conservative in bear)
        'position_scale': 0.5,     # 50% (risk management in bear market)
        'min_hold_days': 1,        # 1 day hold (quick exit in bear)
        'min_profit_required': 0.015,  # 1.5% profit required
        'enabled': True,
        'description': 'Bear market - quick profits, moderate holding',
    },
    'sideways_volatile': {
        'confidence_threshold': 0.60,  # 60% confidence (high bar for choppy markets)
        'position_scale': 0.3,     # 30% (reduced exposure in chop)
        'min_hold_days': 1,        # 1 day hold
        'min_profit_required': 0.01,  # 1% profit required
        'enabled': True,
        'description': 'Volatile sideways - selective trades only',
    },
    'sideways_calm': {
        'confidence_threshold': 0.40,  # 40% confidence (best regime)
        'position_scale': 1.0,     # Full position (best regime)
        'min_hold_days': 2,        # 2 days hold for better profits
        'min_profit_required': 0.015,  # 1.5% profit required
        'enabled': True,
        'description': 'Calm market - full confidence, patient holding',
    },
}


settings = Settings()
