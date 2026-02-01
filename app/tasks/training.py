import json
import logging
import os
from datetime import datetime, timedelta

import optuna
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit

from app.core.database import SessionLocal
from app.ml.features import FeatureEngineer
from app.ml.models import CatBoostWrapper, LGBMWrapper, XGBoostWrapper
from app.ml.predictor import PredictorService
from app.repositories.stock_repo_sync import SyncStockRepository
from app.services.discord_notifier import discord_notifier, notify_on_failure
from app.services.regime import MarketRegime, RegimeDetector
from app.worker import celery_app

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
            # SPY 15분봉 데이터 확인
            spy_ohlcv = repo.get_ohlcv_range('SPY', start_date, end_date, timeframe='15m')

            # SPY 데이터 부족 시 경고
            if len(spy_ohlcv) < 100:
                logger.warning(
                    "SPY 15분봉 데이터 부족 (%d bars). SPY를 symbol_subset에 추가하여 백필하세요.",
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
                from app.core.cache import cache
                vix_cached = cache.get("vix:latest")
                if vix_cached:
                    vix_value = float(vix_cached)
                    logger.info("VIX 캐시에서 로드: %.2f", vix_value)
                else:
                    logger.warning("VIX 캐시 없음 (ATR 기반 레짐 감지 사용)")
            except Exception as e:
                logger.debug("VIX 로드 실패: %s", e)

            # 시계열별로 rolling window regime 분류 (FIX: 전체 기간 한 번에 분류하던 문제 해결)
            if len(spy_features) >= 50:
                logger.info("시계열별 rolling window regime 분류 시작...")

                # 각 샘플의 시점까지의 SPY 데이터로 regime 분류
                regimes = []
                for idx, timestamp in enumerate(X.index):
                    # 해당 시점까지의 SPY 데이터 (최근 200개 윈도우)
                    spy_window = spy_features[spy_features.index <= timestamp].tail(200)

                    if len(spy_window) >= 50:
                        regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
                        regimes.append(regime.value)
                    else:
                        # 초기 데이터 부족 시 기본값
                        regimes.append(MarketRegime.SIDEWAYS_CALM.value)

                    # 진행 상황 로깅 (10% 단위)
                    if (idx + 1) % (len(X) // 10) == 0:
                        logger.info("Regime 분류 진행: %d/%d (%.1f%%)", idx + 1, len(X), (idx + 1) / len(X) * 100)

                X['regime'] = regimes
                logger.info("시계열별 regime 분류 완료: %d개 샘플", len(regimes))
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


def _walk_forward_validation_enhanced(
    ensemble_model,
    X: pd.DataFrame,
    y: pd.Series,
    feature_engineer: FeatureEngineer,
    regime: str,
    n_splits: int = 3
) -> dict:
    """
    Enhanced Walk-Forward Out-of-Sample validation for overfitting detection.

    Phase H.4: Provides detailed OOS metrics to detect regime model overfitting.

    Args:
        ensemble_model: Trained ensemble model
        X: Feature DataFrame (scaled)
        y: Target Series
        feature_engineer: For any additional scaling
        regime: Regime name for logging
        n_splits: Number of walk-forward periods

    Returns:
        Dictionary with detailed validation results:
        {
            'regime': str,
            'in_sample_sharpe': float,
            'oos_sharpe': float,
            'oos_accuracy': float,
            'overfit_ratio': float,  # OOS/IS ratio (< 0.5 indicates overfit)
            'is_overfit': bool,
            'periods': list[dict],
            'model_confidence': float,
        }
    """
    from sklearn.model_selection import TimeSeriesSplit

    results = {
        'regime': regime,
        'periods': [],
        'in_sample_sharpe': 0.0,
        'oos_sharpe': 0.0,
        'oos_accuracy': 0.0,
        'overfit_ratio': 0.0,
        'is_overfit': False,
        'model_confidence': 0.5,
    }

    tscv = TimeSeriesSplit(n_splits=n_splits)
    oos_sharpes = []
    oos_accuracies = []
    is_sharpes = []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(X)):
        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

        if len(X_val) < 30:
            logger.warning(f"{regime} Fold {fold+1}: Too few OOS samples ({len(X_val)})")
            continue

        try:
            # Train a fresh model copy for IS evaluation
            from app.ml.models import EnsembleWrapper
            fold_model = EnsembleWrapper()
            fold_model.train(X_train, y_train)

            # In-sample prediction
            is_pred = fold_model.predict(X_train)
            is_dir = (is_pred > 0).astype(int) * 2 - 1
            is_returns = y_train.values * is_dir
            is_sharpe = is_returns.mean() / (is_returns.std() + 1e-8) * ((252 * 26) ** 0.5)
            is_sharpes.append(is_sharpe)

            # Out-of-sample prediction
            oos_pred = fold_model.predict(X_val)
            oos_dir = (oos_pred > 0).astype(int) * 2 - 1
            oos_returns = y_val.values * oos_dir
            oos_sharpe = oos_returns.mean() / (oos_returns.std() + 1e-8) * ((252 * 26) ** 0.5)
            oos_sharpes.append(oos_sharpe)

            # Direction accuracy
            actual_dir = (y_val.values > 0).astype(int) * 2 - 1
            accuracy = (oos_dir == actual_dir).mean()
            oos_accuracies.append(accuracy)

            period_result = {
                'fold': fold + 1,
                'train_samples': len(X_train),
                'val_samples': len(X_val),
                'in_sample_sharpe': round(is_sharpe, 4),
                'oos_sharpe': round(oos_sharpe, 4),
                'oos_accuracy': round(accuracy, 4),
            }
            results['periods'].append(period_result)

            logger.info(
                f"{regime} Fold {fold+1}: IS Sharpe={is_sharpe:.4f}, "
                f"OOS Sharpe={oos_sharpe:.4f}, OOS Acc={accuracy:.2%}"
            )

        except Exception as e:
            logger.error(f"{regime} Fold {fold+1} validation failed: {e}")

    if not oos_sharpes:
        logger.error(f"{regime}: No valid OOS validation results")
        return results

    # Calculate aggregated metrics
    avg_is_sharpe = sum(is_sharpes) / len(is_sharpes)
    avg_oos_sharpe = sum(oos_sharpes) / len(oos_sharpes)
    avg_oos_accuracy = sum(oos_accuracies) / len(oos_accuracies)

    # Overfit detection
    if avg_is_sharpe > 0:
        overfit_ratio = avg_oos_sharpe / avg_is_sharpe
    else:
        overfit_ratio = 1.0 if avg_oos_sharpe >= 0 else 0.0

    is_overfit = (overfit_ratio < 0.3) or (avg_is_sharpe > 5 and avg_oos_sharpe < 1)

    # Model confidence calculation
    # Based on: OOS Sharpe, accuracy, and overfit ratio
    confidence = 0.5  # Base confidence
    if avg_oos_sharpe > 0:
        confidence += min(0.2, avg_oos_sharpe / 10)  # Up to +0.2 for positive Sharpe
    if avg_oos_accuracy > 0.52:
        confidence += 0.1  # Bonus for > 52% accuracy
    if not is_overfit:
        confidence += 0.1  # Bonus for no overfitting
    confidence = min(1.0, max(0.1, confidence))

    results.update({
        'in_sample_sharpe': round(avg_is_sharpe, 4),
        'oos_sharpe': round(avg_oos_sharpe, 4),
        'oos_accuracy': round(avg_oos_accuracy, 4),
        'overfit_ratio': round(overfit_ratio, 4),
        'is_overfit': is_overfit,
        'model_confidence': round(confidence, 2),
    })

    # Log summary
    overfit_status = "⚠️ OVERFIT DETECTED" if is_overfit else "✅ OK"
    logger.info(
        f"{regime} WF Summary: IS={avg_is_sharpe:.4f}, OOS={avg_oos_sharpe:.4f}, "
        f"Acc={avg_oos_accuracy:.2%}, Ratio={overfit_ratio:.2f} {overfit_status}"
    )

    return results


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
            f.write("-" * 70 + "\n")
            f.write(f"{'Regime':<20} {'Samples':<10} {'Accuracy':<12} {'Sharpe':<12} {'Status':<15}\n")
            f.write("-" * 70 + "\n")
            
            for regime, metrics in regime_results.items():
                samples = metrics.get('samples', 'N/A')
                accuracy = metrics.get('accuracy', 'N/A')
                sharpe = metrics.get('sharpe', 'N/A')
                status = metrics.get('status', 'unknown')
                
                acc_str = f"{accuracy:.2%}" if isinstance(accuracy, float) else str(accuracy)
                sharpe_str = f"{sharpe:.4f}" if isinstance(sharpe, float) else str(sharpe)
                
                f.write(f"{regime:<20} {samples:<10} {acc_str:<12} {sharpe_str:<12} {status:<15}\n")
            
            f.write("-" * 70 + "\n\n")
            
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
                    if isinstance(value, float):
                        f.write(f"  {key}: {value:.4f}\n")
                    else:
                        f.write(f"  {key}: {value}\n")
            
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
        predictor = PredictorService()

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
            repo, feature_engineer, symbols, start_date, end_date, symbol_limit=10, classify_regime=True
        )

        if X.empty:
            logger.error("학습용 데이터를 수집하지 못했습니다")
            return

        # Data size validation
        if len(X) < 500:
            logger.warning(f"데이터셋이 작습니다: {len(X)} 샘플. 더 긴 백필 또는 심볼 확대를 고려하세요.")

        logger.info(f"총 데이터: {len(X)} 샘플, {len(successful_symbols)}개 심볼로부터")

        # Phase H.3: 레짐별 모델 학습
        has_regime = 'regime' in X.columns

        if has_regime:
            logger.info("레짐별 모델 학습")
            _train_regime_specific_models(feature_engineer, X, y)

            # FIX: predictor를 재초기화하여 새로 학습된 모델 로드
            logger.info("\n" + "="*60)
            logger.info("학습된 모델 검증 (PredictorService 사용)")
            logger.info("="*60)

            regime_results = {}  # Collect results for report

            try:
                # 새로 학습된 모델을 로드하기 위해 predictor 재초기화
                predictor.reload_models()
                
                # PredictorService로 각 regime 모델 로드 및 평가
                for regime in MarketRegime:
                    regime_value = regime.value
                    regime_mask = X['regime'] == regime_value
                    X_regime = X[regime_mask].drop(columns=['regime'])
                    y_regime = y[regime_mask]

                    if len(X_regime) < 100:
                        logger.info(f"{regime_value}: 데이터 부족 (샘플 {len(X_regime)}개), 검증 스킵")
                        regime_results[regime_value] = {
                            'samples': len(X_regime),
                            'status': 'insufficient_data'
                        }
                        continue

                    # predictor로 모델 가져오기
                    try:
                        ensemble = predictor.get_model(regime)
                        if ensemble is None:
                            logger.warning(f"{regime_value}: 모델 파일 없음, 검증 스킵")
                            regime_results[regime_value] = {
                                'samples': len(X_regime),
                                'status': 'no_model'
                            }
                            continue

                        # 검증 데이터로 평가 (최근 20% 사용)
                        split_idx = int(len(X_regime) * 0.8)
                        X_val = X_regime.iloc[split_idx:]
                        y_val = y_regime.iloc[split_idx:]

                        # Feature scaling
                        market_avg_volume = X_regime['volume'].mean() if 'volume' in X_regime.columns else None
                        X_val_scaled = feature_engineer.extract_feature_vector(
                            X_val, fit_scaler=False, market_avg_volume=market_avg_volume
                        )

                        # 예측 및 정확도 계산
                        predictions = ensemble.predict(X_val_scaled)
                        pred_dir = (predictions > 0).astype(int) * 2 - 1
                        actual_dir = (y_val.values > 0).astype(int) * 2 - 1
                        accuracy = (pred_dir == actual_dir).mean()

                        # Sharpe ratio 계산
                        returns = y_val.values * pred_dir
                        sharpe = returns.mean() / (returns.std() + 1e-8) * ((252 * 26) ** 0.5)

                        logger.info(f"{regime_value} 모델 검증 완료:")
                        logger.info(f"  - 샘플: {len(X_regime)} (검증: {len(X_val)})")
                        logger.info(f"  - 방향 정확도: {accuracy:.2%}")
                        logger.info(f"  - Sharpe Ratio: {sharpe:.4f}")

                        # Collect results for report
                        regime_results[regime_value] = {
                            'samples': len(X_regime),
                            'validation_samples': len(X_val),
                            'accuracy': accuracy,
                            'sharpe': sharpe,
                            'status': 'success'
                        }

                    except Exception as e:
                        logger.error(f"{regime_value} 모델 검증 실패: {e}", exc_info=True)
                        regime_results[regime_value] = {
                            'samples': len(X_regime),
                            'status': 'error',
                            'error': str(e)
                        }

                logger.info("="*60 + "\n")

                # Save training report to text file
                _save_training_report(regime_results, X)

            except Exception as e:
                logger.error(f"모델 검증 중 오류: {e}", exc_info=True)

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


