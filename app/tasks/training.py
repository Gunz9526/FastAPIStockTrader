import json
import logging
import os
from datetime import datetime, timedelta

import numpy as np
import optuna
import pandas as pd
from sklearn.metrics import accuracy_score, classification_report, f1_score
from sklearn.model_selection import TimeSeriesSplit

from app.core.database import SessionLocal
from app.ml.features import FeatureEngineer
from app.ml.models import (
    CLASS_NAMES,
    CatBoostClassifierWrapper,
    EnsembleClassifierWrapper,
    LGBMClassifierWrapper,
    XGBoostClassifierWrapper,
)
from app.repositories.stock_repo_sync import SyncStockRepository
from app.services.discord_notifier import notify_on_failure
from app.services.regime import MarketRegime, RegimeDetector
from app.worker import celery_app

logger = logging.getLogger(__name__)

# Configuration
LOOKBACK_YEARS = 2
VALIDATION_DAYS = 30
MODEL_SAVE_PATH = "model_artifacts"

# Ternary classification threshold (daily return)
# Returns above +THRESHOLD → UP(2), below -THRESHOLD → DOWN(0), else NEUTRAL(1)
CLASSIFICATION_THRESHOLD = 0.005  # ±0.5% daily — wider NEUTRAL band to reduce noise trades

# Regime-specific class weight overrides
# Addresses: UP over-prediction (all regimes), NEUTRAL collapse (bear_trending)
REGIME_CLASS_WEIGHTS: dict[str, dict[int, float]] = {
    "bull_trending":      {0: 1.3, 1: 1.2, 2: 1.0},  # Reduce UP bias, slightly boost NEUTRAL
    "bear_trending":      {0: 1.0, 1: 1.5, 2: 1.3},  # Strongly boost NEUTRAL (was 7/64 = 11%)
    "sideways_volatile":  {0: 1.2, 1: 1.3, 2: 1.0},  # Reduce UP, boost NEUTRAL
    "sideways_calm":      {0: 1.2, 1: 1.3, 2: 1.0},  # Reduce UP (2775 vs 1739), boost NEUTRAL
}


def _load_and_prepare_data(
    repo: SyncStockRepository,
    feature_engineer: FeatureEngineer,
    symbols: list[str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    symbol_limit: int = None,
    classify_regime: bool = True
) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """
    Shared data loading and preparation function.
    
    Args:
        repo: Stock repository
        feature_engineer: Feature engineering instance
        symbols: List of stock symbols
        start_date: Start date for data collection
        end_date: End date for data collection
        symbol_limit: Maximum number of symbols to process (None = all)
        classify_regime: If True, add regime classification (Phase H.3)
    
    Returns:
        Tuple of (features_df, target_series, successful_symbols)
    """
    all_X, all_y = [], []
    successful_symbols = []

    symbol_subset = symbols[:symbol_limit] if symbol_limit else symbols

    for symbol in symbol_subset:
        try:
            ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date)
            if len(ohlcv) < 100:
                logger.warning(f"{symbol}: Insufficient data ({len(ohlcv)} bars)")
                continue

            df = pd.DataFrame([{
                'date_time': bar.date_time,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume,
                'vwap': bar.vwap if hasattr(bar, 'vwap') else None,
                'trade_count': bar.trade_count if hasattr(bar, 'trade_count') else None
            } for bar in ohlcv])
            df.set_index('date_time', inplace=True)
            df.sort_index(inplace=True)

            # Add symbol column BEFORE feature engineering (needed for sector_id)
            df['symbol'] = symbol

            # Feature engineering
            features_df = feature_engineer.create_features(df)
            if features_df.empty:
                logger.warning(f"{symbol}: Feature engineering failed")
                continue

            # Target: Ternary classification (daily return)
            # 0=DOWN (< -0.3%), 1=NEUTRAL (-0.3% ~ +0.3%), 2=UP (> +0.3%)
            next_return = features_df['close'].pct_change().shift(-1)
            features_df['target'] = np.where(
                next_return > CLASSIFICATION_THRESHOLD, 2,   # UP
                np.where(next_return < -CLASSIFICATION_THRESHOLD, 0, 1)  # DOWN / NEUTRAL
            )
            features_df.dropna(inplace=True)
            features_df['target'] = features_df['target'].astype(int)

            # Add relative_volume (market-relative volume)
            # Note: Using symbol-level average as approximation for market average
            if 'volume' in features_df.columns:
                market_avg_volume = features_df['volume'].mean()
                features_df['relative_volume'] = features_df['volume'] / market_avg_volume
            else:
                features_df['relative_volume'] = 1.0

            # Verify all required features are present
            missing_features = [f for f in feature_engineer.base_feature_columns if f not in features_df.columns]
            if missing_features:
                logger.error(f"{symbol}: Missing features: {missing_features}")
                continue

            all_X.append(features_df[feature_engineer.base_feature_columns])
            all_y.append(features_df['target'])
            successful_symbols.append(symbol)

            logger.info(f"{symbol}: {len(features_df)} samples loaded")

        except Exception as e:
            logger.error(f"Failed to load {symbol}: {e}")
            continue

    if not all_X:
        logger.error("No data loaded for any symbol")
        return pd.DataFrame(), pd.Series(), []

    # Preserve datetime index instead of resetting to integer
    X = pd.concat(all_X, ignore_index=False)
    y = pd.concat(all_y, ignore_index=False)

    # Sort by index to maintain chronological order
    X = X.sort_index()
    y = y.sort_index()

    if classify_regime:
        logger.info("데이터를 시장 레짐별로 분류 중...")
        regime_detector = RegimeDetector()

        # SPY 데이터 로드 (regime classification용)
        try:
            # SPY 일봉 데이터 확인
            spy_ohlcv = repo.get_ohlcv_range('SPY', start_date, end_date, timeframe='1d')

            # SPY 데이터 부족 시 경고
            if len(spy_ohlcv) < 100:
                logger.warning(
                    "SPY 일봉 데이터 부족 (%d bars). SPY를 symbol_subset에 추가하여 백필하세요.",
                    len(spy_ohlcv)
                )

                # 그래도 데이터가 부족하면 기본 레짐 사용
                if len(spy_ohlcv) < 50:
                    logger.warning("SPY 데이터 극도로 부족. 모든 데이터를 SIDEWAYS_CALM으로 분류합니다.")
                    X['regime'] = MarketRegime.SIDEWAYS_CALM.value
                    return X, y, successful_symbols

            # SPY DataFrame 생성
            spy_df = pd.DataFrame([{
                'symbol': bar.symbol,
                'date_time': bar.date_time,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume
            } for bar in spy_ohlcv])
            spy_df.set_index('date_time', inplace=True)
            spy_df.sort_index(inplace=True)
            spy_df['symbol'] = 'SPY'

            # SPY 피처 생성 (레짐 감지용)
            spy_features = feature_engineer.create_features(spy_df)

            # Load VIX data from Redis cache for regime detection
            vix_value = None
            try:
                from app.tasks.vix_data import get_latest_vix
                vix_value = get_latest_vix()
                if vix_value is not None:
                    logger.info("VIX 캐시에서 로드: %.2f", vix_value)
                else:
                    logger.warning("VIX 캐시 없음 (ATR 기반 레짐 감지 사용)")
            except Exception as e:
                logger.debug("VIX 로드 실패: %s", e)

            # 시계열별로 rolling window regime 분류 (FIX: 전체 기간 한 번에 분류하던 문제 해결)
            if len(spy_features) >= 50:
                logger.info("시계열별 rolling window regime 분류 시작...")

                # Pre-compute regime for each SPY bar (vectorized approach)
                # Instead of O(N*M) per-sample detection, compute regime per SPY bar: O(M)
                spy_regimes = []
                spy_timestamps = spy_features.index.tolist()
                for i in range(len(spy_features)):
                    if i >= 49:  # Need at least 50 bars
                        window = spy_features.iloc[max(0, i - 199):i + 1]
                        regime = regime_detector.detect_regime(window, vix_value=vix_value)
                        spy_regimes.append(regime.value)
                    else:
                        spy_regimes.append(MarketRegime.SIDEWAYS_CALM.value)

                logger.info("SPY 레짐 사전 계산 완료: %d개 바", len(spy_regimes))

                # Build SPY regime Series with datetime index
                spy_regime_series = pd.DataFrame({
                    'timestamp': spy_timestamps,
                    'regime': spy_regimes
                }).set_index('timestamp').sort_index()

                # Map training samples to nearest SPY regime via asof merge
                X_temp = pd.DataFrame({'timestamp': X.index}).sort_values('timestamp')
                merged = pd.merge_asof(
                    X_temp,
                    spy_regime_series.reset_index(),
                    on='timestamp',
                    direction='backward'
                )
                # Restore original index order
                merged.index = X_temp.index
                X['regime'] = merged['regime'].fillna(MarketRegime.SIDEWAYS_CALM.value).values

                regime_dist = X['regime'].value_counts().to_dict()
                logger.info("벡터화 regime 분류 완료: %d개 샘플, 분포: %s", len(X), regime_dist)
            else:
                logger.warning("SPY features 부족 (%d개), SIDEWAYS_CALM으로 설정", len(spy_features))
                X['regime'] = MarketRegime.SIDEWAYS_CALM.value

            regime_dist = pd.Series(X['regime']).value_counts().to_dict()
            logger.info("레짐 분포: %s", regime_dist)

        except (ValueError, KeyError, AttributeError) as e:
            logger.warning("레짐 분류 실패: %s. 레짐 없이 진행합니다.", str(e))
            X['regime'] = MarketRegime.SIDEWAYS_CALM.value  # Fallback

    logger.info("총 %d개 샘플, %d개 심볼로부터 데이터 로드 완료", len(X), len(successful_symbols))
    return X, y, successful_symbols


