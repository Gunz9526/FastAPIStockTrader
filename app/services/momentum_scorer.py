"""Cross-Sectional Momentum Scorer — Phase M.1.

Computes relative-strength rankings across the full symbol universe,
normalises within GICS sectors, and produces composite momentum scores
for portfolio construction (symbol selection layer).

The scorer does **not** replace ML predictions — it provides an
additional signal used in ``_select_uncorrelated_symbols()`` as a
tiebreaker and pre-filter.

Composite formula
-----------------
.. math::

    C = 0.20 \\times \\text{norm}(r_{1m})
      + 0.40 \\times \\text{norm}(r_{3m})
      + 0.25 \\times \\text{norm}(r_{6m\\text{-skip-}1m})
      + 0.15 \\times \\text{norm}(\\text{sector\\_rel})

where ``norm`` maps raw values to ``[0, 1]`` via min-max scaling
across the universe.

Redis cache
-----------
* ``momentum:scores:{YYYY-MM-DD}`` — JSON list of all MomentumScores
* ``momentum:sectors:{YYYY-MM-DD}`` — JSON list of SectorRotation
* TTL = 86 400 s (24 h)
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from app.core.cache import cache
from app.core.database import SessionLocal
from app.domain.schemas.momentum import (
    MomentumScore,
    MomentumSummary,
    SectorRotation,
)
from app.ml.sector_map import SECTOR_MAP
from app.repositories.stock_repo_sync import SyncStockRepository

logger = logging.getLogger(__name__)

# ── Redis key format & TTL ──────────────────────────────────────────
_KEY_PREFIX: str = "momentum"
_SCORE_TTL: int = 86_400  # 24 hours

# ── Lookback constants (trading days) ───────────────────────────────
_LOOKBACK_1M: int = 21
_LOOKBACK_3M: int = 63
_LOOKBACK_6M: int = 126
_LOOKBACK_MAX: int = _LOOKBACK_6M + 10  # buffer for calendar gaps

# ── Composite weights ──────────────────────────────────────────────
_W_RETURN_1M: float = 0.20
_W_RETURN_3M: float = 0.40
_W_RETURN_6M_SKIP: float = 0.25
_W_SECTOR_REL: float = 0.15


# =====================================================================
# Public API
# =====================================================================


class CrossSectionalMomentum:
    """Cross-sectional momentum ranking engine.

    Fetches OHLCV data from DB via ``SyncStockRepository``, computes
    per-symbol momentum metrics, normalises across the universe, and
    stores results in Redis.

    Usage::

        scorer = CrossSectionalMomentum()
        scores = scorer.compute_all()        # list[MomentumScore]
        sectors = scorer.get_sector_rotation(scores)  # list[SectorRotation]
        top = scorer.select_top_n(scores, top_pct=0.20)  # list[str]
    """

    # ------------------------------------------------------------------
    # Core computation
    # ------------------------------------------------------------------

    def compute_all(self) -> list[MomentumScore]:
        """Compute momentum scores for every active symbol.

        Returns:
            Sorted list of ``MomentumScore`` (descending by composite).

        Raises:
            RuntimeError: If DB session cannot be created.
        """
        session = SessionLocal()
        try:
            repo = SyncStockRepository(session)
            symbols = repo.get_active_symbols()
            if not symbols:
                logger.warning("No active symbols for momentum scoring")
                return []

            end_date = pd.Timestamp.now(tz="UTC")
            start_date = pd.Timestamp(
                end_date - timedelta(days=_LOOKBACK_MAX * 2),  # calendar days
            )

            raw_scores: list[dict] = []
            for symbol in symbols:
                score = self._compute_symbol(repo, symbol, start_date, end_date)
                if score is not None:
                    raw_scores.append(score)

            if not raw_scores:
                logger.warning("No symbols had sufficient data for momentum scoring")
                return []

            # Normalise and build MomentumScore objects
            scores = self._normalise_and_rank(raw_scores)
            logger.info(
                "Momentum scores computed for %d/%d symbols",
                len(scores),
                len(symbols),
            )
            return scores
        finally:
            session.close()

    def compute_and_cache(self) -> int:
        """Compute scores and persist to Redis.

        Returns:
            Number of cached scores.
        """
        scores = self.compute_all()
        if not scores:
            return 0

        sectors = self.get_sector_rotation(scores)
        today = datetime.now().strftime("%Y-%m-%d")

        # Cache scores
        scores_key = f"{_KEY_PREFIX}:scores:{today}"
        scores_payload = [s.model_dump(mode="json") for s in scores]
        try:
            cache.set(scores_key, scores_payload, ttl_seconds=_SCORE_TTL)
        except Exception:
            logger.exception("Failed to cache momentum scores")

        # Cache sector rotation
        sectors_key = f"{_KEY_PREFIX}:sectors:{today}"
        sectors_payload = [s.model_dump(mode="json") for s in sectors]
        try:
            cache.set(sectors_key, sectors_payload, ttl_seconds=_SCORE_TTL)
        except Exception:
            logger.exception("Failed to cache sector rotation")

        logger.info("Cached %d momentum scores + %d sectors", len(scores), len(sectors))
        return len(scores)

    # ------------------------------------------------------------------
    # Sector rotation
    # ------------------------------------------------------------------

    @staticmethod
    def get_sector_rotation(scores: list[MomentumScore]) -> list[SectorRotation]:
        """Aggregate per-sector momentum and rank sectors.

        Args:
            scores: Pre-computed symbol scores.

        Returns:
            List of ``SectorRotation``, sorted by ``avg_momentum`` descending
            (rank 1 = strongest).
        """
        sector_groups: dict[str, list[MomentumScore]] = defaultdict(list)
        for s in scores:
            sector_groups[s.sector].append(s)

        rotations: list[SectorRotation] = []
        for sector, members in sector_groups.items():
            avg_mom = float(np.mean([m.return_3m for m in members]))
            # Top-3 symbols by composite
            top_3 = sorted(members, key=lambda m: m.composite_score, reverse=True)[:3]
            rotations.append(
                SectorRotation(
                    sector=sector,
                    avg_momentum=round(avg_mom, 6),
                    symbol_count=len(members),
                    rank=0,  # filled below
                    top_symbols=[m.symbol for m in top_3],
                )
            )

        # Assign ranks
        rotations.sort(key=lambda r: r.avg_momentum, reverse=True)
        for idx, rot in enumerate(rotations, start=1):
            rot.rank = idx

        return rotations

    # ------------------------------------------------------------------
    # Top-N selection
    # ------------------------------------------------------------------

    @staticmethod
    def select_top_n(
        scores: list[MomentumScore],
        top_pct: float = 0.20,
    ) -> list[str]:
        """Return symbols in the top ``top_pct`` percentile.

        Args:
            scores: Pre-computed symbol scores (any order).
            top_pct: Fraction of universe to keep (default 20 %).

        Returns:
            Symbol tickers in the top percentile, sorted descending.
        """
        if not scores:
            return []
        sorted_scores = sorted(scores, key=lambda s: s.composite_score, reverse=True)
        n = max(1, int(len(sorted_scores) * top_pct))
        return [s.symbol for s in sorted_scores[:n]]

    # ------------------------------------------------------------------
    # Cache reads
    # ------------------------------------------------------------------

    @staticmethod
    def get_cached_scores(date_str: str | None = None) -> list[MomentumScore]:
        """Read momentum scores from Redis.

        Args:
            date_str: ``"YYYY-MM-DD"`` key suffix.  Defaults to today.

        Returns:
            List of ``MomentumScore`` or empty list if cache miss.
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        key = f"{_KEY_PREFIX}:scores:{date_str}"
        try:
            data = cache.get(key)
            if data is None:
                return []
            return [MomentumScore(**item) for item in data]
        except Exception:
            logger.exception("Failed to read cached momentum scores")
            return []

    @staticmethod
    def get_cached_sectors(date_str: str | None = None) -> list[SectorRotation]:
        """Read sector rotation from Redis.

        Args:
            date_str: ``"YYYY-MM-DD"`` key suffix.  Defaults to today.

        Returns:
            List of ``SectorRotation`` or empty list if cache miss.
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")
        key = f"{_KEY_PREFIX}:sectors:{date_str}"
        try:
            data = cache.get(key)
            if data is None:
                return []
            return [SectorRotation(**item) for item in data]
        except Exception:
            logger.exception("Failed to read cached sector rotation")
            return []

    @staticmethod
    def get_cached_summary(date_str: str | None = None) -> MomentumSummary:
        """Build a ``MomentumSummary`` from cached data.

        Args:
            date_str: ``"YYYY-MM-DD"`` key suffix.  Defaults to today.

        Returns:
            Populated ``MomentumSummary``.
        """
        scores = CrossSectionalMomentum.get_cached_scores(date_str)
        sectors = CrossSectionalMomentum.get_cached_sectors(date_str)
        if not scores:
            return MomentumSummary()

        sorted_scores = sorted(scores, key=lambda s: s.composite_score, reverse=True)
        return MomentumSummary(
            computed_at=sorted_scores[0].computed_at,
            total_symbols=len(sorted_scores),
            top_10=sorted_scores[:10],
            bottom_10=sorted_scores[-10:],
            sector_rotations=sectors,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_symbol(
        self,
        repo: SyncStockRepository,
        symbol: str,
        start_date: pd.Timestamp,
        end_date: pd.Timestamp,
    ) -> dict | None:
        """Compute raw momentum metrics for a single symbol.

        Args:
            repo: DB repository.
            symbol: Stock ticker.
            start_date: Earliest date to fetch.
            end_date: Latest date to fetch.

        Returns:
            Dict with raw metric values, or ``None`` if insufficient data.
        """
        try:
            ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date, timeframe="1d")
            if len(ohlcv) < _LOOKBACK_3M:
                logger.debug("%s: insufficient data (%d bars)", symbol, len(ohlcv))
                return None

            closes = pd.Series(
                [bar.close for bar in ohlcv],
                index=pd.DatetimeIndex([bar.date_time for bar in ohlcv]),
            ).sort_index()

            n = len(closes)

            # 1-month return (last 21 days)
            return_1m = (
                float(closes.iloc[-1] / closes.iloc[-_LOOKBACK_1M] - 1)
                if n >= _LOOKBACK_1M else 0.0
            )

            # 3-month return (last 63 days)
            return_3m = (
                float(closes.iloc[-1] / closes.iloc[-_LOOKBACK_3M] - 1)
                if n >= _LOOKBACK_3M
                else float(closes.iloc[-1] / closes.iloc[0] - 1)
            )

            # 6-month return skipping most recent month (academic convention)
            return_6m_skip_1m = (
                float(closes.iloc[-_LOOKBACK_1M] / closes.iloc[-_LOOKBACK_6M] - 1)
                if n >= _LOOKBACK_6M else 0.0
            )

            # 63-day annualised volatility
            daily_returns = closes.pct_change().dropna()
            recent_returns = (
                daily_returns.iloc[-_LOOKBACK_3M:]
                if len(daily_returns) >= _LOOKBACK_3M else daily_returns
            )
            vol_std = recent_returns.std()
            volatility_63d = float(vol_std * np.sqrt(252)) if vol_std is not None else 0.0

            # Volatility-adjusted momentum
            vol_adj_mom = (
                return_3m / volatility_63d if volatility_63d > 1e-8 else 0.0
            )

            sector = SECTOR_MAP.get(symbol, "Unknown")

            return {
                "symbol": symbol,
                "sector": sector,
                "return_1m": return_1m,
                "return_3m": return_3m,
                "return_6m_skip_1m": return_6m_skip_1m,
                "volatility_63d": volatility_63d,
                "vol_adjusted_momentum": vol_adj_mom,
            }

        except Exception:
            logger.exception("Failed to compute momentum for %s", symbol)
            return None

    @staticmethod
    def _normalise_and_rank(raw_scores: list[dict]) -> list[MomentumScore]:
        """Min-max normalise raw values and compute composite + percentile.

        Args:
            raw_scores: List of dicts from ``_compute_symbol``.

        Returns:
            Sorted list of ``MomentumScore`` (descending composite).
        """
        df = pd.DataFrame(raw_scores)

        # Sector relative strength: symbol return_3m - sector avg return_3m
        sector_avg = df.groupby("sector")["return_3m"].transform("mean")
        df["sector_relative_strength"] = df["return_3m"] - sector_avg

        # Min-max normalisation helper
        def _minmax(series: pd.Series) -> pd.Series:
            smin, smax = series.min(), series.max()
            if abs(smax - smin) < 1e-12:
                return pd.Series(0.5, index=series.index)
            return (series - smin) / (smax - smin)

        norm_1m = _minmax(df["return_1m"])
        norm_3m = _minmax(df["return_3m"])
        norm_6m = _minmax(df["return_6m_skip_1m"])
        norm_sr = _minmax(df["sector_relative_strength"])

        df["composite_score"] = (
            _W_RETURN_1M * norm_1m
            + _W_RETURN_3M * norm_3m
            + _W_RETURN_6M_SKIP * norm_6m
            + _W_SECTOR_REL * norm_sr
        )

        # Clamp to [0, 1]
        df["composite_score"] = df["composite_score"].clip(0.0, 1.0)

        # Percentile rank (0 = weakest, 1 = strongest)
        df["universe_percentile_rank"] = df["composite_score"].rank(pct=True)

        now = datetime.now()
        scores: list[MomentumScore] = []
        for _, row in df.iterrows():
            scores.append(
                MomentumScore(
                    symbol=row["symbol"],
                    sector=row["sector"],
                    return_1m=round(float(row["return_1m"]), 6),
                    return_3m=round(float(row["return_3m"]), 6),
                    return_6m_skip_1m=round(float(row["return_6m_skip_1m"]), 6),
                    volatility_63d=round(float(row["volatility_63d"]), 6),
                    vol_adjusted_momentum=round(float(row["vol_adjusted_momentum"]), 4),
                    sector_relative_strength=round(float(row["sector_relative_strength"]), 6),
                    composite_score=round(float(row["composite_score"]), 6),
                    universe_percentile_rank=round(float(row["universe_percentile_rank"]), 4),
                    computed_at=now,
                )
            )

        scores.sort(key=lambda s: s.composite_score, reverse=True)
        return scores
