import logging
import json
import os
from app.worker import celery_app
from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import pandas as pd
import optuna
from sklearn.model_selection import TimeSeriesSplit

from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository
from app.ml.models import CatBoostWrapper, LGBMWrapper, XGBoostWrapper
from app.ml.features import FeatureEngineer
from app.ml.predictor import PredictorService
from app.services.regime import RegimeDetector, MarketRegime

logger = logging.getLogger(__name__)

# Configuration
LOOKBACK_YEARS = 2
VALIDATION_DAYS = 30
MODEL_SAVE_PATH = "model_artifacts"

# Walk-Forward Validation Periods (in days)
WALK_FORWARD_PERIODS = [
    (90, 60),  # 90~60 days ago
    (60, 30),  # 60~30 days ago
    (30, 0),   # 30~0 days ago (most recent)
]

def _load_and_prepare_data(
    repo: SyncStockRepository,
    feature_engineer: FeatureEngineer,
    symbols: List[str],
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
    symbol_limit: int = None,
    classify_regime: bool = True
) -> Tuple[pd.DataFrame, pd.Series, List[str]]:
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
            
            # Target: Next bar return
            features_df['target'] = features_df['close'].pct_change().shift(-1)
            features_df.dropna(inplace=True)
            
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
    
    X = pd.concat(all_X, ignore_index=True)
    y = pd.concat(all_y, ignore_index=True)
    
    if classify_regime:
        logger.info("데이터를 시장 레짐별로 분류 중...")
        regime_detector = RegimeDetector()
        
        # SPY 데이터 로드 (regime classification용)
        try:
            # SPY 15분봉 데이터 확인
            spy_ohlcv = repo.get_ohlcv_range('SPY', start_date, end_date, timeframe='15m')
            
            # SPY 데이터 부족 시 경고 및 SPY도 symbol_subset에 포함
            if len(spy_ohlcv) < 100:
                logger.warning(
                    "SPY 15분봉 데이터 부족 (%d bars). 레짐 분류를 위해 SPY도 학습에 포함합니다.",
                    len(spy_ohlcv)
                )
                # SPY가 symbol_subset에 없으면 추가
                if 'SPY' not in symbol_subset:
                    symbol_subset = ['SPY'] + list(symbol_subset)
                    logger.info("SPY를 symbol 목록에 추가했습니다.")
                    # SPY 데이터 다시 로드 (루프에서 처리됨)
                    # 여기서는 경고만 하고, 실제 로드는 메인 루프에서
                
                # 그래도 데이터가 부족하면 기본 레짐 사용
                if len(spy_ohlcv) < 50:
                    logger.warning("SPY 데이터 극도로 부족. 모든 데이터를 SIDEWAYS_CALM으로 분류합니다.")
                    X['regime'] = MarketRegime.SIDEWAYS_CALM.value
                    return X, y, successful_symbols
            
            # SPY DataFrame 생성
            spy_df = pd.DataFrame([{
                'date_time': bar.date_time,
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume
            } for bar in spy_ohlcv])
            spy_df.set_index('date_time', inplace=True)
            spy_df.sort_index(inplace=True)
            
            # SPY 피처 생성 (레짐 감지용)
            spy_features = feature_engineer.create_features(spy_df)
            
            # 각 타임스탬프별로 레짐 분류
            regimes = []
            for idx in X.index:
                # 가장 가까운 SPY 타임스탬프 찾기
                spy_window = spy_features[spy_features.index <= idx]
                if len(spy_window) > 0:
                    regime = regime_detector.detect_regime(spy_window)
                    regimes.append(regime.value)
                else:
                    regimes.append(MarketRegime.SIDEWAYS_CALM.value)  # 기본값
            
            X['regime'] = regimes
            regime_dist = pd.Series(regimes).value_counts().to_dict()
            logger.info("레짐 분포: %s", regime_dist)
            
        except (ValueError, KeyError, AttributeError) as e:
            logger.warning("레짐 분류 실패: %s. 레짐 없이 진행합니다.", str(e))
            X['regime'] = MarketRegime.SIDEWAYS_CALM.value  # Fallback
    
    logger.info("총 %d개 샘플, %d개 심볼로부터 데이터 로드 완료", len(X), len(successful_symbols))
    return X, y, successful_symbols

