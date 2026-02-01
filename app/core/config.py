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


# Phase H.4: Regime-Specific Trading Configuration
# ADR-001: Regime-Specific Trading Thresholds
#
# KEY INSIGHT: The model tends to sell too early. Solutions:
# 1. Higher sell_threshold (more negative) = harder to trigger sell
# 2. min_hold_multiplier = extend minimum holding time per regime
# 3. min_profit_required = minimum profit % before allowing sell signal
#
REGIME_TRADING_CONFIG = {
    'bull_trending': {
        'buy_threshold': 0.003,    # 0.3% - moderate entry
        'sell_threshold': -0.005,  # -0.5% - MUCH harder to sell (avoid early exits)
        'position_scale': 0.5,     # 50% position (balanced risk)
        'min_hold_multiplier': 2.0,  # 2x normal hold time (120min instead of 60)
        'min_profit_required': 0.02,  # Require 2% profit before considering sell
        'enabled': True,
        'confidence': 0.4,
        'description': 'Bull markets need patience - extend hold times, avoid early sells',
    },
    'bear_trending': {
        'buy_threshold': 0.004,    # 0.4% - conservative entry in bearish conditions
        'sell_threshold': -0.003,  # -0.3% - moderate sell threshold
        'position_scale': 0.5,     # 50% (risk management in bear market)
        'min_hold_multiplier': 1.5,  # 1.5x hold time
        'min_profit_required': 0.015,  # 1.5% profit required
        'enabled': True,
        'confidence': 0.5,
        'description': 'Bear market - quick profits, moderate holding',
    },
    'sideways_volatile': {
        'buy_threshold': 0.005,    # 0.5% - high threshold for choppy markets
        'sell_threshold': -0.004,  # -0.4% - harder to sell
        'position_scale': 0.3,     # 30% (reduced exposure in chop)
        'min_hold_multiplier': 1.0,  # Normal hold time
        'min_profit_required': 0.01,  # 1% profit required
        'enabled': True,
        'confidence': 0.3,
        'description': 'Volatile sideways - selective trades only',
    },
    'sideways_calm': {
        'buy_threshold': 0.002,    # 0.2% - normal sensitivity
        'sell_threshold': -0.004,  # -0.4% - still conservative on sells
        'position_scale': 1.0,     # Full position (best regime)
        'min_hold_multiplier': 1.5,  # 1.5x hold time for better profits
        'min_profit_required': 0.015,  # 1.5% profit required
        'enabled': True,
        'confidence': 0.7,
        'description': 'Calm market - full confidence, patient holding',
    },
}


settings = Settings()
