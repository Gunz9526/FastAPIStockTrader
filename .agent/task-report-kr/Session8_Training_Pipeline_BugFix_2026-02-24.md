# 세션 8 작업 보고서: 학습 파이프라인 버그 수정

**날짜**: 2026-02-24  
**단계**: J.2.1 (학습 파이프라인 버그 수정)  
**상태**: ✅ 완료  

---

## 목표

Docker worker 학습 로그에서 발견된 3개 critical 에러 수정 + 심층 로직 점검으로 숨겨진 버그 발견 및 수정.

---

## 수정된 버그 (총 8개 + 추가 2개)

### 원본 3개 에러 (worker 로그에서)

| # | 에러 | 근본 원인 | 수정 |
|---|------|----------|------|
| 1 | LightGBM categorical_feature train/predict 불일치 | `predict()`에서 `sector_id`를 `category` dtype으로 변환하지 않음 | `_prepare_categorical_for_predict()` 헬퍼를 모든 predict/predict_proba에 추가 |
| 2 | Feature 이름 불일치 (25 vs 27 features) | 검증에서 `feature_set="legacy"` 사용 (기본값) → `"base"` 아님 | 검증 `extract_feature_vector` 호출에 `feature_set="base"` 추가 |
| 3 | CatBoost "Input data must have at least one feature" | 버그 2의 연쇄 → scaler.transform() 실패 | 버그 2 해결로 자동 수정 |

### 숨겨진 5개 버그 (심층 로직 감사에서 발견)

| # | 심각도 | 버그 | 수정 |
|---|--------|------|------|
| 4 | **CRITICAL** | Regime 튜닝 params JSON 구조 불일치 → Optuna 결과 무시됨 | `_load_regime_params()` 5단계 fallback 검색 |
| 5 | **HIGH** | 단일 `feature_scaler.pkl`이 4개 regime에 의해 덮어씌워짐 | `scaler_suffix` 파라미터 → `feature_scaler_{regime}.pkl` |
| 6 | **HIGH** | `tune_models()`와 `_tune_models_global()`이 잘못된 feature_set 사용 | 모든 tuning 경로에 `feature_set="base"` 추가 |
| 7 | **HIGH** | `best_params_{regime}.json` 파일이 로드되지 않음 | 버그 4 수정에 통합 (`_load_regime_params()`) |
| 8 | **MEDIUM** | 학습 중 `relative_volume` 항상 1.0 | 조건부 덮어쓰기: DataFrame에 이미 존재하면 건드리지 않음 |

### PM 직접 발견 추가 수정

| # | 설명 |
|---|------|
| 9 | 추론 경로 `scaler_suffix` — 4개 호출지점 모두 업데이트 (trading_strategy_sync ×2, backtest, RAG) |
| 10 | Scaler-모델 regime 정합성 — fallback을 scaling 이전에 해결하여 scaler가 모델의 regime과 일치 |

---

## 수정된 파일

| 파일 | 변경 사항 |
|------|----------|
| `app/ml/models.py` | `_prepare_categorical_for_predict()`, 모든 predict/predict_proba categorical 준비 |
| `app/ml/features.py` | `scaler_suffix` 파라미터, `relative_volume` 조건부 덮어쓰기 |
| `app/tasks/training.py` | `_load_regime_params()`, `feature_set="base"` 전체 적용, `scaler_suffix=regime_value` |
| `app/services/trading_strategy_sync.py` | `scaler_suffix=effective_regime`, fallback-before-scaling 로직 |
| `app/backtest/ml_strategy.py` | `scaler_suffix=regime_suffix` |
| `app/api/v1/endpoints/rag.py` | `scaler_suffix=regime_suffix` |

---

## QA 결과

| 파일 | 에러 | 상태 |
|------|------|------|
| `app/ml/models.py` | 0 | ✅ PASS |
| `app/ml/features.py` | 0 | ✅ PASS |
| `app/tasks/training.py` | 0 | ✅ PASS |
| `app/services/trading_strategy_sync.py` | 0 | ✅ PASS |
| `app/backtest/ml_strategy.py` | 0 | ✅ PASS |
| `app/api/v1/endpoints/rag.py` | 0 | ✅ PASS |

