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
REGIME_TRADING_CONFIG = {
    'bull_trending': {
        'buy_threshold': 0.004,    # 0.4% (conservative, 2x normal)
        'sell_threshold': -0.001,  # -0.1% (tight stop)
        'position_scale': 0.3,     # 30% of normal position size
        'enabled': True,           # Can disable entirely if model unreliable
        'confidence': 0.3,         # Model confidence weight
        'description': 'Conservative approach due to poor model performance (49% acc, -0.22 Sharpe)',
    },
    'bear_trending': {
        'buy_threshold': 0.002,    # 0.2% (standard)
        'sell_threshold': -0.002,  # -0.2% (standard)
        'position_scale': 0.7,     # 70% (slightly conservative due to high volatility)
        'enabled': True,
        'confidence': 0.7,
        'description': 'Moderate confidence (52.8% acc, 10.5 Sharpe - may be overfit)',
    },
    'sideways_volatile': {
        'buy_threshold': 0.003,    # 0.3% (wider threshold for noise)
        'sell_threshold': -0.003,  # -0.3%
        'position_scale': 0.5,     # 50% (reduced due to chop)
        'enabled': True,
        'confidence': 0.5,
        'description': 'Moderate caution in choppy markets',
    },
    'sideways_calm': {
        'buy_threshold': 0.002,    # 0.2% (standard)
        'sell_threshold': -0.002,  # -0.2%
        'position_scale': 1.0,     # Full position (most reliable regime)
        'enabled': True,
        'confidence': 0.7,
        'description': 'High confidence (53.2% acc, 6.5 Sharpe)',
    },
}


settings = Settings()