def _train_regime_specific_models(
    feature_engineer: FeatureEngineer,
    X: pd.DataFrame,
    y: pd.Series
):
    """
    Train 4 regime-specific ensemble models.
    
    
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
            with open(best_params_path) as f:
                tuning_config = json.load(f)
        except Exception as e:
            logger.warning(f"튜닝 파라미터 로드 실패: {e}")

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
        # sideways_volatile 같은 희귀 regime도 학습 가능하도록 최소 샘플 100으로 완화 (고민중)
        min_samples = 1000
        if len(X_regime) < min_samples:
            logger.warning(f"Insufficient {regime_value} data: {len(X_regime)} < {min_samples} samples")
            logger.warning(f"Skipping {regime_value} model training (will use generic fallback)")
            continue

        # # 데이터 양에 따른 경고 (1000개 미만)
        # if len(X_regime) < 1000:
        #     logger.warning(f"{regime_value}: 샘플 수 부족 ({len(X_regime)}개). 과적합 위험 있음. 더 많은 데이터 수집 권장.")

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
                logger.error(f"  {name} 처리 실패: {e}", exc_info=True)
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
            logger.error(f"{regime_value} 모델 학습 실패: {e}", exc_info=True)

    logger.info(f"\n{'='*60}")
    logger.info("Regime-specific training complete")
    logger.info(f"{'='*60}\n")


@celery_app.task(name="app.tasks.training.tune_models", bind=True)
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
                X_regime, fit_scaler=True, market_avg_volume=market_avg_volume
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

    # CatBoost tuning
    logger.info("=" * 60)
    logger.info(f"[{regime_name.upper()}] CatBoost Tuning ({n_trials} trials)")
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

        return sum(scores) / len(scores)

    study_cat = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_cat.optimize(catboost_objective, n_trials=n_trials, n_jobs=n_jobs,
                       timeout=timeout, show_progress_bar=False)
    best_catboost = study_cat.best_params
    logger.info(f"[{regime_name.upper()}] CatBoost Best Sharpe: {study_cat.best_value:.4f}")

    # LGBM tuning
    logger.info("=" * 60)
    logger.info(f"[{regime_name.upper()}] LightGBM Tuning ({n_trials} trials)")
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

        return sum(scores) / len(scores)

    study_lgbm = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_lgbm.optimize(lgbm_objective, n_trials=n_trials, n_jobs=n_jobs,
                        timeout=timeout, show_progress_bar=False)
    best_lgbm = study_lgbm.best_params
    logger.info(f"[{regime_name.upper()}] LGBM Best Sharpe: {study_lgbm.best_value:.4f}")

    # XGBoost tuning
    logger.info("=" * 60)
    logger.info(f"[{regime_name.upper()}] XGBoost Tuning ({n_trials} trials)")
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

        return sum(scores) / len(scores)

    study_xgb = optuna.create_study(
        direction='maximize',
        pruner=optuna.pruners.MedianPruner(n_startup_trials=5, n_warmup_steps=10)
    )
    study_xgb.optimize(xgb_objective, n_trials=n_trials, n_jobs=n_jobs,
                       timeout=timeout, show_progress_bar=False)
    best_xgb = study_xgb.best_params
    logger.info(f"[{regime_name.upper()}] XGBoost Best Sharpe: {study_xgb.best_value:.4f}")

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

    logger.info("Global hyperparameter tuning complete - results saved to best_params.json")



@celery_app.task(name="app.tasks.training.analyze_feature_importance", bind=True)
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
            model_path = f"{MODEL_SAVE_PATH}/ensemble_model_{regime}.pkl"
        else:
            model_path = f"{MODEL_SAVE_PATH}/ensemble_model.pkl"

        if not os.path.exists(model_path):
            logger.error(f"Model not found: {model_path}")
            return {'status': 'error', 'message': f'Model not found: {model_path}'}

        # Load model
        from app.ml.models import EnsembleWrapper
        ensemble = EnsembleWrapper()
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