def _save_training_report(regime_results: dict, X: pd.DataFrame) -> str:
    """
    Save training results to a text file for model performance evaluation.
    
    Args:
        regime_results: Dict of regime -> metrics
        X: Feature DataFrame with regime column
    
    Returns:
        Path to saved report file
    """
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    report_path = f"{MODEL_SAVE_PATH}/training_report_{timestamp}.txt"
    
    try:
        os.makedirs(MODEL_SAVE_PATH, exist_ok=True)
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write("=" * 70 + "\n")
            f.write("MODEL TRAINING REPORT\n")
            f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 70 + "\n\n")
            
            # Summary table
            f.write("REGIME PERFORMANCE SUMMARY\n")
            f.write("-" * 90 + "\n")
            f.write(f"{'Regime':<20} {'Samples':<10} {'Accuracy':<12} {'F1':<10} {'NEUTRAL_R':<12} {'Status':<15}\n")
            f.write("-" * 90 + "\n")
            
            for regime, metrics in regime_results.items():
                samples = metrics.get('samples', 'N/A')
                accuracy = metrics.get('accuracy', 'N/A')
                f1_val = metrics.get('f1_score', 'N/A')
                neutral_r = metrics.get('neutral_recall', 'N/A')
                status = metrics.get('status', 'unknown')
                
                acc_str = f"{accuracy:.2%}" if isinstance(accuracy, float) else str(accuracy)
                f1_str = f"{f1_val:.4f}" if isinstance(f1_val, float) else str(f1_val)
                nr_str = f"{neutral_r:.2%}" if isinstance(neutral_r, float) else str(neutral_r)
                
                f.write(f"{regime:<20} {samples:<10} {acc_str:<12} {f1_str:<10} {nr_str:<12} {status:<15}\n")
            
            f.write("-" * 90 + "\n\n")
            
            # Data distribution
            f.write("DATA DISTRIBUTION BY REGIME\n")
            f.write("-" * 70 + "\n")
            if 'regime' in X.columns:
                regime_counts = X['regime'].value_counts()
                total_samples = len(X)
                for regime, count in regime_counts.items():
                    pct = count / total_samples * 100
                    f.write(f"{regime}: {count:,} samples ({pct:.1f}%)\n")
            f.write(f"\nTotal samples: {len(X):,}\n")
            f.write("-" * 70 + "\n\n")
            
            # Detailed metrics for each regime
            f.write("DETAILED METRICS\n")
            f.write("-" * 70 + "\n")
            for regime, metrics in regime_results.items():
                f.write(f"\n[{regime}]\n")
                for key, value in metrics.items():
                    if key in ('classification_report', 'feature_importance_top10'):
                        continue  # Written separately below
                    if isinstance(value, float):
                        f.write(f"  {key}: {value:.4f}\n")
                    else:
                        f.write(f"  {key}: {value}\n")
                
                # Classification report (per-class precision/recall/f1)
                cls_report = metrics.get('classification_report')
                if cls_report:
                    f.write(f"\n  Classification Report:\n")
                    for line in cls_report.strip().split('\n'):
                        f.write(f"    {line}\n")
                
                # Feature importance
                feat_imp = metrics.get('feature_importance_top10')
                if feat_imp:
                    f.write(f"\n  Top-10 Feature Importance:\n")
                    for fname, fval in feat_imp:
                        f.write(f"    {fname:<25} {fval:.4f}\n")
            
            f.write("\n" + "=" * 70 + "\n")
            f.write("END OF REPORT\n")
            f.write("=" * 70 + "\n")
        
        logger.info(f"Training report saved to: {report_path}")
        return report_path
        
    except Exception as e:
        logger.error(f"Failed to save training report: {e}")
        return ""