def _walk_forward_validation(
    model_wrapper,
    X: pd.DataFrame,
    y: pd.Series,
    feature_engineer: FeatureEngineer,
    end_date: pd.Timestamp
) -> float:
    """
    Walk-Forward validation across multiple time periods.
    
    Args:
        model_wrapper: Initialized model (CatBoost/LGBM/XGBoost)
        X: Feature DataFrame with datetime index
        y: Target Series
        feature_engineer: For scaling
        end_date: Reference end date
    
    Returns:
        Average Sharpe ratio across all validation periods
    """
    sharpe_scores = []
    
    for period_idx, (val_start_days, val_end_days) in enumerate(WALK_FORWARD_PERIODS):
        val_start = end_date - timedelta(days=val_start_days)
        val_end = end_date - timedelta(days=val_end_days)
        
        # Split data
        train_mask = (X.index < val_start)
        val_mask = (X.index >= val_start) & (X.index < val_end)
        
        X_train_period = X[train_mask]
        y_train_period = y[train_mask]
        X_val_period = X[val_mask]
        y_val_period = y[val_mask]
        
        if len(X_val_period) < 50:  # Minimum validation samples
            logger.warning(f"Period {period_idx + 1}: Too few validation samples ({len(X_val_period)})")
            continue
        
        # Scale
        X_train_scaled = feature_engineer.extract_feature_vector(X_train_period, fit_scaler=True)
        X_val_scaled = feature_engineer.extract_feature_vector(X_val_period, fit_scaler=False)
        
        # Train and predict
        model_wrapper.train(X_train_scaled, y_train_period)
        predictions = model_wrapper.predict(X_val_scaled)
        
        # Calculate Sharpe
        pred_dir = (predictions > 0).astype(int) * 2 - 1
        returns = y_val_period.values * pred_dir
        sharpe = returns.mean() / (returns.std() + 1e-8) * ((252 * 26) ** 0.5)
        sharpe_scores.append(sharpe)
        
        logger.info(f"Period {period_idx + 1} ({val_start_days}-{val_end_days} days ago): Sharpe={sharpe:.4f}")
    
    if not sharpe_scores:
        logger.error("No valid validation periods")
        return 0.0
    
    avg_sharpe = sum(sharpe_scores) / len(sharpe_scores)
    logger.info(f"Walk-Forward Average Sharpe: {avg_sharpe:.4f}")
    return avg_sharpe

@celery_app.task(name="app.tasks.training.train_models", bind=True, max_retries=3)
def train_models(self):
    """
    Model training task with Walk-Forward validation.
    Uses shared data loading and robust multi-period validation.
    """
    logger.info("Starting model training with Walk-Forward validation...")
    
    session = SessionLocal()
    try:
        repo = SyncStockRepository(session)
        feature_engineer = FeatureEngineer()
        predictor = PredictorService()
        
        # 1. Load active symbols
        symbols = repo.get_active_symbols()
        if not symbols:
            logger.warning("No active symbols found")
            return
        
        logger.info(f"Training on {len(symbols)} symbols")
        
        # 2. Load and prepare data using shared function
        end_date = pd.Timestamp.now(tz='UTC')
        start_date = end_date - timedelta(days=LOOKBACK_YEARS * 365)
        
        X, y, successful_symbols = _load_and_prepare_data(
            repo, feature_engineer, symbols, start_date, end_date, symbol_limit=10, classify_regime=True
        )
        
        if X.empty:
            logger.error("No training data collected")
            return
        
        # Data size validation
        if len(X) < 500:
            logger.warning(f"Small dataset: {len(X)} samples. Consider longer backfill or more symbols.")
        
        logger.info(f"Total data: {len(X)} samples from {len(successful_symbols)} symbols")
        
        # Phase H.3: Train regime-specific models
        has_regime = 'regime' in X.columns
        
        if has_regime:
            logger.info("Phase H.3: Training regime-specific models")
            _train_regime_specific_models(feature_engineer, X, y)
            session.commit()
            logger.info("Training complete - regime-specific models saved")
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


