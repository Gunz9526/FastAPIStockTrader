import json

import numpy as np
import pandas as pd
import pytest
import xgboost

from stock_trader.clock import utcnow
from stock_trader.features import FEATURES
from stock_trader.modeling import FAMILIES, NativeBundle, make_regressor, save_models
from stock_trader.research import ActiveModel, model_contract


@pytest.mark.parametrize("family", [*FAMILIES, "ensemble"])
def test_actual_cpu_models_native_round_trip_and_average(settings, family):
    random = np.random.default_rng(42)
    frame = pd.DataFrame(random.normal(size=(500, len(FEATURES))), columns=FEATURES)
    target = frame["ret_3"] * 0.01 + frame["relative_strength"] * 0.003
    families = FAMILIES if family == "ensemble" else (family,)
    models = {}
    expected = np.zeros(len(frame))
    for backend in families:
        model = make_regressor(backend, 2)
        model.fit(frame, target)
        models[backend] = model
        expected += model.predict(frame) / len(families)
    directory = settings.artifact_dir / "native-test"
    directory.mkdir(parents=True)
    members = save_models(models, directory)
    weights = [1 / len(members)] * len(members)
    restored = NativeBundle(directory, members, weights, 2)
    np.testing.assert_allclose(restored.predict(frame), expected, rtol=1e-6, atol=1e-8)
    metadata = {
        "format_version": 2,
        "version": "native-test",
        "family": family,
        "created_at": utcnow().isoformat(),
        "accepted": True,
        "members": members,
        "weights": weights,
        "contract": model_contract(settings),
        "threshold": 0.003,
        "horizon": 12,
    }
    (directory / "metadata.json").write_text(json.dumps(metadata))
    (settings.artifact_dir / "current").write_text("native-test")
    active = ActiveModel(settings)
    active.refresh()
    np.testing.assert_allclose(active.predict(frame), expected, rtol=1e-6, atol=1e-8)
    # Validation is repeated on refresh even when the model is already cached.
    (directory / members[0]["file"]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="checksum"):
        active.refresh()


def test_unknown_backend_and_nonconvex_weights_rejected(settings):
    with pytest.raises(ValueError, match="family"):
        make_regressor("unknown", 2)
    with pytest.raises(ValueError, match="weights"):
        NativeBundle(settings.artifact_dir, [{"family": "lgbm", "file": "x.txt"}], [-1.0], 2)


def test_xgboost_build_is_cpu_only():
    assert xgboost.build_info()["USE_CUDA"] is False
