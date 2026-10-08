import numpy as np
import pandas as pd
import pytest

from stock_trader.swing_followup import validate_prediction_keys


def test_frozen_oof_alignment_preserves_unpredicted_training_rows():
    frame = pd.DataFrame(
        {
            "date": ["2025-11-06", "2025-11-07", "2026-06-15"],
            "symbol": ["AAPL"] * 3,
            "feature_valid": [True, True, False],
        }
    )
    saved = frame.iloc[1:].copy()
    saved["xgb"] = [0.01, np.nan]
    result = validate_prediction_keys(saved, frame, "2026-06-16")
    assert pd.isna(result.iloc[0]) and result.iloc[1] == 0.01 and pd.isna(result.iloc[2])


def test_changed_or_future_prediction_keys_are_rejected():
    frame = pd.DataFrame(
        {"date": ["2025-11-07", "2026-06-15"], "symbol": ["AAPL"] * 2, "feature_valid": True}
    )
    saved = frame.copy()
    saved["xgb"] = 0.01
    with pytest.raises(ValueError, match="prediction keys"):
        validate_prediction_keys(saved.iloc[::-1], frame, "2026-06-16")
    saved.loc[1, "date"] = "2026-06-16"
    with pytest.raises(ValueError, match="boundary"):
        validate_prediction_keys(saved, frame, "2026-06-16")
