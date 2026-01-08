"""
기초재무 데이터 제공자
Phase F.2: yfinance API를 사용한 재무지표 수집
"""
import logging
from typing import Dict, Optional
from functools import lru_cache
from datetime import datetime, timedelta
import yfinance as yf

logger = logging.getLogger(__name__)


class FundamentalDataProvider:
    """
    yfinance에서 기본 재무지표를 가져옵니다.

    지표:
    - P/E: 주가수익비율
    - P/B: 주가순자산비율
    - ROE: 자기자본이익률
    - 배당수익률
    - 시가총액
    - 베타
    """
    
    def __init__(self):
        self._cache_ttl = timedelta(hours=24)  # Fundamentals change daily
        self._cache = {}  # Simple in-memory cache (symbol -> data)
    
    @lru_cache(maxsize=500)
    def get_fundamentals(self, symbol: str) -> Dict[str, Optional[float]]:
        """
        Fetch fundamental metrics for a symbol.
        
        Args:
            symbol: Stock ticker symbol (e.g., 'AAPL')
        
        Returns:
            Dict with fundamental metrics:
            {
                'pe_ratio': float or None,
                'pb_ratio': float or None,
                'roe': float or None (as decimal, e.g., 0.15 = 15%),
                'dividend_yield': float or None (as decimal),
                'market_cap': float or None,
                'beta': float or None,
                'updated_at': datetime
            }
        """
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.info
            
            # Extract metrics with fallback to None
            fundamentals = {
                'pe_ratio': info.get('trailingPE') or info.get('forwardPE'),
                'pb_ratio': info.get('priceToBook'),
                'roe': info.get('returnOnEquity'),  # Already as decimal
                'dividend_yield': info.get('dividendYield'),  # Already as decimal
                'market_cap': info.get('marketCap'),
                'beta': info.get('beta'),
                'updated_at': datetime.now()
            }
            
            logger.info(f"{symbol} 재무지표 수집됨: PE={fundamentals['pe_ratio']}, PB={fundamentals['pb_ratio']}")
            return fundamentals
        
        except Exception as e:
            logger.error(f"{symbol} 재무지표 수집 실패: {e}")
            return {
                'pe_ratio': None,
                'pb_ratio': None,
                'roe': None,
                'dividend_yield': None,
                'market_cap': None,
                'beta': None,
                'updated_at': datetime.now()
            }
    
    def get_pe_ratio(self, symbol: str) -> Optional[float]:
        """Get P/E ratio"""
        return self.get_fundamentals(symbol)['pe_ratio']
    
    def get_pb_ratio(self, symbol: str) -> Optional[float]:
        """Get P/B ratio"""
        return self.get_fundamentals(symbol)['pb_ratio']
    
    def get_roe(self, symbol: str) -> Optional[float]:
        """Get ROE (Return on Equity) as decimal"""
        return self.get_fundamentals(symbol)['roe']
    
    def get_dividend_yield(self, symbol: str) -> Optional[float]:
        """Get dividend yield as decimal"""
        return self.get_fundamentals(symbol)['dividend_yield']
    
    def is_value_stock(self, symbol: str, pe_threshold: float = 15.0, pb_threshold: float = 3.0) -> bool:
        """
        Determine if stock is a value stock based on P/E and P/B ratios.
        
        Criteria:
        - P/E < 15 (typically undervalued)
        - P/B < 3 (assets are undervalued)
        
        Args:
            symbol: Stock ticker
            pe_threshold: Maximum P/E ratio for value stock
            pb_threshold: Maximum P/B ratio for value stock
        
        Returns:
            True if stock meets value criteria
        """
        fundamentals = self.get_fundamentals(symbol)
        
        pe = fundamentals['pe_ratio']
        pb = fundamentals['pb_ratio']
        
        # Require both metrics to be available
        if pe is None or pb is None:
            return False
        
        is_value = pe < pe_threshold and pb < pb_threshold
        
        logger.debug(f"{symbol} 가치판단: PE={pe:.2f}, PB={pb:.2f}, is_value={is_value}")
        return is_value
    
    def is_growth_stock(self, symbol: str, roe_threshold: float = 0.15) -> bool:
        """
        Determine if stock is a growth stock based on ROE.
        
        Criteria:
        - ROE > 15% (strong profitability)
        
        Args:
            symbol: Stock ticker
            roe_threshold: Minimum ROE for growth stock (as decimal)
        
        Returns:
            True if stock meets growth criteria
        """
        roe = self.get_roe(symbol)
        
        if roe is None:
            return False
        
        is_growth = roe > roe_threshold
        
        logger.debug(f"{symbol} 성장판단: ROE={roe:.2%}, is_growth={is_growth}")
        return is_growth
    
    def is_income_stock(self, symbol: str, dividend_yield_threshold: float = 0.03) -> bool:
        """
        Determine if stock is an income stock based on dividend yield.
        
        Criteria:
        - Dividend Yield > 3%
        
        Args:
            symbol: Stock ticker
            dividend_yield_threshold: Minimum dividend yield (as decimal)
        
        Returns:
            True if stock meets income criteria
        """
        dividend_yield = self.get_dividend_yield(symbol)
        
        if dividend_yield is None:
            return False
        
        is_income = dividend_yield > dividend_yield_threshold
        
        logger.debug(f"{symbol} 배당판단: Div Yield={dividend_yield:.2%}, is_income={is_income}")
        return is_income
    
    def get_stock_category(self, symbol: str) -> str:
        """
        Categorize stock as VALUE, GROWTH, INCOME, or BLEND.
        
        Args:
            symbol: Stock ticker
        
        Returns:
            One of: 'VALUE', 'GROWTH', 'INCOME', 'BLEND', 'UNKNOWN'
        """
        is_value = self.is_value_stock(symbol)
        is_growth = self.is_growth_stock(symbol)
        is_income = self.is_income_stock(symbol)
        
        # Priority: If multiple categories match, use BLEND
        if sum([is_value, is_growth, is_income]) >= 2:
            return 'BLEND'
        elif is_value:
            return 'VALUE'
        elif is_growth:
            return 'GROWTH'
        elif is_income:
            return 'INCOME'
        else:
            return 'UNKNOWN'
    
    def get_risk_adjusted_score(self, symbol: str) -> Optional[float]:
        """
        Calculate risk-adjusted fundamental score.
        
        Formula:
        score = (ROE / PE_Ratio) * (1 + Dividend_Yield) / Beta
        
        Higher score = Better risk-adjusted value
        
        Args:
            symbol: Stock ticker
        
        Returns:
            Risk-adjusted score or None if insufficient data
        """
        fundamentals = self.get_fundamentals(symbol)
        
        roe = fundamentals['roe']
        pe = fundamentals['pe_ratio']
        div_yield = fundamentals['dividend_yield'] or 0.0  # Default to 0
        beta = fundamentals['beta']
        
        # Require ROE, PE, and Beta
        if roe is None or pe is None or beta is None or pe <= 0 or beta <= 0:
            return None
        
        try:
            score = (roe / pe) * (1 + div_yield) / beta
            logger.debug(f"{symbol} 위험조정 점수: {score:.4f}")
            return score
        except Exception as e:
            logger.warning(f"{symbol} 위험조정 점수 계산 실패: {e}")
            return None


# Singleton instance
_fundamental_provider_instance = None

def get_fundamental_provider() -> FundamentalDataProvider:
    """Get singleton FundamentalDataProvider instance"""
    global _fundamental_provider_instance
    if _fundamental_provider_instance is None:
        _fundamental_provider_instance = FundamentalDataProvider()
    return _fundamental_provider_instance
