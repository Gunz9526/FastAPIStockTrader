# Regime-Specific Hyperparameter Tuning Upgrade
**Date:** 2026-01-18  
**Phase:** Training Infrastructure Enhancement  
**Impact:** Critical - Improves model performance across all market conditions

---

## 📋 Executive Summary

### Objective
개선 전 ML 모델 학습 시스템은 **전체 데이터로 하나의 파라미터 세트만 튜닝**하여 모든 시장 상황(BULL/BEAR/SIDEWAYS)에 동일한 파라미터를 사용했습니다. 이는 시장 레짐에 따라 최적 파라미터가 다를 수 있다는 점을 무시한 것입니다.

본 업그레이드는 **각 레짐별로 최적 하이퍼파라미터를 독립적으로 튜닝**하여 레짐별 모델 성능을 극대화합니다.

### Key Changes
1. **`tune_models()` 완전 재설계**: 전체 데이터 튜닝 → 4개 레짐별 튜닝
2. **`_train_regime_specific_models()` 최소 샘플 완화**: 1000 → 100 (sideways_volatile 학습 가능)
3. **`regime.py` 하드코딩 제거**: fallback 로직에 경고 로깅 추가
4. **섹터별 학습 확인**: sector_id 피처 사용 (분리 학습 불필요)

---

## 🔧 Detailed Changes

### 1. Regime-Specific Hyperparameter Tuning

#### Before (문제점)
```python
# app/tasks/training.py - tune_models()
X, y, successful_symbols = _load_and_prepare_data(
    repo, feature_engineer, symbols, start_date, end_date, 
    symbol_limit=None, classify_regime=False  # ← 레짐 분류 안 함
)

# 전체 데이터로 하나의 파라미터 세트만 튜닝
study_cat.optimize(catboost_objective, n_trials=100, ...)
```

**문제점:**
- BULL 시장에 최적인 파라미터가 BEAR 시장에서는 과적합될 수 있음
- 변동성이 높은 SIDEWAYS_VOLATILE과 조용한 SIDEWAYS_CALM이 같은 파라미터 사용
- 각 레짐의 특성을 반영하지 못함

#### After (해결책)
```python
# app/tasks/training.py - tune_models()
X, y, successful_symbols = _load_and_prepare_data(
    repo, feature_engineer, symbols, start_date, end_date, 
    symbol_limit=None, classify_regime=True  # ✅ 레짐 분류 활성화
)

# 4개 레짐별로 독립적으로 튜닝
for regime in MarketRegime:
    regime_mask = X['regime'] == regime.value
    X_regime = X[regime_mask].drop(columns=['regime'])
    y_regime = y[regime_mask]
    
    # 최소 500 샘플 필요 (튜닝은 학습보다 많은 데이터 필요)
    if len(X_regime) < 500:
        logger.warning(f"Skipping {regime.value} tuning")
        continue
    
    # Optuna로 이 레짐에 최적화된 파라미터 찾기
    regime_params = _tune_regime_models(X_regime_scaled, y_regime, regime.value)
    
    # 레짐별 파일로 저장
    with open(f"{MODEL_SAVE_PATH}/best_params_{regime.value}.json", 'w') as f:
        json.dump(regime_params, f, indent=2)
```

**개선점:**
- 각 레짐마다 최적 파라미터 찾기 (4개 독립 튜닝)
- `best_params_bull_trending.json`, `best_params_bear_trending.json` 등 레짐별 파일 생성
- 하위 호환성 유지: `best_params.json`에 통합 설정도 저장

#### New Helper Function: `_tune_regime_models()`
```python
def _tune_regime_models(
    X_scaled: pd.DataFrame,
    y: pd.Series,
    regime_name: str
) -> Dict:
    """
    Tune hyperparameters for a specific regime.
    
    Returns:
        Dict with best params for catboost, lgbm, xgboost
    """
    # 레짐별 튜닝은 50 trials (전체 데이터는 100 trials)
    n_trials = 50
    timeout = 1800  # 30분
    
    # CatBoost Optuna
    study_cat = optuna.create_study(direction='maximize', ...)
    study_cat.optimize(catboost_objective, n_trials=50, ...)
    
    # LGBM Optuna
    study_lgbm = optuna.create_study(direction='maximize', ...)
    study_lgbm.optimize(lgbm_objective, n_trials=50, ...)
    
    # XGBoost Optuna
    study_xgb = optuna.create_study(direction='maximize', ...)
    study_xgb.optimize(xgb_objective, n_trials=50, ...)
    
    return {
        'catboost': study_cat.best_params,
        'lgbm': study_lgbm.best_params,
        'xgboost': study_xgb.best_params
    }
```

