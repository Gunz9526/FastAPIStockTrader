import numpy as np
import pandas as pd

from stock_trader.entry_study import training_mask


def test_training_mask_matches_entry_gate_and_excludes_validation_and_benchmarks():
    frame = pd.DataFrame(
        {
            "date": ["2026-01-01"] * 5 + ["2026-01-02"],
            "feature_valid": [True] * 6,
            "symbol": ["AAPL", "AAPL", "AAPL", "SPY", "AAPL", "AAPL"],
            "ma_distance": [0.01, -0.01, 0.01, 0.01, 0.01, 0.01],
            "relative_strength": [0.01, 0.01, -0.01, 0.01, 0.01, 0.01],
        }
    )
    target = pd.Series([0.01, 0.01, 0.01, 0.01, np.nan, 0.01])
    assert training_mask(frame, target, "2026-01-01", ("SPY", "QQQ")).tolist() == [
        True,
        False,
        False,
        False,
        False,
        False,
    ]