---

## 🎯 모델 방향성 분석: Regime × Sector 분할 여부

### 질문
> "현재 통합 모델, 섹터별로 다른 종목들이 여러개 추가되어있음. 레짐별, 섹터별 모델을 나누는게 나은지?"

### 결론: **현재 아키텍처 유지 (4 regime 모델 + sector_id categorical feature)**

### 근거

#### 1. 데이터 부족 문제 (결정적 이유)
| 구분 | 수치 |
|------|------|
| 총 데이터 | ~30,000 샘플 (60종목 × ~500일봉) |
| Regime별 | ~7,500 (불균형: bear_trending ~15%, sideways_calm ~35%) |
| **Regime × Sector별** | **~577 (평균), 실제 375~3,000 (불균형 심각)** |

- Basic Materials, Real Estate, Utilities: 각 3종목 → regime당 **~375 샘플**
- Walk-Forward validation 최소 train+val ≈ 300 → **대부분의 셀에서 검증 불가능**
- Ternary classification (3클래스) + ~50% NEUTRAL → 소규모 모델에서 **클래스 불균형 폭증**

#### 2. Gradient Boosting의 본질적 장점
- CatBoost/LightGBM/XGBoost의 트리 분할은 자연스럽게 `sector_id`별 패턴을 학습
- 예: "sector=Technology AND RSI<30 → UP 확률 ↑" — 이것이 바로 **모델을 나누는 것과 동일한 효과**
- **차이점**: 통합 모델은 다른 섹터의 정보를 **공유** (Transfer Learning 효과)

#### 3. Native Categorical Encoding
- 현재 **CatBoost의 Ordered Target Statistics**가 sector_id를 처리
- 이는 sector별 target 확률을 학습하면서도 overfitting을 방지하는 기법
- 모델 분할 시 이 정보 공유가 사라짐

#### 4. Cross-Sector 정보 손실
- 시장 전체(매크로) 패턴은 모든 섹터에 영향
- 통합 regime 모델은 이 공통 패턴을 학습한 위에 sector-specific 조정을 학습
- 분할하면 **공통 패턴 학습에 필요한 데이터가 1/13로 축소**

#### 5. 컴퓨팅 비용
| 아키텍처 | 모델 수 | Estimator 수 | 예상 학습 시간 |
|----------|---------|-------------|---------------|
| **현재 (4 regime)** | 4 | 12 | ~2-4시간 |
| Regime × Sector | 52 | 156 | **~20시간+** |
| + Optuna tuning | 52 × 100trials | — | **수일** |

- 4-core CPU, 24GB RAM 서버에서 **비현실적**

#### 6. 향후 발전 방향 (Phase M)
- **M.2 SHAP Feature Selection**: regime 모델 내에서 sector별 feature importance 분석
- **M.1 Cross-Sectional Momentum**: sector rotation signal을 **feature로** 추가 (모델 경계가 아닌 입력 차원에서 해결)
- **Adaptive Thresholds (M.3)**: sector별 confidence_threshold 차별화 가능

### 최종 권장사항

```
현재 아키텍처 ────────── 4 Regime 모델 × (CatBoost + LightGBM + XGBoost)
                         └── sector_id (0-12) = Native Categorical Feature
                         └── 27 base features (scaler regime별 분리)
                         └── 60종목의 공통+섹터별 패턴을 단일 모델 내에서 학습

미래 최적화 (Phase M) ── SHAP 기반 sector별 feature importance 분석
                         └── Sector Rotation Signal → 새로운 feature로 추가
                         └── Sector별 Confidence Threshold 차별화
```

**신뢰도**: 95% — 데이터 규모(30K 샘플)와 서버 제약(4-core CPU)을 고려하면 현재 아키텍처가 최적.