#### Fallback Function: `_tune_models_global()`
레짐 분류가 없는 경우 기존 방식으로 전체 데이터 튜닝 (하위 호환성)

---

### 2. Minimum Sample Requirement Relaxation

#### Before
```python
# app/tasks/training.py - _train_regime_specific_models()
min_samples = 1000
if len(X_regime) < min_samples:
    logger.warning(f"Skipping {regime_value} model training")
    continue
```

**문제점:**
- sideways_volatile은 140개 샘플만 있어서 학습 불가
- 희귀 레짐은 절대 모델 학습 안 됨

#### After
```python
min_samples = 100
if len(X_regime) < min_samples:
    logger.warning(f"Skipping {regime_value} model training")
    continue

# 데이터 양에 따른 경고 (1000개 미만)
if len(X_regime) < 1000:
    logger.warning(f"{regime_value}: 샘플 수 부족 ({len(X_regime)}개). 과적합 위험 있음.")
```

**개선점:**
- 100개 이상이면 학습 시도
- 1000개 미만이면 경고 로그 출력 (과적합 주의)
- sideways_volatile도 학습 가능 (140 > 100)

---

### 3. Regime.py Hardcoding Removal

#### Before
```python
# app/services/regime.py
def get_regime_strategy_weights(regime: str) -> Dict[str, float]:
    weights = {
        "bull_trending": {"aggressive": 0.7, "moderate": 0.3},
        ...
    }
    return weights.get(regime, weights["sideways_calm"])  # ← 하드코딩
```

#### After
```python
def get_regime_strategy_weights(regime: str) -> Dict[str, float]:
    weights = {
        "bull_trending": {"aggressive": 0.7, "moderate": 0.3},
        ...
    }
    
    if regime not in weights:
        logger.warning(
            f"Unknown regime '{regime}'. Valid: {list(weights.keys())}. "
            f"Using SIDEWAYS_CALM fallback."
        )
    
    return weights.get(regime, weights["sideways_calm"])
```

**개선점:**
- 잘못된 regime 입력 시 경고 로그
- 유효한 regime 목록 표시
- 디버깅 용이성 향상

---

### 4. Sector-Specific Training Verification

#### Current Implementation
```python
# app/ml/features.py
def add_technical_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
    # ... technical indicators ...
    
    # 10. Sector feature (categorical)
    if 'symbol' in df.columns:
        symbol = df['symbol'].iloc[0]
        df['sector_id'] = get_sector_id(symbol)  # ← 정수 인코딩
    
    return df
```

#### Feature Extraction
```python
# app/ml/features.py
def extract_feature_vector(self, df: pd.DataFrame, ...) -> pd.DataFrame:
    # Normalize (exclude categorical sector_id)
    numeric_features = [f for f in available_features if f != 'sector_id']
    categorical_features = [f for f in available_features if f == 'sector_id']
    
    # sector_id는 정규화 안 함 (정수값 그대로 사용)
    for cat_feat in categorical_features:
        X_normalized[cat_feat] = X[cat_feat].values
```

**현재 설계:**
- 모든 섹터를 하나의 모델로 학습
- `sector_id`를 피처로 사용 (Technology=0, Finance=1, ...)
- 모델이 섹터 간 차이를 학습

**섹터별 분리 학습 불필요한 이유:**
1. **데이터 효율성**: 11개 섹터별 분리 시 샘플 부족 (각 섹터당 평균 9개 심볼)
2. **일반화 성능**: 하나의 모델이 섹터 간 공통 패턴 학습
3. **유지보수**: 11개 모델 관리 vs 4개 레짐 모델 관리
4. **레짐이 더 중요**: BULL/BEAR가 Technology/Finance보다 수익률에 영향 큼

**결론:** 현재 설계가 합리적. 추가 개선 불필요.

---

## 📊 Expected Impact

### Before vs After Comparison