def _train_regime_specific_models(
    feature_engineer: FeatureEngineer,
    X: pd.DataFrame,
    y: pd.Series
):
    """
    Train 4 regime-specific ensemble models.
    
    Phase H.3 Implementation:
    - Split data by market regime
    - Train separate ensemble for each regime
    - Save 4 model files: ensemble_model_{regime}.pkl
    
    Args:
        feature_engineer: Feature engineering instance
        X: Feature DataFrame (with 'regime' column)
        y: Target Series
    """
    from app.ml.models import EnsembleWrapper
    
    # Load tuning params
    best_params_path = f"{MODEL_SAVE_PATH}/best_params.json"
    tuning_config = {}
    if os.path.exists(best_params_path):
        try:
            with open(best_params_path, 'r') as f:
                tuning_config = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load tuned params: {e}")
    
    # Iterate through each regime
    for regime in MarketRegime:
        regime_value = regime.value
        logger.info(f"\n{'='*60}")
        logger.info(f"Training {regime_value.upper()} regime model")
        logger.info(f"{'='*60}")
        
        # Filter data for this regime
        regime_mask = X['regime'] == regime_value
        X_regime = X[regime_mask].drop(columns=['regime'])
        y_regime = y[regime_mask]
        
        logger.info(f"Regime data: {len(X_regime)} samples ({len(X_regime)/len(X)*100:.1f}% of total)")
        
        # Minimum data requirement
        if len(X_regime) < 1000:
            logger.warning(f"Insufficient {regime_value} data: {len(X_regime)} < 1000 samples")
            logger.warning(f"Skipping {regime_value} model training (will use generic fallback)")
            continue
        
        # Calculate market average volume for this regime
        market_avg_volume = X_regime['volume'].mean() if 'volume' in X_regime.columns else None
        
        # Feature scaling
        X_regime_scaled = feature_engineer.extract_feature_vector(
            X_regime, fit_scaler=True, market_avg_volume=market_avg_volume
        )
        
        # Walk-Forward validation to calculate weights
        models_to_eval = [
            ('catboost', CatBoostWrapper(**tuning_config.get('catboost', {}))),
            ('lgbm', LGBMWrapper(**tuning_config.get('lgbm', {}))),
            ('xgboost', XGBoostWrapper(**tuning_config.get('xgboost', {})))
        ]
        
        sharpe_ratios = []
        for name, model in models_to_eval:
            try:
                logger.info(f"  Training {name}...")
                # Use TimeSeriesSplit for regime-specific validation
                tscv = TimeSeriesSplit(n_splits=3)
                scores = []
                for train_idx, val_idx in tscv.split(X_regime_scaled):
                    X_tr, X_val = X_regime_scaled.iloc[train_idx], X_regime_scaled.iloc[val_idx]
                    y_tr, y_val = y_regime.iloc[train_idx], y_regime.iloc[val_idx]
                    model.train(X_tr, y_tr)
                    pred = model.predict(X_val)
                    pred_dir = (pred > 0).astype(int) * 2 - 1
                    returns = y_val.values * pred_dir
                    sharpe = returns.mean() / (returns.std() + 1e-8) * ((252 * 26) ** 0.5)
                    scores.append(sharpe)
                sharpe = sum(scores) / len(scores) if scores else 0.0
                sharpe_ratios.append(max(sharpe, 0.1))
                logger.info(f"  {name} | Sharpe: {sharpe:.4f}")
            except Exception as e:
                logger.error(f"  ❌ Failed {name}: {e}", exc_info=True)
                sharpe_ratios.append(0.1)
        
        # Normalize weights
        total = sum(sharpe_ratios)
        weights = [s / total for s in sharpe_ratios] if total > 0 else [0.33, 0.33, 0.34]
        logger.info(f"  Ensemble weights: {[round(w, 3) for w in weights]}")
        
        # Train ensemble
        try:
            ensemble = EnsembleWrapper(weights=weights, model_params=tuning_config)
            ensemble.train(X_regime_scaled, y_regime)
            
            # Save model
            model_filename = f"ensemble_model_{regime_value}.pkl"
            model_path = os.path.join(MODEL_SAVE_PATH, model_filename)
            ensemble.save(model_path)
            
            logger.info(f"{regime_value.upper()} model saved: {model_filename}")
            
        except Exception as e:
            logger.error(f"❌ Failed to train {regime_value} model: {e}", exc_info=True)
    
    logger.info(f"\n{'='*60}")
    logger.info("Regime-specific training complete")
    logger.info(f"{'='*60}\n")


