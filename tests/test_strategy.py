import pytest
import pandas as pd
from app.ml.features import FeatureEngineer
from app.services.risk_manager import RiskManager

def test_risk_manager():
    rm = RiskManager(max_position_size_pct=0.1, stop_loss_pct=0.02)
    
    # 1. Normal Buy
    # Balance 100,000 -> Max Pos 10,000. Price 100 -> Qty 100.
    allowed, qty = rm.check_buy_signal("AAPL", 100.0, 100000.0)
    assert allowed is True
    assert qty == 100

    # 2. Insufficient Funds (Price > Max Pos)
    # Max Pos 10,000. Price 11,000. Qty 0.
    allowed, qty = rm.check_buy_signal("BRK.A", 11000.0, 100000.0)
    assert allowed is False
    assert qty == 0

    # 3. Exit Prices
    sl, tp = rm.get_exit_prices(100.0)
    assert sl == 98.0 # 2% down
    assert tp == 105.0 # 5% up (default)

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
