"""
Phase L.2a: 15min Intraday Feature Calculator.

Computes RSI(14) and MACD(12,26,9) on 15min OHLCV bars for rule-based
entry signal detection. These are NOT ML features — they are deterministic
technical indicators used by the DualTimeframeOrchestrator (Phase L.2b).

Indicator computation uses TA-Lib for consistency with the daily ML pipeline
(app/ml/features.py). Requires at least 50 bars (≈2 trading days) for
MACD(26) to stabilize.

Ref: .agent/plan-report/Plan_2026-02-27_L2-Dual-Timeframe-Split.md
"""
import logging
from datetime import datetime, timedelta

import numpy as np
import talib
from pytz import timezone
from sqlalchemy.orm import Session

from app.domain.schemas.intraday import IntradayIndicators, IntradayIndicatorsSummary
from app.repositories.stock_repo_sync import SyncStockRepository

logger = logging.getLogger(__name__)

# Minimum bars required for MACD(26) stabilization + buffer
MIN_BARS_REQUIRED: int = 50

# RSI period
RSI_PERIOD: int = 14

# MACD parameters
MACD_FAST: int = 12
MACD_SLOW: int = 26
MACD_SIGNAL: int = 9


def is_market_hours(dt: datetime | None = None) -> bool:
    """
    Check if the given time is within US market hours (9:30–16:00 ET, Mon–Fri).

    Args:
        dt: Datetime to check. If None, uses current ET time.

    Returns:
        True if within market trading hours.
    """
    et_tz = timezone("America/New_York")
    if dt is None:
        dt = datetime.now(et_tz)
    elif dt.tzinfo is None:
        dt = et_tz.localize(dt)
    else:
        dt = dt.astimezone(et_tz)

    # Weekend check (0=Monday, 6=Sunday)
    if dt.weekday() > 4:
        return False

    # Market hours: 9:30 – 16:00 ET
    market_open = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = dt.replace(hour=16, minute=0, second=0, microsecond=0)

    return market_open <= dt <= market_close


def compute_intraday_indicators(
    symbol: str,
    close_prices: np.ndarray,
    timestamp: datetime,
) -> IntradayIndicators:
    """
    Compute RSI(14) and MACD(12,26,9) from 15min close prices.

    Args:
        symbol: Stock ticker symbol.
        close_prices: Array of close prices from 15min bars, oldest first.
            Must have at least MIN_BARS_REQUIRED elements.
        timestamp: Computation timestamp (ET).

    Returns:
        IntradayIndicators with computed values, or None fields if
        insufficient bars.
    """
    if len(close_prices) < MIN_BARS_REQUIRED:
        logger.debug(
            f"{symbol}: 15min bars 부족 ({len(close_prices)}/{MIN_BARS_REQUIRED})"
        )
        return IntradayIndicators(
            symbol=symbol,
            timestamp=timestamp,
            rsi_14=None,
            macd_line=None,
            macd_signal=None,
            macd_histogram=None,
            prev_macd_histogram=None,
        )

    # RSI(14)
    rsi = talib.RSI(close_prices, timeperiod=RSI_PERIOD)
    current_rsi = float(rsi[-1]) if not np.isnan(rsi[-1]) else None

    # MACD(12, 26, 9)
    macd_line, macd_signal, macd_hist = talib.MACD(
        close_prices,
        fastperiod=MACD_FAST,
        slowperiod=MACD_SLOW,
        signalperiod=MACD_SIGNAL,
    )

    current_macd_line = float(macd_line[-1]) if not np.isnan(macd_line[-1]) else None
    current_macd_signal = float(macd_signal[-1]) if not np.isnan(macd_signal[-1]) else None
    current_macd_hist = float(macd_hist[-1]) if not np.isnan(macd_hist[-1]) else None
    prev_macd_hist = float(macd_hist[-2]) if len(macd_hist) >= 2 and not np.isnan(macd_hist[-2]) else None

    return IntradayIndicators(
        symbol=symbol,
        timestamp=timestamp,
        rsi_14=current_rsi,
        macd_line=current_macd_line,
        macd_signal=current_macd_signal,
        macd_histogram=current_macd_hist,
        prev_macd_histogram=prev_macd_hist,
    )


def compute_indicators_for_symbol(
    symbol: str,
    db: Session,
    lookback_days: int = 5,
) -> IntradayIndicators:
    """
    Fetch recent 15min bars from DB and compute intraday indicators.

    Args:
        symbol: Stock ticker symbol.
        db: SQLAlchemy session.
        lookback_days: Number of calendar days to look back for 15min bars.

    Returns:
        IntradayIndicators with computed values.
    """
    et_tz = timezone("America/New_York")
    now = datetime.now(et_tz)

    repo = SyncStockRepository(db)
    end_time = now
    start_time = now - timedelta(days=lookback_days)

    bars = repo.get_ohlcv_range(
        symbol=symbol,
        start_date=start_time,
        end_date=end_time,
        timeframe="15m",
    )

    if not bars:
        logger.debug(f"{symbol}: 15min bars 없음 (lookback={lookback_days}d)")
        return IntradayIndicators(
            symbol=symbol,
            timestamp=now,
            rsi_14=None,
            macd_line=None,
            macd_signal=None,
            macd_histogram=None,
            prev_macd_histogram=None,
        )

    close_prices = np.array([float(bar.close) for bar in bars], dtype=np.float64)

    return compute_intraday_indicators(
        symbol=symbol,
        close_prices=close_prices,
        timestamp=now,
    )


def compute_all_indicators(
    db: Session,
    symbols: list[str] | None = None,
    lookback_days: int = 5,
) -> IntradayIndicatorsSummary:
    """
    Compute intraday indicators for all active symbols.

    Args:
        db: SQLAlchemy session.
        symbols: Optional specific symbols. If None, uses all active symbols.
        lookback_days: Number of calendar days to look back.

    Returns:
        IntradayIndicatorsSummary with per-symbol indicators.
    """
    et_tz = timezone("America/New_York")
    now = datetime.now(et_tz)

    repo = SyncStockRepository(db)

    if symbols is None:
        symbols = repo.get_active_symbols()

    if not symbols:
        logger.warning("인트라데이 지표 계산: 활성 심볼 없음")
        return IntradayIndicatorsSummary(
            timestamp=now,
            total_symbols=0,
            signals_found=0,
            indicators=[],
        )

    indicators: list[IntradayIndicators] = []
    signals_found = 0

    for symbol in symbols:
        indicator = compute_indicators_for_symbol(
            symbol=symbol,
            db=db,
            lookback_days=lookback_days,
        )
        indicators.append(indicator)

        if indicator.has_entry_signal:
            signals_found += 1
            logger.info(
                f"{symbol}: 15min 진입 시그널 감지! "
                f"RSI={indicator.rsi_14:.1f}, MACD_hist={indicator.macd_histogram:.6f}"
            )

    logger.info(
        f"인트라데이 지표 계산 완료: {len(symbols)}개 심볼, {signals_found}개 진입 시그널"
    )

    return IntradayIndicatorsSummary(
        timestamp=now,
        total_symbols=len(symbols),
        signals_found=signals_found,
        indicators=indicators,
    )