@celery_app.task(name="app.tasks.training.train_models", bind=True, max_retries=3)
@notify_on_failure("train_models")
def train_models(self):
    """
    Model training task with Walk-Forward validation.
    Uses shared data loading and robust multi-period validation.
    """
    logger.info("워크포워드 검증을 사용한 모델 학습 시작...")

    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)
        feature_engineer = FeatureEngineer()

        # 1. Load active symbols
        symbols = repo.get_active_symbols()
        if not symbols:
            logger.warning("활성 심볼이 없습니다")
            return

        logger.info(f"{len(symbols)}개 심볼로 학습을 시작합니다")

        # 2. Load and prepare data using shared function
        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - timedelta(days=LOOKBACK_YEARS * 365)

        X, y, successful_symbols = _load_and_prepare_data(
            repo, feature_engineer, symbols, start_date, end_date, symbol_limit=None, classify_regime=True
        )

        if X.empty:
            logger.error("학습용 데이터를 수집하지 못했습니다")
            return

        # Data size validation (daily bars: 일봉 기준 300개 ≈ 1.2년)
        if len(X) < 300:
            logger.warning(f"데이터셋이 작습니다: {len(X)} 샘플. 더 긴 백필 또는 심볼 확대를 고려하세요.")

        logger.info(f"총 데이터: {len(X)} 샘플, {len(successful_symbols)}개 심볼로부터")

        # Phase H.3: 레짐별 모델 학습
        has_regime = 'regime' in X.columns

        if has_regime:
            logger.info("레짐별 모델 학습")
            regime_results = _train_regime_specific_models(feature_engineer, X, y)

            # Save training report
            _save_training_report(regime_results, X)

            session.commit()
            logger.info("학습 완료 - 레짐별 모델이 저장되었습니다")
        else:
            logger.info("No regime classification, training generic model")
            # Fallback to old training logic (temporarily disabled)
            logger.warning("Generic model training not implemented. Enable classify_regime=True")

    except Exception as e:
        logger.error(f"Training error: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


def _load_regime_params(regime_value: str) -> dict:
    """Load best hyperparameters for a specific regime.

    Search order:
    1. ``best_params_{regime_value}.json`` (regime-specific file from tune_models)
    2. ``best_params.json`` → ``regime_specific[regime_value]`` (combined file)
    3. ``best_params.json`` → ``default`` (combined file fallback)
    4. ``best_params.json`` top-level keys (legacy global format)
    5. Empty dict (will use model defaults)

    Args:
        regime_value: Regime name string (e.g., 'bull_trending').

    Returns:
        Dict with 'catboost', 'lgbm', 'xgboost' keys.
    """
    model_keys = ('catboost', 'lgbm', 'xgboost')

    # 1. Try regime-specific file
    regime_path = f"{MODEL_SAVE_PATH}/best_params_{regime_value}.json"
    if os.path.exists(regime_path):
        try:
            with open(regime_path) as f:
                data = json.load(f)
            # Regime file may have extra keys like 'regime', 'samples', 'tuned_at'
            params = {k: v for k, v in data.items() if k in model_keys}
            if params:
                logger.info("Loaded regime-specific params from %s", regime_path)
                return params
        except Exception as e:
            logger.warning("Failed to load %s: %s", regime_path, e)

    # 2-4. Try combined file
    combined_path = f"{MODEL_SAVE_PATH}/best_params.json"
    if os.path.exists(combined_path):
        try:
            with open(combined_path) as f:
                data = json.load(f)

            # 2. Check regime_specific section
            regime_specific = data.get("regime_specific", {})
            if regime_value in regime_specific and regime_specific[regime_value]:
                logger.info("Loaded params from best_params.json[regime_specific][%s]", regime_value)
                return regime_specific[regime_value]

            # 3. Use default section
            default_params = data.get("default")
            if default_params and isinstance(default_params, dict):
                logger.info("Loaded default params from best_params.json for %s", regime_value)
                return default_params

            # 4. Legacy format: top-level catboost/lgbm/xgboost keys
            if "catboost" in data:
                logger.info("Loaded legacy params from best_params.json for %s", regime_value)
                return {k: v for k, v in data.items() if k in model_keys}

        except Exception as e:
            logger.warning("Failed to load %s: %s", combined_path, e)

    # 5. No params found
    logger.info("No tuned params found for %s, using model defaults", regime_value)
    return {}


def _train_regime_specific_models(
    feature_engineer: FeatureEngineer,
    X: pd.DataFrame,
    y: pd.Series,
) -> dict[str, dict]:
    """Train 4 regime-specific ensemble classifier models with holdout validation.

    For each regime:
    1. Split data into 80% train / 20% holdout (chronological).
    2. Walk-Forward weight calculation on the **train** portion only.
    3. Train a *validation* ensemble on train → evaluate on holdout (true OOS).
    4. Train a *production* ensemble on **all** regime data → save to disk.

    Uses EnsembleClassifierWrapper (VotingClassifier with soft voting)
    for ternary classification (0=DOWN, 1=NEUTRAL, 2=UP).

    Args:
        feature_engineer: Feature engineering instance.
        X: Feature DataFrame (with 'regime' column).
        y: Target Series (ternary: 0/1/2).

    Returns:
        Dict of ``regime_value`` → validation result dict.
    """
    holdout_results: dict[str, dict] = {}
    holdout_ratio = 0.2

    for regime in MarketRegime:
        regime_value = regime.value
        logger.info(f"\n{'='*60}")
        logger.info(f"Training {regime_value.upper()} regime classifier model")
        logger.info(f"{'='*60}")

        # Filter data for this regime
        regime_mask = X['regime'] == regime_value
        X_regime = X[regime_mask].drop(columns=['regime'])
        y_regime = y[regime_mask]

        logger.info(f"Regime data: {len(X_regime)} samples ({len(X_regime)/len(X)*100:.1f}% of total)")

        # Minimum data requirement (daily bars: 500 ≈ 2 years)
        min_samples = 500
        if len(X_regime) < min_samples:
            logger.warning(f"Insufficient {regime_value} data: {len(X_regime)} < {min_samples} samples")
            logger.warning(f"Skipping {regime_value} model training (will use generic fallback)")
            holdout_results[regime_value] = {
                'samples': len(X_regime),
                'status': 'insufficient_data',
            }
            continue

        # Log class distribution
        class_dist = y_regime.value_counts().to_dict()
        logger.info(f"  Class distribution: {class_dist}")

        # Load regime-specific tuning params
        regime_params = _load_regime_params(regime_value)

        # Inject regime-specific class weights (override defaults)
        regime_weights = REGIME_CLASS_WEIGHTS.get(regime_value)
        if regime_weights:
            regime_params.setdefault('catboost', {})
            regime_params.setdefault('lgbm', {})
            regime_params.setdefault('xgboost', {})
            # CatBoost uses list format: [weight_class_0, weight_class_1, weight_class_2]
            regime_params['catboost']['class_weights'] = [
                regime_weights[i] for i in range(len(CLASS_NAMES))
            ]
            # LightGBM uses dict format: {class_label: weight}
            regime_params['lgbm']['class_weight'] = regime_weights.copy()
            # XGBoost uses dict format: {class_label: weight} (converted to sample_weight internally)
            regime_params['xgboost']['class_weight'] = regime_weights.copy()
            logger.info(f"  Regime class weights: {regime_weights}")

        # --- 1. Holdout split (chronological) ---
        split_idx = int(len(X_regime) * (1 - holdout_ratio))
        X_train = X_regime.iloc[:split_idx]
        y_train = y_regime.iloc[:split_idx]
        X_holdout = X_regime.iloc[split_idx:]
        y_holdout = y_regime.iloc[split_idx:]

        market_avg_volume_train = (
            X_train['volume'].mean() if 'volume' in X_train.columns else None
        )

        # --- 2. Walk-Forward weight calculation on TRAIN portion only ---
        models_to_eval = [
            ('catboost', CatBoostClassifierWrapper()),
            ('lgbm', LGBMClassifierWrapper()),
            ('xgboost', XGBoostClassifierWrapper()),
        ]

        accuracy_scores: list[float] = []
        for name, model in models_to_eval:
            try:
                logger.info(f"  Walk-Forward classifier {name}...")
                tscv = TimeSeriesSplit(n_splits=3)
                scores: list[float] = []
                for train_idx, val_idx in tscv.split(X_train):
                    X_tr_raw = X_train.iloc[train_idx]
                    X_val_raw = X_train.iloc[val_idx]
                    y_tr = y_train.iloc[train_idx]
                    y_val = y_train.iloc[val_idx]

                    X_tr = feature_engineer.extract_feature_vector(
                        X_tr_raw, fit_scaler=True,
                        market_avg_volume=market_avg_volume_train,
                        feature_set="base", scaler_suffix=regime_value,
                    )
                    X_val = feature_engineer.extract_feature_vector(
                        X_val_raw, fit_scaler=False,
                        market_avg_volume=market_avg_volume_train,
                        feature_set="base", scaler_suffix=regime_value,
                    )

                    model.train(X_tr, y_tr)
                    pred = model.predict(X_val)
                    acc = accuracy_score(y_val.values, pred)
                    scores.append(acc)

                avg_acc = sum(scores) / len(scores) if scores else 0.0
                accuracy_scores.append(max(avg_acc, 0.1))
                logger.info(f"  {name} | Accuracy: {avg_acc:.4f}")
            except Exception as e:
                logger.error(f"  {name} 처리 실패: {e}", exc_info=True)
                accuracy_scores.append(0.1)

        total = sum(accuracy_scores)
        weights = [s / total for s in accuracy_scores] if total > 0 else [0.33, 0.33, 0.34]
        logger.info(f"  Ensemble weights: {[round(w, 3) for w in weights]}")

        # --- 3. Validation ensemble (train → holdout, true OOS) ---
        try:
            X_train_scaled = feature_engineer.extract_feature_vector(
                X_train, fit_scaler=True,
                market_avg_volume=market_avg_volume_train,
                feature_set="base", scaler_suffix=regime_value,
            )
            X_holdout_scaled = feature_engineer.extract_feature_vector(
                X_holdout, fit_scaler=False,
                market_avg_volume=market_avg_volume_train,
                feature_set="base", scaler_suffix=regime_value,
            )

            val_ensemble = EnsembleClassifierWrapper(weights=weights, model_params=regime_params)
            val_ensemble.train(X_train_scaled, y_train)

            predictions = val_ensemble.predict(X_holdout_scaled)
            accuracy = accuracy_score(y_holdout.values, predictions)
            f1 = f1_score(y_holdout.values, predictions, average='weighted', zero_division=0)

            # Per-class metrics
            cls_report_str = classification_report(
                y_holdout.values, predictions,
                target_names=CLASS_NAMES, zero_division=0,
            )
            logger.info(f"\n{cls_report_str}")

            # NEUTRAL recall (class 1)
            neutral_mask = y_holdout.values == 1
            neutral_correct = (predictions[neutral_mask] == 1).sum() if neutral_mask.any() else 0
            neutral_total = neutral_mask.sum()
            neutral_recall = neutral_correct / neutral_total if neutral_total > 0 else 0.0

            logger.info(f"{regime_value} 모델 검증 (True Out-of-Sample):")
            logger.info(f"  - 학습: {len(X_train)} 샘플, 검증: {len(X_holdout)} 샘플")
            logger.info(f"  - 정확도: {accuracy:.2%}")
            logger.info(f"  - F1-Score (weighted): {f1:.4f}")
            logger.info(f"  - 예측 분포: {pd.Series(predictions).value_counts().to_dict()}")
            logger.info(f"  - 실제 분포: {y_holdout.value_counts().to_dict()}")

            holdout_results[regime_value] = {
                'samples': len(X_regime),
                'train_samples': len(X_train),
                'validation_samples': len(X_holdout),
                'accuracy': accuracy,
                'f1_score': f1,
                'neutral_recall': neutral_recall,
                'classification_report': cls_report_str,
                'pred_distribution': pd.Series(predictions).value_counts().to_dict(),
                'actual_distribution': y_holdout.value_counts().to_dict(),
                'status': 'success',
            }
        except Exception as e:
            logger.error(f"{regime_value} 검증 앙상블 학습 실패: {e}", exc_info=True)
            holdout_results[regime_value] = {
                'samples': len(X_regime),
                'status': 'error',
                'error': str(e),
            }

        # --- 4. Production ensemble (ALL data → save) ---
        try:
            market_avg_volume = (
                X_regime['volume'].mean() if 'volume' in X_regime.columns else None
            )
            X_regime_scaled = feature_engineer.extract_feature_vector(
                X_regime, fit_scaler=True, market_avg_volume=market_avg_volume,
                feature_set="base", scaler_suffix=regime_value,
            )
            production_ensemble = EnsembleClassifierWrapper(weights=weights, model_params=regime_params)
            production_ensemble.train(X_regime_scaled, y_regime)

            # Log feature importance (top 10)
            try:
                feature_names = list(X_regime_scaled.columns) if hasattr(X_regime_scaled, 'columns') else [f"f{i}" for i in range(X_regime_scaled.shape[1])]
                importances = {}
                for name, estimator in production_ensemble.model.named_estimators_.items():
                    if hasattr(estimator, 'feature_importances_'):
                        imp = estimator.feature_importances_
                        if len(imp) == len(feature_names):
                            for fn, iv in zip(feature_names, imp):
                                importances[fn] = importances.get(fn, 0.0) + iv / 3.0
                if importances:
                    sorted_imp = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:10]
                    logger.info(f"  {regime_value} Top-10 features: {sorted_imp}")
                    holdout_results[regime_value]['feature_importance_top10'] = sorted_imp
            except Exception as e:
                logger.debug(f"Feature importance extraction failed: {e}")

            model_filename = f"ensemble_classifier_{regime_value}.pkl"
            model_path = os.path.join(MODEL_SAVE_PATH, model_filename)
            production_ensemble.save(model_path)

            logger.info(f"{regime_value.upper()} production classifier saved: {model_filename}")
        except Exception as e:
            logger.error(f"{regime_value} 프로덕션 모델 학습 실패: {e}", exc_info=True)

    logger.info(f"\n{'='*60}")
    logger.info("Regime-specific classifier training complete")
    logger.info(f"{'='*60}\n")

    return holdout_results


