import pytest
import pandas as pd
from app.ml.features import FeatureEngineer
from app.services.risk_manager import RiskManager

def test_risk_manager():
    rm = RiskManager(
        max_position_size_pct=0.1,
        stop_loss_atr_multiplier=2.0,
        take_profit_atr_multiplier=3.0
    )
    
    # 1. Normal Position Sizing
    # Balance 100,000 -> Max Pos 10% = 10,000. Price 100 -> Qty 100.
    allowed, qty = rm.calculate_position_size("AAPL", 100.0, 100000.0)
    assert allowed is True
    assert qty == 100

    # 2. Insufficient Funds (Price > Max Position)
    # Max Pos 10,000. Price 11,000. Qty 0.
    allowed, qty = rm.calculate_position_size("BRK.A", 11000.0, 100000.0)
    assert allowed is False
    assert qty == 0

    # 3. Dynamic Exit Prices (ATR-based, mock ATR=2.0)
    atr = 2.0
    sl, tp, trailing = rm.calculate_exit_prices(100.0, atr)
    assert sl == 100.0 - (2.0 * 2.0)  # 96.0 (stop_loss_atr_mult=2.0)
    assert tp == 100.0 + (3.0 * 2.0)  # 106.0 (take_profit_atr_mult=3.0)
    assert trailing == 100.0 - (1.5 * 2.0)  # 97.0 (trailing_stop_atr_mult=1.5)

def test_feature_engineer():
    # Mock OHLCV
    data = {
        'close': [100.0] * 100,
        'high': [105.0] * 100,
        'low': [95.0] * 100,
        'volume': [1000] * 100
    }
    df = pd.DataFrame(data)
    
    # Run features
    fe = FeatureEngineer()
    df_new = fe.add_technical_indicators(df)
    
    # Check columns exist
    expected_cols = ['rsi', 'macd', 'bb_upper', 'sma_20']
    for col in expected_cols:
        assert col in df_new.columns
    
    # Check no NaNs
    assert not df_new.isnull().values.any()