@celery_app.task(name="app.tasks.training.tune_models", bind=True)
def tune_models(self):
    """
    Hyperparameter tuning task using Optuna.
    Uses shared data loading for consistency.
    """
    logger.info("Starting hyperparameter tuning with Optuna...")
    
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
        
        # Load data using shared function
        X, y, successful_symbols = _load_and_prepare_data(
            repo, feature_engineer, symbols, start_date, end_date, symbol_limit=5  # Fewer for tuning
        )
        
        if X.empty:
            logger.error("No tuning data available")
            return
        
        logger.info(f"Tuning data: {len(X)} samples from {len(successful_symbols)} symbols")
        
        # Calculate market average volume
        market_avg_volume = X['volume'].mean() if 'volume' in X.columns else None
        
        # Scale features
        X_scaled = feature_engineer.extract_feature_vector(X, fit_scaler=True, market_avg_volume=market_avg_volume)
        
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
            
            model = CatBoostWrapper(**params)
            tscv = TimeSeriesSplit(n_splits=3)
            scores = []
            
            for train_idx, val_idx in tscv.split(X_scaled):
                X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
                y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]
                
                model.train(X_tr, y_tr)
                pred = model.predict(X_val)
                
                pred_dir = (pred > 0).astype(int) * 2 - 1
                returns = y_val.values * pred_dir
                sharpe = returns.mean() / (returns.std() + 1e-8) * ((252 * 26) ** 0.5)
                scores.append(sharpe)
            
            avg_sharpe = sum(scores) / len(scores)
            logger.info(f"[CatBoost Trial {trial.number + 1}/100] Avg Sharpe: {avg_sharpe:.4f}")
            return avg_sharpe
        
        study_cat = optuna.create_study(
            direction='maximize',
            pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
        )
        study_cat.optimize(catboost_objective, n_trials=100, n_jobs=3, timeout=3600, show_progress_bar=False)
        best_catboost = study_cat.best_params
        logger.info("=" * 60)
        logger.info(f"CatBoost Best Params: {best_catboost}")
        logger.info(f"CatBoost Best Sharpe: {study_cat.best_value:.4f}")
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
            
            model = LGBMWrapper(**params)
            tscv = TimeSeriesSplit(n_splits=3)
            scores = []
            
            for train_idx, val_idx in tscv.split(X_scaled):
                X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
                y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]
                
                model.train(X_tr, y_tr)
                pred = model.predict(X_val)
                
                pred_dir = (pred > 0).astype(int) * 2 - 1
                returns = y_val.values * pred_dir
                sharpe = returns.mean() / (returns.std() + 1e-8) * ((252 * 26) ** 0.5)
                scores.append(sharpe)
            
            avg_sharpe = sum(scores) / len(scores)
            logger.info(f"[LGBM Trial {trial.number + 1}/100] Avg Sharpe: {avg_sharpe:.4f}")
            return avg_sharpe
        
        study_lgbm = optuna.create_study(
            direction='maximize',
            pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
        )
        study_lgbm.optimize(lgbm_objective, n_trials=100, n_jobs=3, timeout=3600, show_progress_bar=False)
        best_lgbm = study_lgbm.best_params
        logger.info("=" * 60)
        logger.info(f"LGBM Best Params: {best_lgbm}")
        logger.info(f"LGBM Best Sharpe: {study_lgbm.best_value:.4f}")
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
            
            model = XGBoostWrapper(**params)
            tscv = TimeSeriesSplit(n_splits=3)
            scores = []
            
            for train_idx, val_idx in tscv.split(X_scaled):
                X_tr, X_val = X_scaled.iloc[train_idx], X_scaled.iloc[val_idx]
                y_tr, y_val = y.iloc[train_idx], y.iloc[val_idx]
                
                model.train(X_tr, y_tr)
                pred = model.predict(X_val)
                
                pred_dir = (pred > 0).astype(int) * 2 - 1
                returns = y_val.values * pred_dir
                sharpe = returns.mean() / (returns.std() + 1e-8) * ((252 * 26) ** 0.5)
                scores.append(sharpe)
            
            avg_sharpe = sum(scores) / len(scores)
            logger.info(f"[XGBoost Trial {trial.number + 1}/100] Avg Sharpe: {avg_sharpe:.4f}")
            return avg_sharpe
        
        study_xgb = optuna.create_study(
            direction='maximize',
            pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
        )
        study_xgb.optimize(xgb_objective, n_trials=100, n_jobs=3, timeout=3600, show_progress_bar=False)
        best_xgb = study_xgb.best_params
        logger.info("=" * 60)
        logger.info(f"XGBoost Best Params: {best_xgb}")
        logger.info(f"XGBoost Best Sharpe: {study_xgb.best_value:.4f}")
        logger.info("=" * 60)
        
        # Save all tuned configs (without ratio tuning - use Sharpe only for simplicity)
        tuning_config = {
            'catboost': best_catboost,
            'lgbm': best_lgbm,
            'xgboost': best_xgb,
            'tuned_at': datetime.now().isoformat()
        }
        
        os.makedirs(MODEL_SAVE_PATH, mode=0o777, exist_ok=True)
        with open(f"{MODEL_SAVE_PATH}/best_params.json", 'w') as f:
            json.dump(tuning_config, f, indent=2)
        
        session.commit()
        logger.info("Hyperparameter tuning complete - results saved to best_params.json")
        
    except Exception as e:
        logger.error(f"Tuning error: {e}", exc_info=True)
        session.rollback()
        raise
    finally:
        session.close()