@celery_app.task(name="app.tasks.training.tune_models", bind=True, max_retries=2)
@notify_on_failure("tune_models")
def tune_models(self):
    """
    Hyperparameter tuning task using Optuna.
    Performs regime-specific tuning for better performance in each market condition.
    
    Strategy:
    1. Load data and classify by regime
    2. Tune for each regime separately (4 tuning runs)
    3. Save regime-specific best params to best_params_{regime}.json
    4. Also save combined best_params.json for backward compatibility
    """
    logger.info("Starting regime-specific hyperparameter tuning with Optuna...")

    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)
        feature_engineer = FeatureEngineer()

        symbols = repo.get_active_symbols()
        if not symbols:
            logger.warning("No symbols found")
            return

        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - timedelta(days=LOOKBACK_YEARS * 365)

        # Load data with regime classification
        X, y, successful_symbols = _load_and_prepare_data(
            repo, feature_engineer, symbols, start_date, end_date,
            symbol_limit=None,  # Use all active symbols
            classify_regime=True  # Enable regime classification
        )

        if X.empty:
            logger.error("No tuning data available")
            return

        logger.info(f"Tuning data: {len(X)} samples from {len(successful_symbols)} symbols")

        # Check if regime column exists
        has_regime = 'regime' in X.columns
        if not has_regime:
            logger.warning("No regime classification found. Falling back to global tuning.")
            # Fallback to old behavior (tune on all data)
            return _tune_models_global(X, y, feature_engineer)

        # Regime-specific tuning
        logger.info("\n" + "="*60)
        logger.info("REGIME-SPECIFIC HYPERPARAMETER TUNING")
        logger.info("="*60)

        regime_dist = X['regime'].value_counts().to_dict()
        logger.info("Regime distribution: %s", regime_dist)

        all_regime_params = {}

        for regime in MarketRegime:
            regime_value = regime.value
            logger.info(f"\n{'='*60}")
            logger.info(f"Tuning for {regime_value.upper()} regime")
            logger.info(f"{'='*60}")

            # Filter data for this regime
            regime_mask = X['regime'] == regime_value
            X_regime = X[regime_mask].drop(columns=['regime'])
            y_regime = y[regime_mask]

            logger.info(f"Regime data: {len(X_regime)} samples ({len(X_regime)/len(X)*100:.1f}% of total)")

            # Skip if insufficient data (use 500 as minimum for tuning)
            if len(X_regime) < 500:
                logger.warning(f"Insufficient {regime_value} data for tuning: {len(X_regime)} < 500")
                logger.warning(f"Skipping {regime_value} tuning (will use generic params)")
                all_regime_params[regime_value] = None
                continue

            # Scale features
            market_avg_volume = X_regime['volume'].mean() if 'volume' in X_regime.columns else None
            X_regime_scaled = feature_engineer.extract_feature_vector(
                X_regime, fit_scaler=True, market_avg_volume=market_avg_volume,
                feature_set="base", scaler_suffix=regime_value
            )

            # Tune for this regime
            regime_params = _tune_regime_models(
                X_regime_scaled, y_regime, regime_value
            )
            all_regime_params[regime_value] = regime_params

            # Save regime-specific params
            regime_config_path = f"{MODEL_SAVE_PATH}/best_params_{regime_value}.json"
            os.makedirs(MODEL_SAVE_PATH, mode=0o777, exist_ok=True)
            with open(regime_config_path, 'w') as f:
                json.dump({
                    'regime': regime_value,
                    'samples': len(X_regime),
                    'tuned_at': datetime.now().isoformat(),
                    **regime_params
                }, f, indent=2)

            logger.info(f"{regime_value.upper()} params saved: {regime_config_path}")

        # Save combined config for backward compatibility
        # Use most common regime (sideways_calm) as default
        default_params = all_regime_params.get(MarketRegime.SIDEWAYS_CALM.value)
        if default_params is None:
            # If sideways_calm tuning failed, use first available
            for params in all_regime_params.values():
                if params is not None:
                    default_params = params
                    break

        if default_params:
            combined_config = {
                'default': default_params,
                'regime_specific': all_regime_params,
                'tuned_at': datetime.now().isoformat()
            }

            with open(f"{MODEL_SAVE_PATH}/best_params.json", 'w') as f:
                json.dump(combined_config, f, indent=2)

            logger.info("\n" + "="*60)
            logger.info("Regime-specific tuning complete")
            logger.info(f"Results saved to {MODEL_SAVE_PATH}/")
            logger.info("="*60)
        else:
            logger.error("All regime tuning failed. No params saved.")

        session.commit()

    except Exception as e:
        logger.error(f"Tuning error: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


def _calculate_composite_score(
    y_val_values: np.ndarray,
    predictions: np.ndarray
) -> tuple:
    """
    Calculate composite objective score for classification models.

    Combines accuracy, weighted F1-score, and class balance into
    a single scalar for Optuna optimization.

    Composite = 0.40 * accuracy + 0.40 * f1_weighted + 0.20 * (1 - class_imbalance)

    Args:
        y_val_values: Actual target values (numpy array, ternary 0/1/2)
        predictions: Model predictions (numpy array, ternary 0/1/2)

    Returns:
        Tuple of (composite_score, accuracy, f1_weighted, class_balance_penalty)
    """
    # 1. Overall accuracy
    acc = float(accuracy_score(y_val_values, predictions))

    # 2. Weighted F1-score (handles class imbalance)
    f1 = float(f1_score(y_val_values, predictions, average='weighted', zero_division=0))

    # 3. Class balance: penalize if model predicts only one class
    unique_preds = np.unique(predictions)
    class_balance = len(unique_preds) / len(CLASS_NAMES)  # 0.33 to 1.0

    composite = (
        0.40 * acc
        + 0.40 * f1
        + 0.20 * class_balance
    )

    return composite, acc, f1, class_balance


def _tune_regime_models(
    X_scaled: pd.DataFrame,
    y: pd.Series,
    regime_name: str
) -> dict:
    """
    Tune hyperparameters for a specific regime.
    
    Args:
        X_scaled: Scaled features
        y: Target values
        regime_name: Name of the regime (for logging)
    
    Returns:
        Dict with best params for catboost, lgbm, xgboost
    """
    # Reduce trials for regime-specific tuning (50 instead of 100)
    n_trials = 50
    n_jobs = 3
    timeout = 1800  # 30 minutes per model

    # CatBoost Classifier tuning
    logger.info("=" * 60)
    logger.info(f"[{regime_name.upper()}] CatBoost Classifier Tuning ({n_trials} trials)")
    logger.info("=" * 60)

    def catboost_objective(trial):
        params = {
            'iterations': trial.suggest_int('iterations', 100, 500),
            'depth': trial.suggest_int('depth', 4, 10),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.3),
            'l2_leaf_reg': trial.suggest_float('l2_leaf_reg', 1, 10),
            'verbose': False,
            'allow_writing_files': False
        }

        model = CatBoostClassifierWrapper(**params)
        tscv = TimeSeriesSplit(n_splits=3)
        fold_composites, fold_accs, fold_f1s, fold_balances = [], [], [], []

        for train_idx, val_idx in tscv.split(X_scaled):
            X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            model.train(X_tr, y_tr)
            pred = model.predict(X_val)

            composite, acc, f1, balance = _calculate_composite_score(y_val.values, pred)
            fold_composites.append(composite)
            fold_accs.append(acc)
            fold_f1s.append(f1)
            fold_balances.append(balance)

        avg_composite = sum(fold_composites) / len(fold_composites)
        trial.set_user_attr('avg_accuracy', sum(fold_accs) / len(fold_accs))
        trial.set_user_attr('avg_f1', sum(fold_f1s) / len(fold_f1s))
        trial.set_user_attr('avg_balance', sum(fold_balances) / len(fold_balances))
        return avg_composite

    study_cat = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_cat.optimize(catboost_objective, n_trials=n_trials, n_jobs=n_jobs,
                       timeout=timeout, show_progress_bar=False)
    best_catboost = study_cat.best_params
    best_trial_cat = study_cat.best_trial
    logger.info(f"[{regime_name.upper()}] CatBoost Best Composite: {best_trial_cat.value:.4f} "
                f"(Acc={best_trial_cat.user_attrs['avg_accuracy']:.2%}, "
                f"F1={best_trial_cat.user_attrs['avg_f1']:.4f}, "
                f"Balance={best_trial_cat.user_attrs['avg_balance']:.2f})")

    # LGBM Classifier tuning
    logger.info("=" * 60)
    logger.info(f"[{regime_name.upper()}] LightGBM Classifier Tuning ({n_trials} trials)")
    logger.info("=" * 60)

    def lgbm_objective(trial):
        params = {
            'n_estimators': trial.suggest_int('n_estimators', 100, 500),
            'max_depth': trial.suggest_int('max_depth', 3, 8),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.15),
            'num_leaves': trial.suggest_int('num_leaves', 15, 60),
            'min_data_in_leaf': trial.suggest_int('min_data_in_leaf', 5, 50),
            'min_gain_to_split': trial.suggest_float('min_gain_to_split', 0.0, 0.1),
            'reg_alpha': trial.suggest_float('reg_alpha', 0.1, 10),
            'reg_lambda': trial.suggest_float('reg_lambda', 0.1, 10),
            'verbose': -1
        }

        model = LGBMClassifierWrapper(**params)
        tscv = TimeSeriesSplit(n_splits=3)
        fold_composites, fold_accs, fold_f1s, fold_balances = [], [], [], []

        for train_idx, val_idx in tscv.split(X_scaled):
            X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            model.train(X_tr, y_tr)
            pred = model.predict(X_val)

            composite, acc, f1, balance = _calculate_composite_score(y_val.values, pred)
            fold_composites.append(composite)
            fold_accs.append(acc)
            fold_f1s.append(f1)
            fold_balances.append(balance)

        avg_composite = sum(fold_composites) / len(fold_composites)
        trial.set_user_attr('avg_accuracy', sum(fold_accs) / len(fold_accs))
        trial.set_user_attr('avg_f1', sum(fold_f1s) / len(fold_f1s))
        trial.set_user_attr('avg_balance', sum(fold_balances) / len(fold_balances))
        return avg_composite

    study_lgbm = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_lgbm.optimize(lgbm_objective, n_trials=n_trials, n_jobs=n_jobs,
                        timeout=timeout, show_progress_bar=False)
    best_lgbm = study_lgbm.best_params
    best_trial_lgbm = study_lgbm.best_trial
    logger.info(f"[{regime_name.upper()}] LGBM Best Composite: {best_trial_lgbm.value:.4f} "
                f"(Acc={best_trial_lgbm.user_attrs['avg_accuracy']:.2%}, "
                f"F1={best_trial_lgbm.user_attrs['avg_f1']:.4f}, "
                f"Balance={best_trial_lgbm.user_attrs['avg_balance']:.2f})")

    # XGBoost Classifier tuning
    logger.info("=" * 60)
    logger.info(f"[{regime_name.upper()}] XGBoost Classifier Tuning ({n_trials} trials)")
    logger.info("=" * 60)

    def xgb_objective(trial):
        params = {
            'n_estimators': trial.suggest_int('n_estimators', 100, 500),
            'max_depth': trial.suggest_int('max_depth', 3, 10),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.3),
            'subsample': trial.suggest_float('subsample', 0.6, 1.0),
            'colsample_bytree': trial.suggest_float('colsample_bytree', 0.6, 1.0),
            'reg_alpha': trial.suggest_float('reg_alpha', 0.1, 10),
            'reg_lambda': trial.suggest_float('reg_lambda', 0.1, 10),
            'verbosity': 0
        }

        model = XGBoostClassifierWrapper(**params)
        tscv = TimeSeriesSplit(n_splits=3)
        fold_composites, fold_accs, fold_f1s, fold_balances = [], [], [], []

        for train_idx, val_idx in tscv.split(X_scaled):
            X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            model.train(X_tr, y_tr)
            pred = model.predict(X_val)

            composite, acc, f1, balance = _calculate_composite_score(y_val.values, pred)
            fold_composites.append(composite)
            fold_accs.append(acc)
            fold_f1s.append(f1)
            fold_balances.append(balance)

        avg_composite = sum(fold_composites) / len(fold_composites)
        trial.set_user_attr('avg_accuracy', sum(fold_accs) / len(fold_accs))
        trial.set_user_attr('avg_f1', sum(fold_f1s) / len(fold_f1s))
        trial.set_user_attr('avg_balance', sum(fold_balances) / len(fold_balances))
        return avg_composite

    study_xgb = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_xgb.optimize(xgb_objective, n_trials=n_trials, n_jobs=n_jobs,
                       timeout=timeout, show_progress_bar=False)
    best_xgb = study_xgb.best_params
    best_trial_xgb = study_xgb.best_trial
    logger.info(f"[{regime_name.upper()}] XGBoost Best Composite: {best_trial_xgb.value:.4f} "
                f"(Acc={best_trial_xgb.user_attrs['avg_accuracy']:.2%}, "
                f"F1={best_trial_xgb.user_attrs['avg_f1']:.4f}, "
                f"Balance={best_trial_xgb.user_attrs['avg_balance']:.2f})")

    return {
        'catboost': best_catboost,
        'lgbm': best_lgbm,
        'xgboost': best_xgb
    }


