import numpy as np
import pandas as pd

from stock_trader.event_diagnostics import prediction_summary


def test_unknown_outcomes_are_counted_and_never_imputed_as_zero():
    frame = pd.DataFrame(
        {"label_target": [0.02, -0.01, np.nan], "label_status": ["closed", "closed", "censored_end"]}
    )
    result = prediction_summary(frame, pd.Series([0.015, 0.006, 0.03]))
    assert result["known_completed_events"] == 2 and result["predicted_signals"] == 3
    assert result["actual_gross_mean_completed"] == 0.005
    assert result["thresholds"]["0.01"]["unknown_events"] == 1
    assert result["thresholds"]["0.01"]["completed_mean_gross_return"] == 0.02
    assert result["spearman_completed"] > 0.99 and result["censoring_corrected"] is False


def test_empty_or_constant_predictions_do_not_emit_nan_correlations():
    frame = pd.DataFrame({"label_target": [0.02, -0.01], "label_status": ["closed", "closed"]})
    constant = prediction_summary(frame, pd.Series([0.01, 0.01]))
    assert constant["pearson_completed"] is None and constant["spearman_completed"] is None
    empty = prediction_summary(frame, pd.Series([np.nan, np.nan]))
    assert empty["known_completed_events"] == 0 and empty["actual_gross_mean_completed"] is None
