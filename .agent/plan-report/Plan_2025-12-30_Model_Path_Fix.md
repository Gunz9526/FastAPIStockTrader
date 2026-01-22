# Model Persistence Fix Plan

## Goal
Ensure `model_artifacts` are correctly saved and persisted on the server by fixing path ambiguity and permissions.

## User Constraints
- **Remote Server Environment**: Cannot run local commands or expect local file visibility immediately without volume sync.
- **Docker Only**: All execution happens inside Docker.
- **Path Confusion**: `model_artifacts` vs `app/ml/model_artifacts`.

## Solutions

### 1. Unify Model Path
- **Current**: `_model_path = "model_artifacts/ensemble_model.pkl"` (Relative).
- **Fix**: Change to absolute path `/app/model_artifacts/ensemble_model.pkl`. This guarantees it lands in the volume-mounted directory (`.:/app`).

### 2. Fix Permissions
- User suggested `777`. Since it's a dev/small-scale server, this is acceptable for now.
- **Action**: Add a setup step in `app/main.py` or `worker` startup to `os.chmod` the directory, or do it in the Dockerfile.
- **Immediate Fix**: Ensure `os.makedirs(..., mode=0o777)` is used in Python code.

### 3. Ensemble vs Individual Models
- **User Question**: "Is ensemble result sufficient?"
- **Answer**: For prediction, yes. But for debugging/analysis, saving individual models is better.
- **Plan**: Update `EnsembleWrapper.save` to save sub-models (CatBoost, XGB, LGBM) into separate files within the directory.

## Changes

### `app/ml/predictor.py`
- [MODIFY] Change `_model_path` to `/app/model_artifacts/ensemble_model.pkl`.
- [MODIFY] Ensure directory creation uses `exist_ok=True` and sets permissions.

### `app/ml/models.py`
- [MODIFY] Update `EnsembleWrapper.save` to verify it dumps the *entire* voting regressor (which includes sub-estimators). `joblib.dump` on the `VotingRegressor` object *does* save everything, so separate files aren't strictly necessary for persistence, but might help analysis. We will stick to dumping the main object for now to solve the "missing file" issue first.

## Verification
- User will verify file existence on their server after next training run.
