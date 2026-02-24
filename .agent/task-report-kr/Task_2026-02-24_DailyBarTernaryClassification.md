# 작업 보고서: 일봉 전환 + 3진 분류 시스템 구축
**날짜**: 2026-02-24  
**세션**: 5  
**상태**: ✅ 완료  
**범위**: Cross-cutting 리팩토링 — 14개 이상 파일 수정

---

## 요약

15분봉에서 일봉으로의 전체 시스템 전환과 회귀(VotingRegressor) → 분류(VotingClassifier) 변환을 완료했습니다. ML 파이프라인은 이제 연속적인 signal 값 대신 3진 예측(UP / NEUTRAL / DOWN)과 softmax confidence 점수를 출력합니다. 데이터 수집, 학습, 예측, 거래 로직 모두 일봉 타임프레임으로 업데이트되었습니다.

---

## 변경 사항 (15개 파일)

### 1. `app/ml/models.py` (360 → 1001줄)
- 5개 classifier wrapper 클래스 추가:
  - `CompatibleCatBoostClassifier` — pickle 호환 CatBoost 래퍼
  - `CatBoostClassifierWrapper` — class weights 포함 학습 인터페이스
  - `LGBMClassifierWrapper` — class weights 포함 LightGBM 분류기
  - `XGBoostClassifierWrapper` — class weights 포함 XGBoost 분류기
  - `EnsembleClassifierWrapper` — VotingClassifier(voting='soft') 오케스트레이터
- 상수: `DEFAULT_CLASS_WEIGHTS = {0: 1.5, 1: 0.5, 2: 1.5}`, `CLASS_NAMES = ["DOWN", "NEUTRAL", "UP"]`
- 기존 regressor 코드 모두 하위 호환성을 위해 보존

### 2. `app/tasks/training.py` (~1320줄)
- 학습 파이프라인 전체 classification 전환
- `CLASSIFICATION_THRESHOLD = 0.003` (3진 타겟 생성용)
- 타겟: `np.where(returns > threshold, 2, np.where(returns < -threshold, 0, 1))`
- SPY 타임프레임 `'1d'`로 변경
- 최소 샘플: 1000 → 300
- 정확도 기반 앙상블 가중치 계산
- `EnsembleClassifierWrapper` 학습 통합
- 분류 메트릭: `accuracy_score`, `f1_score` (macro)

### 3. `app/ml/predictor.py` (~340줄)
- 새 메서드: `predict_class()` → `(predicted_class, confidence, probabilities)` 반환
- `_classifier_map` — 새 모델 파일명 (`ensemble_classifier_{regime}.pkl`)
- `_load_models_from_disk` — classifier 우선, 레거시 regressor fallback
- `predict_next()` 하위 호환성 유지

### 4. `app/services/regime.py` (130줄)
- 일봉 기준 임계값 업데이트: ADX 18→25, ATR 0.015→0.03, price_change 0.005→0.02

### 5. `app/core/config.py` (134줄)
- `REGIME_TRADING_CONFIG` 재설계:
  - `buy_threshold`/`sell_threshold` 제거 (float signal 방식)
  - `confidence_threshold` 추가 (국면별 0.40–0.60)
  - `min_hold_days` 추가 (국면별 1–2일)

### 6. `app/services/risk_manager.py` (547줄)
- `min_hold_bars`: 4 → 2, `cooldown_bars`: 4 → 1
- `bars_per_cycle`: 15 → 1440, `_entry_time_ttl`: 86400 → 604800

### 7. `app/repositories/stock_repo_sync.py` (181줄)
- 기본 timeframe: `'15m'` → `'1d'`

### 8. `app/repositories/portfolio_repo.py` (276줄)
- 기본 timeframe: `'15m'` → `'1d'`

### 9. `app/services/portfolio_optimizer.py` (448줄)
- 모든 timeframe 파라미터: `'15m'` → `'1d'`

### 10. `app/worker.py` (157줄)
- Celery Beat 스케줄 전면 개편:
  - 15분 수집 작업 제거
  - `daily_ohlcv` 추가 (17:00 ET, 장 마감 후)
  - `market_scan` → 1일 1회 (10:00 ET)
  - `trailing_stops` → 1일 2회 (10:00, 15:00 ET)

### 11. `app/tasks/realtime_data.py` (158줄)
- 완전 재작성: `collect_15m_realtime` → `collect_daily_ohlcv`
- `TimeFrame.Day`, 장 마감 후 단일 실행, DB 참조 `'1d'`

### 12. `app/tasks/trading.py` (214줄)
- 타임프레임 `'15m'` → `'1d'`, 일봉 기준 시간 창 조정

### 13. `app/tasks/market_analysis.py` (129줄)
- 타임프레임 `'15m'` → `'1d'`, 최소 바 100→20, 거래량 임계값 100K→1M

### 14. `app/api/v1/endpoints/rag.py` (686줄)
- SPY 및 종목 데이터 타임프레임 `'15m'` → `'1d'`, 최소 바 500→50

### 15. `app/services/trading_strategy_sync.py` (1001 → 1055줄)
- 분류 시스템 대규모 재작성:
  - `predict_class()` 사용 (기존 `predict_next()` 대체)
  - `_execute_trade_logic` — class/confidence/probabilities 기반
  - `_calculate_adjusted_confidence` — `_calculate_adjusted_signal` 대체
  - 캐시 TTL 300→3600초, 데이터 윈도우 30d→365d, 최소 바 500→50

---

## QA 결과: ✅ 통과

| 검사 항목 | 결과 |
|-----------|------|
| `app/` 내 `timeframe='15m'` 0건 | ✅ 확인 |
| `15분봉` 참조 0건 (시계 표현 제외) | ✅ 확인 |
| Classifier 하위 호환성 (regressor fallback) | ✅ 확인 |
| Config confidence threshold 재설계 | ✅ 확인 |
| 미사용 import 및 dead code 없음 | ✅ 확인 |
| 모든 신규 public 메서드에 type hint | ✅ 확인 |

---

## 영향

### 아키텍처
- **예측 모델**: 회귀(연속 signal) → 분류(이산 class + confidence)
- **데이터 단위**: 15분 인트라데이 → 일봉 (장 마감 후)
- **거래 빈도**: ~26회/일 → 종목당 1회/일
- **노이즈 감소**: 일봉이 모델 정확도를 저하시키던 장중 노이즈 제거

### 성능 (예상)
- **Signal 품질**: confidence threshold 기반 분류로 더 깔끔하고 실행 가능한 신호
- **거래 비용**: 대폭 감소 (1회/일 vs 최대 26회/일)
- **모델 학습**: 깨끗한 일봉 타겟으로 빠른 수렴 (300 샘플 충분 vs 1000)
- **운영 단순성**: 복잡한 15분 스케줄링을 단일 일봉 수집으로 대체

### 리스크 관리
- **최소 보유 기간**: 2 거래일 (기존 60분)
- **쿨다운**: 1 거래일 (기존 60분)
- **포지션 사이징**: confidence 기반 (40–60% 임계값), 기존 signal 크기 기반 대체

---

## 다음 단계 의존성
1. **일봉 데이터 백필** — `scripts/backfill_ohlcv.py`로 50–100 종목 `timeframe='1d'` 실행
2. **모델 재학습** — `EnsembleClassifierWrapper`를 사용한 새 classifier 모델 학습
3. **Dual-Timeframe Orchestrator** — 일봉 ML 방향 + 15분 진입 타이밍 (향후 단계)