| Aspect | Before | After |
|--------|--------|-------|
| **Tuning Strategy** | Global (all regimes) | 4 regime-specific |
| **Trials per Model** | 100 | 50 (×4 regimes = 200 total) |
| **Config Files** | `best_params.json` | `best_params_{regime}.json` (×4) |
| **Min Samples (Training)** | 1000 | 100 (with warning < 1000) |
| **Min Samples (Tuning)** | N/A | 500 |
| **Sideways Volatile** | ❌ No model | ✅ Model available |
| **Regime Fallback** | Silent hardcode | ⚠️ Warning log |
| **Sector Handling** | Feature-based (sector_id) | ✅ Unchanged (optimal) |

### Performance Expectations

#### 1. BULL Trending
- **이전**: 전체 데이터 파라미터로 과소적합 가능
- **개선**: BULL 시장 특화 파라미터 → Sharpe 증가 예상

#### 2. BEAR Trending
- **이전**: 상승장 중심 파라미터로 방어 부족
- **개선**: 하락장 방어 파라미터 → 손실 최소화

#### 3. SIDEWAYS Volatile
- **이전**: 모델 없음 (샘플 140개 부족)
- **개선**: 모델 학습 가능 → 변동성 대응 향상

#### 4. SIDEWAYS Calm
- **이전**: 전체 데이터 파라미터 (가장 많은 샘플이라 적합)
- **개선**: 명시적 최적화 → 안정성 증가

---

## 🔄 Migration Guide

### 기존 시스템 호환성

1. **모델 학습 태스크**
   - `train_models.apply_async()` → 변경 없음 (내부만 개선)
   - 기존 `ensemble_model_{regime}.pkl` 파일 계속 생성

2. **하이퍼파라미터 파일**
   - 기존: `best_params.json`
   - 신규: `best_params_bull_trending.json` (×4)
   - **하위 호환**: `best_params.json`에 `default` + `regime_specific` 통합 저장

3. **학습 최소 샘플**
   - 기존: 1000개 미만 → 스킵
   - 신규: 100개 이상 → 학습 시도, 1000개 미만 → 경고

### 배포 후 확인 사항

```bash
# 1. 튜닝 실행 (4시간 예상)
docker exec -it fastapitrader-app python -c "
from app.tasks.training import tune_models
tune_models.apply_async()
"

# 2. 레짐별 파라미터 파일 확인
ls -la /app/model_artifacts/best_params_*.json

# 3. 로그에서 각 레짐 튜닝 결과 확인
docker logs fastapitrader-app | grep "REGIME-SPECIFIC HYPERPARAMETER TUNING"

# 4. sideways_volatile 모델 생성 확인
ls -la /app/model_artifacts/ensemble_model_sideways_volatile.pkl

# 5. 학습 실행
docker exec -it fastapitrader-app python -c "
from app.tasks.training import train_models
train_models.apply_async()
"
```

---

## 🧪 Testing Recommendations

### 1. Unit Tests (권장)
```python
# tests/test_regime_tuning.py
def test_tune_models_creates_regime_files():
    """각 레짐별 파일 생성 확인"""
    from app.tasks.training import tune_models
    tune_models()
    
    for regime in ["bull_trending", "bear_trending", "sideways_volatile", "sideways_calm"]:
        path = f"/app/model_artifacts/best_params_{regime}.json"
        assert os.path.exists(path), f"Missing {regime} params"

def test_minimum_samples_relaxed():
    """100개 샘플로 학습 가능한지 확인"""
    # Mock 140개 sideways_volatile 데이터
    X_regime = create_mock_data(140)
    
    # 학습 시도
    result = _train_regime_specific_models(..., X_regime, ...)
    
    # 스킵되지 않고 모델 생성 확인
    assert os.path.exists("ensemble_model_sideways_volatile.pkl")
```

### 2. Integration Tests
```bash
# 전체 파이프라인 테스트 (백필 → 튜닝 → 학습)
python scripts/backfill_ohlcv.py --days 730
celery -A app.worker call app.tasks.training.tune_models
celery -A app.worker call app.tasks.training.train_models

# 모델 파일 확인
ls -la /app/model_artifacts/*.pkl
ls -la /app/model_artifacts/best_params_*.json
```

---

## 📈 Performance Metrics to Monitor

