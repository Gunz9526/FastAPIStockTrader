# 작업 보고서: 크리티컬 수정 구현

**날짜:** 2026-02-23  
**상태:** 완료

---

## 1. 변경 요약

### 1.1 피처 세트 통일 (P0 Critical)
- `process_symbol()`과 `process_portfolio()` 모두 `feature_set="legacy"` → `"base"` 변경
- 학습(27피처 momentum 포함)과 추론이 동일한 피처 세트 사용
- Sentiment/PE/PB/ROE는 ML 입력에서 제거, 기존 `_calculate_adjusted_signal()` 가중 조정에서 계속 사용

### 1.2 포지션 사이징 수정 (P0 Critical)
- `base_qty = 1` (항상 1주) → `RiskManager.calculate_position_size()` (ATR 기반 동적 사이징)
- 포트폴리오 가치의 2% 리스크 + ATR 기반 변동성 반영 + 레짐 스케일 적용

### 1.3 스케일러 Look-Ahead Bias 수정 (P0 Critical)
- CV 루프 밖에서 전체 데이터에 `fit_scaler=True` → 각 fold 안에서 학습 데이터에만 fit
- 프로덕션 최종 앙상블은 전체 데이터로 스케일러 fit (정상)

### 1.4 레짐 분류 벡터화 (P1 High)
- O(N×M) 개별 샘플 루프 → O(M) SPY 사전 계산 + `pd.merge_asof`
- 100K+ 샘플 기준 ~10배 속도 향상

### 1.5 PredictorService 수정 (P1 High)
- 존재하지 않는 `self._model_path` 참조하던 `retrain_weighted()` 제거
- 레짐 파라미터를 받는 단일 `retrain()` 메서드로 통합

### 1.6 데드코드 제거 (P1 High, ~277줄)
- `_walk_forward_validation()` (미사용 62줄)
- `_walk_forward_validation_enhanced()` (미사용 130줄)
- `WALK_FORWARD_PERIODS` 상수 (5줄)
- `REGIME_STRATEGY_WEIGHTS` + `REGIME_RISK_PARAMS` + accessor 함수 (~80줄)

---

## 2. Sentiment/Fundamentals 의견

**추천: ML 피처가 아닌 외부 트레이딩 신호 조정기로 유지**
- 과거 OHLCV 데이터에는 sentiment/fundamentals 없음 → 학습 불가
- 현재 `_calculate_adjusted_signal()` (ML 75% + Sentiment 15% + Fundamentals 10%) 방식이 아키텍처적으로 적절
- 코드 변경 불필요

---

## 3. 리빌드 vs 개선 의견

**추천: 개선 (리빌드 X)**
- 인프라 견고함: Docker, Celery, Redis, TimescaleDB, 분산 락, Circuit Breaker
- 문제는 3개 파일에 집중됨 → 수술적 수정으로 해결 가능
- 리빌드 비용 3-4주 vs 수정 비용 1-2일

**다음 세션 작업:**
- RiskManager cooldown Redis 영속화
- Optuna objective에 accuracy/max-drawdown 추가
- Backtest engine 현재 모델 연동
- 테스트 커버리지 확대

---

## 4. 수정된 파일

| 파일 | 변경 내용 |
|---|---|
| `app/services/trading_strategy_sync.py` | 피처 세트 → base, 포지션 사이징 → RiskManager |
| `app/tasks/training.py` | 데드코드 제거, 스케일러 바이어스 수정, 레짐 벡터화 |
| `app/ml/predictor.py` | retrain() 메서드 수정 |
| `app/services/regime.py` | 데드코드 제거 |
