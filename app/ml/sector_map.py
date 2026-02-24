"""
Sector mapping for stock symbols.
Used for categorical feature engineering.

Auto-fetches sector from yfinance if not in cache.

Updated: 2026-02-24 (Session 7 — Full 60-symbol GICS coverage, Unknown remap 99→12)
"""

import logging
from functools import lru_cache

import yfinance as yf

logger = logging.getLogger(__name__)

# Manual overrides (for speed, avoids API calls during training)
# Covers all 60 symbols from scripts/add_symbols.py
SECTOR_MAP: dict[str, str] = {
    # ── Market Index ETF ──
    'SPY': 'Market Index',
    'QQQ': 'Market Index',

    # ── Technology (12) ──
    'AAPL': 'Technology',
    'MSFT': 'Technology',
    'NVDA': 'Technology',
    'AMD': 'Technology',
    'AVGO': 'Technology',
    'CRM': 'Technology',
    'ADBE': 'Technology',
    'ORCL': 'Technology',
    'CSCO': 'Technology',
    'INTC': 'Technology',
    'TXN': 'Technology',
    'QCOM': 'Technology',
    'NOW': 'Technology',

    # ── Communication Services (4) ──
    'GOOGL': 'Communication Services',
    'META': 'Communication Services',
    'NFLX': 'Communication Services',
    'DIS': 'Communication Services',

    # ── Consumer Cyclical (6) ──
    'AMZN': 'Consumer Cyclical',
    'TSLA': 'Consumer Cyclical',
    'HD': 'Consumer Cyclical',
    'MCD': 'Consumer Cyclical',
    'NKE': 'Consumer Cyclical',
    'SBUX': 'Consumer Cyclical',

    # ── Consumer Defensive (5) ──
    'WMT': 'Consumer Defensive',
    'PG': 'Consumer Defensive',
    'KO': 'Consumer Defensive',
    'PEP': 'Consumer Defensive',
    'COST': 'Consumer Defensive',

    # ── Financial Services (6) ──
    'JPM': 'Financial Services',
    'V': 'Financial Services',
    'MA': 'Financial Services',
    'BAC': 'Financial Services',
    'GS': 'Financial Services',
    'BLK': 'Financial Services',

    # ── Healthcare (6) ──
    'JNJ': 'Healthcare',
    'UNH': 'Healthcare',
    'LLY': 'Healthcare',
    'PFE': 'Healthcare',
    'ABT': 'Healthcare',
    'TMO': 'Healthcare',

    # ── Energy (4) ──
    'XOM': 'Energy',
    'CVX': 'Energy',
    'COP': 'Energy',
    'SLB': 'Energy',

    # ── Industrials (6) ──
    'HON': 'Industrials',
    'CAT': 'Industrials',
    'UNP': 'Industrials',
    'GE': 'Industrials',
    'RTX': 'Industrials',
    'BA': 'Industrials',

    # ── Basic Materials (3) ──
    'LIN': 'Basic Materials',
    'APD': 'Basic Materials',
    'SHW': 'Basic Materials',

    # ── Real Estate (3) ──
    'AMT': 'Real Estate',
    'PLD': 'Real Estate',
    'CCI': 'Real Estate',

    # ── Utilities (3) ──
    'NEE': 'Utilities',
    'DUK': 'Utilities',
    'SO': 'Utilities',
}

# Sector to numeric ID (for CatBoost/LightGBM categorical features)
# Range: 0–12 (contiguous). No gaps — required for proper categorical encoding.
SECTOR_TO_ID: dict[str, int] = {
    'Technology': 0,
    'Communication Services': 1,
    'Consumer Cyclical': 2,
    'Consumer Defensive': 3,
    'Financial Services': 4,
    'Healthcare': 5,
    'Market Index': 6,
    'Energy': 7,
    'Industrials': 8,
    'Basic Materials': 9,
    'Real Estate': 10,
    'Utilities': 11,
    'Unknown': 12,  # Was 99 → remapped to 12 for contiguous categorical encoding
}

# Number of distinct sector categories (for model validation)
NUM_SECTORS: int = len(SECTOR_TO_ID)  # 13 (12 named + Unknown)

@lru_cache(maxsize=1000)
def get_sector_from_yfinance(symbol: str) -> str:
    """
    Fetch sector from yfinance API (cached).
    
    Returns:
        Sector name or 'Unknown' if failed
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        sector = info.get('sector', 'Unknown')

        logger.info(f"Auto-fetched sector for {symbol}: {sector}")
        return sector

    except Exception as e:
        logger.warning(f"Failed to fetch sector for {symbol}: {e}")
        return 'Unknown'

def get_sector(symbol: str) -> str:
    """
    Get sector for a symbol.
    
    Priority (UPDATED - API First):
    1. yfinance API (most up-to-date, cached)
    2. Manual SECTOR_MAP (fallback if API fails)
    
    Rationale:
    - API provides real-time sector data
    - Manual map serves as reliable fallback
    - LRU cache prevents excessive API calls
    """
    # Priority 1: Auto-fetch from yfinance (cached)
    sector = get_sector_from_yfinance(symbol)

    # Priority 2: Fallback to manual map if API failed
    if sector == 'Unknown' and symbol in SECTOR_MAP:
        sector = SECTOR_MAP[symbol]
        logger.debug(f"Using manual sector for {symbol}: {sector}")

    # Cache for future use (even if Unknown)
    if symbol not in SECTOR_MAP:
        SECTOR_MAP[symbol] = sector

    return sector

def get_sector_id(symbol: str) -> int:
    """Get numeric sector ID for a symbol.

    Returns:
        Integer 0–12 (contiguous). 12 = Unknown.
    """
    sector = get_sector(symbol)
    return SECTOR_TO_ID.get(sector, 12)  # 12 = Unknown (contiguous range)