def _tune_models_global(
    X: pd.DataFrame,
    y: pd.Series,
    feature_engineer: FeatureEngineer
) -> None:
    """
    Fallback: Tune on all data globally (old behavior).
    Used when regime classification is not available.
    """
    logger.warning("Performing global tuning (no regime classification)")

    # Calculate market average volume
    market_avg_volume = X['volume'].mean() if 'volume' in X.columns else None

    # Scale features
    X_scaled = feature_engineer.extract_feature_vector(X, fit_scaler=True, market_avg_volume=market_avg_volume, feature_set="base")

    # CatBoost tuning
    logger.info("=" * 60)
    logger.info("Starting CatBoost Hyperparameter Tuning (100 trials, 3 parallel)")
    logger.info("=" * 60)
    def catboost_objective(trial):
        logger.info(f"[CatBoost Trial {trial.number + 1}/100] Testing parameters...")
        params = {
            'iterations': trial.suggest_int('iterations', 100, 500),
            'depth': trial.suggest_int('depth', 4, 10),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.3),
            'l2_leaf_reg': trial.suggest_float('l2_leaf_reg', 1, 10),
            'verbose': False,
            'allow_writing_files': False
        }

        model = CatBoostClassifierWrapper(**params)
        tscv = TimeSeriesSplit(n_splits=3)
        fold_composites, fold_accs, fold_f1s, fold_balances = [], [], [], []

        for train_idx, val_idx in tscv.split(X_scaled):
            X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            model.train(X_tr, y_tr)
            pred = model.predict(X_val)

            composite, acc, f1, balance = _calculate_composite_score(y_val.values, pred)
            fold_composites.append(composite)
            fold_accs.append(acc)
            fold_f1s.append(f1)
            fold_balances.append(balance)

        avg_composite = sum(fold_composites) / len(fold_composites)
        trial.set_user_attr('avg_accuracy', sum(fold_accs) / len(fold_accs))
        trial.set_user_attr('avg_f1', sum(fold_f1s) / len(fold_f1s))
        trial.set_user_attr('avg_balance', sum(fold_balances) / len(fold_balances))
        logger.info(f"[CatBoost Trial {trial.number + 1}/100] Composite: {avg_composite:.4f} "
                    f"(Acc={trial.user_attrs['avg_accuracy']:.2%}, "
                    f"F1={trial.user_attrs['avg_f1']:.4f}, "
                    f"Balance={trial.user_attrs['avg_balance']:.2f})")
        return avg_composite

    study_cat = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_cat.optimize(catboost_objective, n_trials=100, n_jobs=3, timeout=3600, show_progress_bar=False)
    best_catboost = study_cat.best_params
    best_trial_cat = study_cat.best_trial
    logger.info("=" * 60)
    logger.info(f"CatBoost Best Params: {best_catboost}")
    logger.info(f"CatBoost Best Composite: {best_trial_cat.value:.4f} "
                f"(Acc={best_trial_cat.user_attrs['avg_accuracy']:.2%}, "
                f"F1={best_trial_cat.user_attrs['avg_f1']:.4f}, "
                f"Balance={best_trial_cat.user_attrs['avg_balance']:.2f})")
    logger.info("=" * 60)

    # LGBM tuning
    logger.info("=" * 60)
    logger.info("Starting LightGBM Hyperparameter Tuning (100 trials, 3 parallel)")
    logger.info("=" * 60)
    def lgbm_objective(trial):
        logger.info(f"[LGBM Trial {trial.number + 1}/100] Testing parameters...")
        params = {
            'n_estimators': trial.suggest_int('n_estimators', 100, 500),
            'max_depth': trial.suggest_int('max_depth', 3, 8),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.15),
            'num_leaves': trial.suggest_int('num_leaves', 15, 60),
            'min_data_in_leaf': trial.suggest_int('min_data_in_leaf', 5, 50),
            'min_gain_to_split': trial.suggest_float('min_gain_to_split', 0.0, 0.1),
            'reg_alpha': trial.suggest_float('reg_alpha', 0.1, 10),
            'reg_lambda': trial.suggest_float('reg_lambda', 0.1, 10),
            'verbose': -1
        }

        model = LGBMClassifierWrapper(**params)
        tscv = TimeSeriesSplit(n_splits=3)
        fold_composites, fold_accs, fold_f1s, fold_balances = [], [], [], []

        for train_idx, val_idx in tscv.split(X_scaled):
            X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            model.train(X_tr, y_tr)
            pred = model.predict(X_val)

            composite, acc, f1, balance = _calculate_composite_score(y_val.values, pred)
            fold_composites.append(composite)
            fold_accs.append(acc)
            fold_f1s.append(f1)
            fold_balances.append(balance)

        avg_composite = sum(fold_composites) / len(fold_composites)
        trial.set_user_attr('avg_accuracy', sum(fold_accs) / len(fold_accs))
        trial.set_user_attr('avg_f1', sum(fold_f1s) / len(fold_f1s))
        trial.set_user_attr('avg_balance', sum(fold_balances) / len(fold_balances))
        logger.info(f"[LGBM Trial {trial.number + 1}/100] Composite: {avg_composite:.4f} "
                    f"(Acc={trial.user_attrs['avg_accuracy']:.2%}, "
                    f"F1={trial.user_attrs['avg_f1']:.4f}, "
                    f"Balance={trial.user_attrs['avg_balance']:.2f})")
        return avg_composite

    study_lgbm = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_lgbm.optimize(lgbm_objective, n_trials=100, n_jobs=3, timeout=3600, show_progress_bar=False)
    best_lgbm = study_lgbm.best_params
    best_trial_lgbm = study_lgbm.best_trial
    logger.info("=" * 60)
    logger.info(f"LGBM Best Params: {best_lgbm}")
    logger.info(f"LGBM Best Composite: {best_trial_lgbm.value:.4f} "
                f"(Acc={best_trial_lgbm.user_attrs['avg_accuracy']:.2%}, "
                f"F1={best_trial_lgbm.user_attrs['avg_f1']:.4f}, "
                f"Balance={best_trial_lgbm.user_attrs['avg_balance']:.2f})")
    logger.info("=" * 60)

    # XGBoost tuning
    logger.info("=" * 60)
    logger.info("Starting XGBoost Hyperparameter Tuning (100 trials, 3 parallel)")
    logger.info("=" * 60)
    def xgb_objective(trial):
        logger.info(f"[XGBoost Trial {trial.number + 1}/100] Testing parameters...")
        params = {
            'n_estimators': trial.suggest_int('n_estimators', 100, 500),
            'max_depth': trial.suggest_int('max_depth', 3, 10),
            'learning_rate': trial.suggest_float('learning_rate', 0.01, 0.3),
            'subsample': trial.suggest_float('subsample', 0.6, 1.0),
            'colsample_bytree': trial.suggest_float('colsample_bytree', 0.6, 1.0),
            'reg_alpha': trial.suggest_float('reg_alpha', 0.1, 10),
            'reg_lambda': trial.suggest_float('reg_lambda', 0.1, 10),
            'verbosity': 0
        }

        model = XGBoostClassifierWrapper(**params)
        tscv = TimeSeriesSplit(n_splits=3)
        fold_composites, fold_accs, fold_f1s, fold_balances = [], [], [], []

        for train_idx, val_idx in tscv.split(X_scaled):
            X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
            y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]

            model.train(X_tr, y_tr)
            pred = model.predict(X_val)

            composite, acc, f1, balance = _calculate_composite_score(y_val.values, pred)
            fold_composites.append(composite)
            fold_accs.append(acc)
            fold_f1s.append(f1)
            fold_balances.append(balance)

        avg_composite = sum(fold_composites) / len(fold_composites)
        trial.set_user_attr('avg_accuracy', sum(fold_accs) / len(fold_accs))
        trial.set_user_attr('avg_f1', sum(fold_f1s) / len(fold_f1s))
        trial.set_user_attr('avg_balance', sum(fold_balances) / len(fold_balances))
        logger.info(f"[XGBoost Trial {trial.number + 1}/100] Composite: {avg_composite:.4f} "
                    f"(Acc={trial.user_attrs['avg_accuracy']:.2%}, "
                    f"F1={trial.user_attrs['avg_f1']:.4f}, "
                    f"Balance={trial.user_attrs['avg_balance']:.2f})")
        return avg_composite

    study_xgb = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_xgb.optimize(xgb_objective, n_trials=100, n_jobs=3, timeout=3600, show_progress_bar=False)
    best_xgb = study_xgb.best_params
    best_trial_xgb = study_xgb.best_trial
    logger.info("=" * 60)
    logger.info(f"XGBoost Best Params: {best_xgb}")
    logger.info(f"XGBoost Best Composite: {best_trial_xgb.value:.4f} "
                f"(Acc={best_trial_xgb.user_attrs['avg_accuracy']:.2%}, "
                f"F1={best_trial_xgb.user_attrs['avg_f1']:.4f}, "
                f"Balance={best_trial_xgb.user_attrs['avg_balance']:.2f})")
    logger.info("=" * 60)

    # Save all tuned configs (optimized via composite score: Accuracy + F1 + Balance)
    tuning_config = {
        'catboost': best_catboost,
        'lgbm': best_lgbm,
        'xgboost': best_xgb,
        'tuned_at': datetime.now().isoformat()
    }

    os.makedirs(MODEL_SAVE_PATH, mode=0o777, exist_ok=True)
    with open(f"{MODEL_SAVE_PATH}/best_params.json", 'w') as f:
        json.dump(tuning_config, f, indent=2)

    logger.info("Global hyperparameter tuning complete - results saved to best_params.json")