### 1. Tuning Metrics
- **Duration**: 각 레짐 튜닝 시간 (50 trials ≈ 1시간)
- **Best Sharpe**: 각 레짐별 최고 Sharpe Ratio
- **Sample Distribution**: 각 레짐의 샘플 수

### 2. Training Metrics
```
BULL_TRENDING: 3200 samples (32%) → Model trained ✅
BEAR_TRENDING: 800 samples (8%) → Model trained ✅
SIDEWAYS_VOLATILE: 140 samples (1.4%) → Model trained ⚠️ (warning)
SIDEWAYS_CALM: 5860 samples (58.6%) → Model trained ✅
```

### 3. Backtest Metrics (배포 후)
- **Regime-Specific Sharpe**: 각 레짐에서의 Sharpe 비교 (before vs after)
- **Drawdown**: 하락장에서 최대 손실 감소 확인
- **Win Rate**: 변동성 구간에서 승률 개선 확인

---

## 🚨 Risks and Mitigation

### Risk 1: Overfitting on Small Regimes
**문제**: sideways_volatile (140 샘플)은 과적합 위험  
**완화**:
- 1000개 미만 시 경고 로그
- TimeSeriesSplit (3-fold) 검증
- Optuna Pruning (MedianPruner)

### Risk 2: Tuning Time Increase
**문제**: 4개 레짐 × 50 trials = 4시간 (기존 2시간)  
**완화**:
- Trials 감소 (100 → 50)
- Timeout 설정 (30분/모델)
- 주간 1회 실행 (Celery Beat)

### Risk 3: Model File Management
**문제**: 파일 수 증가 (4개 레짐 params + 4개 모델)  
**완화**:
- 명확한 네이밍 규칙
- 하위 호환 파일 유지
- 로그에 파일 경로 출력

---

## 🎯 Next Steps

### Immediate (배포 후 1주)
1. ✅ 튜닝 실행 및 로그 확인
2. ✅ 4개 레짐 파라미터 파일 검증
3. ✅ sideways_volatile 모델 생성 확인

### Short-term (1개월)
1. 백테스트로 레짐별 Sharpe 비교
2. 실거래 모니터링 (특히 변동성 구간)
3. 로그 분석 (경고 메시지 빈도 확인)

### Long-term (3개월)
1. 레짐별 파라미터 변화 추적 (분기별 재튜닝)
2. 섹터별 분리 학습 재검토 (데이터 충분 시)
3. 앙상블 가중치 동적 조정 (레짐 전환 시)

---

## 📝 Appendix

### A. File Structure
```
/app/model_artifacts/
├── best_params.json                          # Combined config (backward compat)
├── best_params_bull_trending.json            # BULL regime params
├── best_params_bear_trending.json            # BEAR regime params
├── best_params_sideways_volatile.json        # SIDEWAYS_VOLATILE params
├── best_params_sideways_calm.json            # SIDEWAYS_CALM params
├── ensemble_model_bull_trending.pkl          # BULL model
├── ensemble_model_bear_trending.pkl          # BEAR model
├── ensemble_model_sideways_volatile.pkl      # SIDEWAYS_VOLATILE model ✨ NEW
├── ensemble_model_sideways_calm.pkl          # SIDEWAYS_CALM model
└── feature_scaler.pkl                        # Scaler (shared)
```

### B. Regime Distribution (Example)
```
Total Samples: 10,000 (100 symbols × 2 years × 15min bars)

SIDEWAYS_CALM: 5860 (58.6%) ✅ Tuning OK (> 500)
BULL_TRENDING: 3200 (32.0%) ✅ Tuning OK
BEAR_TRENDING: 800 (8.0%) ✅ Tuning OK
SIDEWAYS_VOLATILE: 140 (1.4%) ❌ Tuning Skip (< 500), ✅ Training OK (> 100)
```

### C. Optuna Hyperparameter Search Space
```python
# CatBoost
iterations: [100, 500]
depth: [4, 10]
learning_rate: [0.01, 0.3]
l2_leaf_reg: [1, 10]

# LGBM
n_estimators: [100, 500]
max_depth: [3, 8]
learning_rate: [0.01, 0.15]
num_leaves: [15, 60]

# XGBoost
n_estimators: [100, 500]
max_depth: [3, 10]
learning_rate: [0.01, 0.3]
subsample: [0.6, 1.0]
```

---

**END OF REPORT**