# ============================================================================
# Phase F.4: Feature Importance Analysis
# ============================================================================

@celery_app.task(name="app.tasks.training.analyze_feature_importance", bind=True)
def analyze_feature_importance(self, regime: str = None):
    """
    Analyze feature importance for trained models.
    
    Phase F.4: Provides insights into which features contribute most to predictions.
    
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
            model_path = f"{MODEL_SAVE_PATH}/ensemble_model_{regime}.pkl"
        else:
            model_path = f"{MODEL_SAVE_PATH}/ensemble_model.pkl"
        
        if not os.path.exists(model_path):
            logger.error(f"Model not found: {model_path}")
            return {'status': 'error', 'message': f'Model not found: {model_path}'}
        
        # Load model
        from app.ml.models import EnsembleWrapper
        ensemble = EnsembleWrapper.load(model_path)
        
        # Extract feature importance from each base model
        feature_names = None
        importance_scores = {}
        
        for model_name, model in zip(['catboost', 'lgbm', 'xgboost'], ensemble.models):
            if model is None:
                continue
            
            # Get feature importance from tree-based model
            if hasattr(model, 'feature_importances_'):
                importances = model.feature_importances_
                if feature_names is None and hasattr(model, 'feature_names_'):
                    feature_names = model.feature_names_
                
                importance_scores[model_name] = importances.tolist()
                logger.info(f"{model_name} feature importances extracted: {len(importances)} features")
        
        if not importance_scores:
            logger.warning("No feature importances found in models")
            return {'status': 'warning', 'message': 'No feature importances available'}
        
        # Calculate weighted average importance (using ensemble weights)
        avg_importance = np.zeros(len(next(iter(importance_scores.values()))))
        total_weight = 0
        
        for i, (model_name, importances) in enumerate(importance_scores.items()):
            weight = ensemble.weights[i] if i < len(ensemble.weights) else 0.33
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
