"""Unit tests for CrossSectionalMomentum — Phase M.1.

Tests cover:
- Return calculations (1m, 3m, 6m-skip-1m)
- Min-max normalisation and composite score
- Sector relative strength
- Percentile ranking
- Sector rotation aggregation
- Top-N% selection
- Edge cases (insufficient data, single-symbol sector)
- Integration with _select_uncorrelated_symbols momentum filter
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from app.domain.schemas.momentum import MomentumScore, MomentumSummary, SectorRotation
from app.services.momentum_scorer import (
    CrossSectionalMomentum,
    _LOOKBACK_1M,
    _LOOKBACK_3M,
    _LOOKBACK_6M,
    _W_RETURN_1M,
    _W_RETURN_3M,
    _W_RETURN_6M_SKIP,
    _W_SECTOR_REL,
)


# ── Fixtures ──────────────────────────────────────────────────────────


def _make_ohlcv_bars(
    base_price: float,
    n_bars: int,
    daily_return: float = 0.001,
    start_date: datetime | None = None,
) -> list:
    """Generate mock OHLCV bars with steady growth/decline.

    Args:
        base_price: Starting close price.
        n_bars: Number of daily bars.
        daily_return: Per-day return (0.001 = +0.1%).
        start_date: First bar date. Defaults to ~7 months ago.

    Returns:
        List of mock bars with ``.close`` and ``.date_time`` attributes.
    """
    if start_date is None:
        start_date = datetime.now() - timedelta(days=int(n_bars * 1.5))

    bars = []
    price = base_price
    for idx in range(n_bars):
        bar = MagicMock()
        bar.close = round(price, 4)
        bar.date_time = start_date + timedelta(days=idx)
        bar.volume = 1_000_000
        bars.append(bar)
        price *= (1 + daily_return)

    return bars


def _build_score(
    symbol: str,
    sector: str,
    composite: float,
    percentile: float,
    return_3m: float = 0.0,
) -> MomentumScore:
    """Helper to build a MomentumScore fixture."""
    return MomentumScore(
        symbol=symbol,
        sector=sector,
        return_1m=0.01,
        return_3m=return_3m,
        return_6m_skip_1m=0.05,
        volatility_63d=0.20,
        vol_adjusted_momentum=0.5,
        sector_relative_strength=0.01,
        composite_score=composite,
        universe_percentile_rank=percentile,
        computed_at=datetime.now(),
    )


# ── Return Calculations ──────────────────────────────────────────────


class TestReturnCalculations:
    """Verify 1m/3m/6m return calculations against manual results."""

    def test_return_1m_positive(self):
        """1-month return for steadily rising stock."""
        bars = _make_ohlcv_bars(100.0, 130, daily_return=0.002)
        closes = pd.Series(
            [b.close for b in bars],
            index=pd.DatetimeIndex([b.date_time for b in bars]),
        )
        return_1m = float(closes.iloc[-1] / closes.iloc[-_LOOKBACK_1M] - 1)
        # 21 days * 0.2% = ~4.3% compound
        assert return_1m > 0.04
        assert return_1m < 0.06

    def test_return_3m_negative(self):
        """3-month return for declining stock."""
        bars = _make_ohlcv_bars(100.0, 130, daily_return=-0.002)
        closes = pd.Series(
            [b.close for b in bars],
            index=pd.DatetimeIndex([b.date_time for b in bars]),
        )
        return_3m = float(closes.iloc[-1] / closes.iloc[-_LOOKBACK_3M] - 1)
        assert return_3m < 0.0

    def test_return_6m_skip_1m(self):
        """6-month skip-1m return excludes most recent month."""
        bars = _make_ohlcv_bars(100.0, 130, daily_return=0.001)
        closes = pd.Series(
            [b.close for b in bars],
            index=pd.DatetimeIndex([b.date_time for b in bars]),
        )
        # Skip recent 21 days: compare close[-21] vs close[-126]
        if len(closes) >= _LOOKBACK_6M:
            ret = float(closes.iloc[-_LOOKBACK_1M] / closes.iloc[-_LOOKBACK_6M] - 1)
            assert ret > 0.0  # Should be positive for rising stock


# ── Normalisation & Composite ─────────────────────────────────────────


class TestNormalisationAndComposite:
    """Verify min-max scaling and composite formula."""

    def test_normalise_single_symbol_gives_05(self):
        """Single symbol should get composite = 0.5 (all normalised to 0.5)."""
        raw = [{
            "symbol": "AAPL",
            "sector": "Technology",
            "return_1m": 0.05,
            "return_3m": 0.10,
            "return_6m_skip_1m": 0.15,
            "volatility_63d": 0.20,
            "vol_adjusted_momentum": 0.50,
        }]
        scores = CrossSectionalMomentum._normalise_and_rank(raw)
        assert len(scores) == 1
        assert abs(scores[0].composite_score - 0.5) < 0.01

    def test_strongest_symbol_gets_rank_1(self):
        """Symbol with highest returns should rank first."""
        raw = [
            {
                "symbol": "STRONG",
                "sector": "Technology",
                "return_1m": 0.10,
                "return_3m": 0.30,
                "return_6m_skip_1m": 0.50,
                "volatility_63d": 0.15,
                "vol_adjusted_momentum": 2.0,
            },
            {
                "symbol": "WEAK",
                "sector": "Technology",
                "return_1m": -0.05,
                "return_3m": -0.10,
                "return_6m_skip_1m": -0.20,
                "volatility_63d": 0.30,
                "vol_adjusted_momentum": -0.33,
            },
        ]
        scores = CrossSectionalMomentum._normalise_and_rank(raw)
        assert scores[0].symbol == "STRONG"
        assert scores[0].composite_score > scores[1].composite_score

    def test_composite_weights_sum_to_one(self):
        """Verify formula weights are valid."""
        assert abs(_W_RETURN_1M + _W_RETURN_3M + _W_RETURN_6M_SKIP + _W_SECTOR_REL - 1.0) < 1e-9

    def test_composite_in_range(self):
        """All composite scores should be clamped to [0, 1]."""
        raw = [
            {
                "symbol": f"SYM{i}",
                "sector": "Technology" if i < 5 else "Healthcare",
                "return_1m": 0.01 * i,
                "return_3m": 0.02 * i,
                "return_6m_skip_1m": 0.015 * i,
                "volatility_63d": 0.15 + 0.01 * i,
                "vol_adjusted_momentum": 0.1 * i,
            }
            for i in range(10)
        ]
        scores = CrossSectionalMomentum._normalise_and_rank(raw)
        for s in scores:
            assert 0.0 <= s.composite_score <= 1.0
            assert 0.0 <= s.universe_percentile_rank <= 1.0


# ── Sector Relative Strength ─────────────────────────────────────────


class TestSectorRelativeStrength:
    """Verify sector-relative calculations."""

    def test_sector_leader_positive_relative(self):
        """Symbol beating sector average should have positive relative strength."""
        raw = [
            {
                "symbol": "LEADER",
                "sector": "Technology",
                "return_1m": 0.08,
                "return_3m": 0.20,
                "return_6m_skip_1m": 0.30,
                "volatility_63d": 0.18,
                "vol_adjusted_momentum": 1.1,
            },
            {
                "symbol": "LAGGARD",
                "sector": "Technology",
                "return_1m": 0.01,
                "return_3m": 0.02,
                "return_6m_skip_1m": 0.03,
                "volatility_63d": 0.25,
                "vol_adjusted_momentum": 0.08,
            },
        ]
        scores = CrossSectionalMomentum._normalise_and_rank(raw)
        leader = next(s for s in scores if s.symbol == "LEADER")
        laggard = next(s for s in scores if s.symbol == "LAGGARD")
        assert leader.sector_relative_strength > 0
        assert laggard.sector_relative_strength < 0

    def test_cross_sector_relative(self):
        """Sector relative strength is computed within each sector."""
        raw = [
            {
                "symbol": "TECH1",
                "sector": "Technology",
                "return_1m": 0.05,
                "return_3m": 0.15,
                "return_6m_skip_1m": 0.20,
                "volatility_63d": 0.15,
                "vol_adjusted_momentum": 1.0,
            },
            {
                "symbol": "TECH2",
                "sector": "Technology",
                "return_1m": 0.03,
                "return_3m": 0.10,
                "return_6m_skip_1m": 0.12,
                "volatility_63d": 0.18,
                "vol_adjusted_momentum": 0.56,
            },
            {
                "symbol": "FIN1",
                "sector": "Financial Services",
                "return_1m": 0.04,
                "return_3m": 0.12,
                "return_6m_skip_1m": 0.18,
                "volatility_63d": 0.20,
                "vol_adjusted_momentum": 0.60,
            },
        ]
        scores = CrossSectionalMomentum._normalise_and_rank(raw)
        tech1 = next(s for s in scores if s.symbol == "TECH1")
        tech2 = next(s for s in scores if s.symbol == "TECH2")
        # TECH1 above sector avg, TECH2 below
        assert tech1.sector_relative_strength > 0
        assert tech2.sector_relative_strength < 0


# ── Sector Rotation ──────────────────────────────────────────────────


class TestSectorRotation:
    """Verify sector-level aggregation."""

    def test_sector_ranking(self):
        """Sectors should be ranked by average momentum."""
        scores = [
            _build_score("AAPL", "Technology", 0.8, 0.9, return_3m=0.20),
            _build_score("MSFT", "Technology", 0.7, 0.8, return_3m=0.15),
            _build_score("JPM", "Financial Services", 0.5, 0.5, return_3m=0.05),
            _build_score("XOM", "Energy", 0.3, 0.2, return_3m=-0.05),
        ]
        rotations = CrossSectionalMomentum.get_sector_rotation(scores)
        assert rotations[0].sector == "Technology"
        assert rotations[0].rank == 1
        assert rotations[-1].sector == "Energy"

    def test_sector_top_symbols(self):
        """Top-3 symbols per sector should be correct."""
        scores = [
            _build_score("AAPL", "Technology", 0.9, 0.95, return_3m=0.25),
            _build_score("MSFT", "Technology", 0.8, 0.85, return_3m=0.20),
            _build_score("NVDA", "Technology", 0.7, 0.75, return_3m=0.15),
            _build_score("AMD", "Technology", 0.6, 0.65, return_3m=0.10),
        ]
        rotations = CrossSectionalMomentum.get_sector_rotation(scores)
        tech = rotations[0]
        assert tech.top_symbols == ["AAPL", "MSFT", "NVDA"]
        assert tech.symbol_count == 4

    def test_sector_count(self):
        """Each sector should have correct symbol count."""
        scores = [
            _build_score("AAPL", "Technology", 0.8, 0.9, return_3m=0.1),
            _build_score("JPM", "Financial Services", 0.5, 0.5, return_3m=0.05),
        ]
        rotations = CrossSectionalMomentum.get_sector_rotation(scores)
        for r in rotations:
            if r.sector in ("Technology", "Financial Services"):
                assert r.symbol_count == 1


# ── Top-N Selection ──────────────────────────────────────────────────


class TestTopNSelection:
    """Verify top-N% cutoff."""

    def test_top_20_percent(self):
        """Top 20% of 10 symbols = 2 symbols."""
        scores = [_build_score(f"SYM{i}", "Technology", i / 10, i / 10) for i in range(10)]
        top = CrossSectionalMomentum.select_top_n(scores, top_pct=0.20)
        assert len(top) == 2
        assert top[0] == "SYM9"
        assert top[1] == "SYM8"

    def test_top_50_percent(self):
        """Top 50% of 10 = 5 symbols."""
        scores = [_build_score(f"SYM{i}", "Technology", i / 10, i / 10) for i in range(10)]
        top = CrossSectionalMomentum.select_top_n(scores, top_pct=0.50)
        assert len(top) == 5

    def test_empty_scores(self):
        """Empty input returns empty list."""
        assert CrossSectionalMomentum.select_top_n([], top_pct=0.20) == []

    def test_minimum_one_symbol(self):
        """Even with tiny percentage, at least 1 symbol returned."""
        scores = [_build_score("AAPL", "Technology", 0.9, 0.9)]
        top = CrossSectionalMomentum.select_top_n(scores, top_pct=0.01)
        assert len(top) == 1


# ── Edge Cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    """Handle missing data, degenerate inputs."""

    def test_insufficient_bars_skipped(self):
        """Symbols with < 63 bars should be skipped."""
        scorer = CrossSectionalMomentum()
        repo = MagicMock()
        repo.get_ohlcv_range.return_value = _make_ohlcv_bars(100.0, 30)

        now = pd.Timestamp.now()
        result = scorer._compute_symbol(
            repo, "SHORT", pd.Timestamp(now - timedelta(days=200)), now
        )
        assert result is None

    def test_sufficient_bars_computed(self):
        """Symbols with >= 63 bars should produce a score."""
        scorer = CrossSectionalMomentum()
        repo = MagicMock()
        repo.get_ohlcv_range.return_value = _make_ohlcv_bars(100.0, 130, daily_return=0.001)

        now = pd.Timestamp.now()
        result = scorer._compute_symbol(
            repo, "AAPL", pd.Timestamp(now - timedelta(days=300)), now
        )
        assert result is not None
        assert result["symbol"] == "AAPL"
        assert result["return_3m"] > 0

    def test_zero_volatility_handled(self):
        """Flat prices (zero vol) should not cause division by zero."""
        scorer = CrossSectionalMomentum()
        repo = MagicMock()
        # All bars at exactly same price
        repo.get_ohlcv_range.return_value = _make_ohlcv_bars(100.0, 130, daily_return=0.0)

        now = pd.Timestamp.now()
        result = scorer._compute_symbol(
            repo, "FLAT", pd.Timestamp(now - timedelta(days=300)), now
        )
        assert result is not None
        assert result["vol_adjusted_momentum"] == 0.0


# ── Integration: _select_uncorrelated_symbols ────────────────────────


class TestMomentumIntegration:
    """Test momentum filter in symbol selection pipeline."""

    @patch.object(CrossSectionalMomentum, "get_cached_scores")
    def test_momentum_filter_removes_weak(self, mock_scores):
        """Symbols below 50th percentile should be filtered out."""
        # AAPL: strong momentum, INTC: weak momentum
        mock_scores.return_value = [
            _build_score("AAPL", "Technology", 0.9, 0.9),
            _build_score("INTC", "Technology", 0.1, 0.1),
        ]

        # Import here to use patched module
        from app.services.trading_strategy_sync import SyncTradingStrategy

        # Build minimal strategy object
        session = MagicMock()
        session.execute = MagicMock(return_value=MagicMock(scalars=MagicMock(return_value=[])))

        with patch.object(SyncTradingStrategy, "__init__", lambda self, s: None):
            strategy = SyncTradingStrategy.__new__(SyncTradingStrategy)

            signals = {
                "AAPL": {"class": 2, "confidence": 0.60, "kelly": 0.05, "price": 200},
                "INTC": {"class": 2, "confidence": 0.55, "kelly": 0.04, "price": 30},
            }
            corr_matrix = pd.DataFrame(
                [[1.0, 0.3], [0.3, 1.0]],
                index=["AAPL", "INTC"],
                columns=["AAPL", "INTC"],
            )
            selected = strategy._select_uncorrelated_symbols(
                signals, corr_matrix, set(), max_new_positions=5
            )
            assert "AAPL" in selected
            assert "INTC" not in selected  # Filtered by momentum

    @patch.object(CrossSectionalMomentum, "get_cached_scores")
    def test_graceful_degradation_no_momentum(self, mock_scores):
        """When no momentum data, all UP signals should pass through."""
        mock_scores.return_value = []  # No cached data

        from app.services.trading_strategy_sync import SyncTradingStrategy

        with patch.object(SyncTradingStrategy, "__init__", lambda self, s: None):
            strategy = SyncTradingStrategy.__new__(SyncTradingStrategy)

            signals = {
                "AAPL": {"class": 2, "confidence": 0.60, "kelly": 0.05, "price": 200},
                "INTC": {"class": 2, "confidence": 0.55, "kelly": 0.04, "price": 30},
            }
            corr_matrix = pd.DataFrame(
                [[1.0, 0.3], [0.3, 1.0]],
                index=["AAPL", "INTC"],
                columns=["AAPL", "INTC"],
            )
            selected = strategy._select_uncorrelated_symbols(
                signals, corr_matrix, set(), max_new_positions=5
            )
            # Both should pass since momentum lookup is empty (graceful degradation)
            assert "AAPL" in selected
            assert "INTC" in selected

    @patch.object(CrossSectionalMomentum, "get_cached_scores")
    def test_momentum_tiebreaker(self, mock_scores):
        """When confidence is similar, momentum rank breaks ties."""
        mock_scores.return_value = [
            _build_score("AAPL", "Technology", 0.9, 0.95),  # High momentum
            _build_score("MSFT", "Technology", 0.7, 0.70),  # Lower momentum
        ]

        from app.services.trading_strategy_sync import SyncTradingStrategy

        with patch.object(SyncTradingStrategy, "__init__", lambda self, s: None):
            strategy = SyncTradingStrategy.__new__(SyncTradingStrategy)

            signals = {
                "AAPL": {"class": 2, "confidence": 0.55, "kelly": 0.04, "price": 200},
                "MSFT": {"class": 2, "confidence": 0.55, "kelly": 0.04, "price": 400},
            }
            corr_matrix = pd.DataFrame(
                [[1.0, 0.3], [0.3, 1.0]],
                index=["AAPL", "MSFT"],
                columns=["AAPL", "MSFT"],
            )
            selected = strategy._select_uncorrelated_symbols(
                signals, corr_matrix, set(), max_new_positions=1
            )
            # AAPL should be selected first (higher momentum with same confidence)
            assert selected == ["AAPL"]


# ── Schema Validation ─────────────────────────────────────────────────


class TestSchemas:
    """Pydantic schema constraints."""

    def test_momentum_score_valid(self):
        """Valid MomentumScore should instantiate."""
        score = MomentumScore(
            symbol="AAPL",
            sector="Technology",
            composite_score=0.85,
            universe_percentile_rank=0.90,
        )
        assert score.symbol == "AAPL"
        assert score.composite_score == 0.85

    def test_composite_score_clamped(self):
        """Composite score > 1.0 should fail validation."""
        with pytest.raises(ValueError):
            MomentumScore(
                symbol="X",
                sector="X",
                composite_score=1.5,
                universe_percentile_rank=0.5,
            )

    def test_sector_rotation_valid(self):
        """Valid SectorRotation instantiation."""
        rot = SectorRotation(
            sector="Technology",
            avg_momentum=0.15,
            symbol_count=12,
            rank=1,
            top_symbols=["AAPL", "MSFT", "NVDA"],
        )
        assert rot.rank == 1
        assert len(rot.top_symbols) == 3

    def test_momentum_summary(self):
        """MomentumSummary should have correct structure."""
        summary = MomentumSummary(
            total_symbols=60,
            top_10=[_build_score(f"S{i}", "Technology", 0.9 - i * 0.01, 0.9) for i in range(10)],
            bottom_10=[_build_score(f"B{i}", "Technology", 0.1 + i * 0.01, 0.1) for i in range(10)],
        )
        assert summary.total_symbols == 60
        assert len(summary.top_10) == 10
