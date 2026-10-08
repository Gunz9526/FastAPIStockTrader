from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_SYMBOLS = (
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AVGO",
    "AMD",
    "NFLX",
    "COST",
    "WMT",
    "JPM",
    "BAC",
    "GS",
    "XOM",
    "CVX",
    "UNH",
    "JNJ",
    "ABBV",
    "LLY",
    "PG",
    "HD",
    "CAT",
    "GE",
    "CRM",
    "ORCL",
    "ADBE",
    "SPY",
    "QQQ",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", secrets_dir=".secrets", extra="ignore")

    alpaca_key: SecretStr = SecretStr("")
    alpaca_secret: SecretStr = SecretStr("")
    discord_webhook: SecretStr = SecretStr("")
    api_token: SecretStr = SecretStr("")
    db_password: SecretStr = SecretStr("")
    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "trader"
    db_user: str = "trader"
    capital_limit: float = Field(default=100_000, gt=0)
    daily_loss_limit: float = Field(default=0.015, gt=0, le=0.10)
    trade_risk: float = Field(default=0.0025, gt=0, le=0.01)
    gross_exposure_limit: float = Field(default=0.60, gt=0, le=1)
    symbol_exposure_limit: float = Field(default=0.15, gt=0, le=0.30)
    max_positions: int = Field(default=4, ge=1, le=10)
    max_drawdown: float = Field(default=0.10, gt=0, le=0.30)
    max_spread: float = Field(default=0.003, gt=0, le=0.01)
    stop_atr: float = Field(default=2, ge=1, le=5)
    reward_risk: float = Field(default=2, ge=1, le=4)
    round_trip_cost_bps: float = Field(default=20, ge=5, le=200)
    training_threads: int = Field(default=2, ge=1, le=2)
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    benchmark_symbols: tuple[str, ...] = ("SPY", "QQQ")
    data_dir: Path = Path("data")
    artifact_dir: Path = Path("artifacts")
    report_dir: Path = Path("reports")
    auto_start: bool = True
    poll_seconds: int = Field(default=5, ge=2, le=30)
    reentry_cooldown_minutes: int = Field(default=30, ge=15, le=120)
    model_max_age_days: int = Field(default=21, ge=7, le=90)
    history_days: int = Field(default=730, ge=180, le=2500)

    @field_validator("symbols", "benchmark_symbols")
    @classmethod
    def validate_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > 30 or len(set(value)) != len(value):
            raise ValueError("IEX symbols must be unique and contain 1 to 30 entries")
        if any(not s.replace(".", "").isalpha() or s != s.upper() for s in value):
            raise ValueError("invalid stock symbol")
        return value

    @model_validator(mode="after")
    def validate_contract(self):
        if not set(self.benchmark_symbols).issubset(self.symbols):
            raise ValueError("benchmarks must be included in the 30-symbol subscription")
        if self.trade_risk >= self.daily_loss_limit:
            raise ValueError("trade risk must be below daily loss limit")
        return self

    @property
    def credentials_present(self) -> bool:
        return bool(self.alpaca_key.get_secret_value() and self.alpaca_secret.get_secret_value())

    @property
    def trade_symbols(self) -> tuple[str, ...]:
        return tuple(s for s in self.symbols if s not in self.benchmark_symbols)

    def public_contract(self) -> dict:
        return self.model_dump(
            mode="json",
            exclude={
                "alpaca_key",
                "alpaca_secret",
                "discord_webhook",
                "api_token",
                "db_password",
            },
        )

    def db_kwargs(self) -> dict:
        return dict(
            host=self.db_host,
            port=self.db_port,
            dbname=self.db_name,
            user=self.db_user,
            password=self.db_password.get_secret_value(),
            connect_timeout=10,
        )
