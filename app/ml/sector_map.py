"""
Sector mapping for stock symbols.
Used for categorical feature engineering.

Auto-fetches sector from yfinance if not in cache.
"""

import yfinance as yf
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# Manual overrides (for speed, avoids API calls)
SECTOR_MAP = {
    # Technology
    'AAPL': 'Technology',
    'GOOGL': 'Technology',
    'MSFT': 'Technology',
    'NVDA': 'Technology',
    'META': 'Technology',
    'AMD': 'Technology',
    'NFLX': 'Communication Services',
    
    # Automotive
    'TSLA': 'Consumer Cyclical',
    
    # Finance
    'JPM': 'Financial Services',
    'BAC': 'Financial Services',
    'V': 'Financial Services',
    
    # Healthcare
    'JNJ': 'Healthcare',
    'PFE': 'Healthcare',
    
    # Consumer
    'WMT': 'Consumer Defensive',
    'HD': 'Consumer Cyclical',
    'AMZN': 'Consumer Cyclical',
    
    # Market Index
    'SPY': 'Market Index',
}

# Sector to numeric ID (for CatBoost categorical features)
SECTOR_TO_ID = {
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
    'Unknown': 99,
}

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
    """Get numeric sector ID for a symbol."""
    sector = get_sector(symbol)
    return SECTOR_TO_ID.get(sector, 99)  # 99 = Unknown