@celery_app.task(name="app.tasks.training.analyze_feature_importance", bind=True, max_retries=2)
@notify_on_failure("analyze_feature_importance")
def analyze_feature_importance(self, regime: str = None):
    """
    Analyze feature importance for trained models.
    
    
    Args:
        regime: Specific regime to analyze (e.g., 'bull_trending', 'bear_trending')
                If None, analyze generic ensemble model
    
    Returns:
        Dict with feature importance scores and visualization data
    """
    logger.info(f"Starting feature importance analysis (regime={regime})")

    try:
        import matplotlib.pyplot as plt
        import numpy as np

        # Determine model path
        if regime:
            model_path = f"{MODEL_SAVE_PATH}/ensemble_classifier_{regime}.pkl"
        else:
            model_path = f"{MODEL_SAVE_PATH}/ensemble_classifier.pkl"

        if not os.path.exists(model_path):
            logger.error(f"Model not found: {model_path}")
            return {'status': 'error', 'message': f'Model not found: {model_path}'}

        # Load model
        from app.ml.models import EnsembleClassifierWrapper
        ensemble = EnsembleClassifierWrapper()
        ensemble.load(model_path)

        # Check if ensemble model was loaded successfully
        if ensemble.model is None:
            logger.error(f"앙상블 모델 로드 실패: {model_path}")
            return {'status': 'error', 'message': '모델 로드 실패'}

        # Validate model structure
        if not hasattr(ensemble.model, 'estimators_'):
            logger.error("Loaded model does not have estimators_ attribute")
            return {'status': 'error', 'message': 'Invalid model structure'}

        # Debug: Check estimators_ structure
        logger.info(f"Number of estimators: {len(ensemble.model.estimators_)}")
        logger.info(f"Estimator types: {[type(est).__name__ for est in ensemble.model.estimators_]}")

        # Extract feature importance from each base model
        # VotingRegressor.estimators_ is a list of fitted estimators (not tuples)
        feature_names = None
        importance_scores = {}

        model_names = ['catboost', 'lgbm', 'xgboost']
        for model_name, model in zip(model_names, ensemble.model.estimators_):
            if model is None:
                logger.warning(f"{model_name} estimator is None")
                continue

            # Get feature importance from tree-based model
            if hasattr(model, 'feature_importances_'):
                importances = model.feature_importances_
                if feature_names is None and hasattr(model, 'feature_names_'):
                    feature_names = model.feature_names_

                importance_scores[model_name] = importances.tolist()
                logger.info(f"{model_name} feature importances extracted: {len(importances)} features")
            else:
                logger.warning(f"{model_name} does not have feature_importances_ attribute")

        if not importance_scores:
            logger.warning("No feature importances found in models")
            return {'status': 'warning', 'message': 'No feature importances available'}

        # Calculate weighted average importance (using ensemble weights)
        avg_importance = np.zeros(len(next(iter(importance_scores.values()))))
        total_weight = 0

        # Get weights from ensemble (stored in metadata or use equal weights)
        weights = ensemble.weights if ensemble.weights else [1/3, 1/3, 1/3]

        for i, (model_name, importances) in enumerate(importance_scores.items()):
            weight = weights[i] if i < len(weights) else 0.33
            avg_importance += np.array(importances) * weight
            total_weight += weight

        avg_importance /= total_weight

        # Create feature importance DataFrame
        feature_engineer = FeatureEngineer()
        if feature_names is None:
            # Use base_feature_columns since models are trained on historical data
            feature_names = feature_engineer.base_feature_columns

        importance_df = pd.DataFrame({
            'feature': feature_names,
            'importance': avg_importance
        }).sort_values('importance', ascending=False)

        # Log top 10 features
        logger.info("\n" + "="*60)
        logger.info(f"Top 10 Features (Regime: {regime or 'Generic'})")
        logger.info("="*60)
        for idx, row in importance_df.head(10).iterrows():
            logger.info(f"{row['feature']:20s}: {row['importance']:.4f}")
        logger.info("="*60)

        # Save importance plot
        plt.figure(figsize=(10, 8))
        top_features = importance_df.head(15)
        plt.barh(top_features['feature'], top_features['importance'])
        plt.xlabel('Importance Score')
        plt.ylabel('Feature')
        plt.title(f'Feature Importance (Regime: {regime or "Generic"})')
        plt.tight_layout()

        plot_filename = f"feature_importance_{regime or 'generic'}.png"
        plot_path = os.path.join(MODEL_SAVE_PATH, plot_filename)
        plt.savefig(plot_path, dpi=150, bbox_inches='tight')
        plt.close()

        logger.info(f"Feature importance plot saved: {plot_path}")

        # Save importance data as JSON
        importance_json = importance_df.to_dict(orient='records')
        json_filename = f"feature_importance_{regime or 'generic'}.json"
        json_path = os.path.join(MODEL_SAVE_PATH, json_filename)

        with open(json_path, 'w') as f:
            json.dump({
                'regime': regime or 'generic',
                'analyzed_at': datetime.now().isoformat(),
                'top_features': importance_json[:20],
                'model_weights': ensemble.weights
            }, f, indent=2)

        logger.info(f"Feature importance data saved: {json_path}")

        return {
            'status': 'success',
            'regime': regime or 'generic',
            'top_10_features': importance_json[:10],
            'plot_path': plot_path,
            'json_path': json_path
        }

    except Exception as e:
        logger.error(f"Feature importance analysis failed: {e}", exc_info=True)
        return {'status': 'error', 'message': str(e)}
