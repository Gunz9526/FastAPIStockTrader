# 크로스 레퍼런스 감사: 원본 감사 vs 현재 코드 상태

**날짜:** 2026-02-24  
**범위:** Session 5 이후 (Daily Bar + Ternary Classification 전환)  
**방법:** 관련 파일 전체 직접 코드 검사

---

## 요약

| 분류 | 건수 |
|---|---|
| **완료 (COMPLETED)** | 16 |
| **부분 해결 (PARTIALLY)** | 5 |
| **미해결 (STILL VALID)** | 2 |
| **해당없음 (N/A)** | 0 |
| **신규 이슈 (NEW)** | 3 |

**총 감사 항목:** 23개 → **69.6% 완료**, **21.7% 부분 해결**, **8.7% 미해결**

---

## Section 2: 치명적 이슈 (P0) — 전체 완료

| ID | 원본 이슈 | 상태 | 증거 | 비고 |
|---|---|---|---|---|
| 2.1 | 모델 성능 근본 결함 (bull 48.78%, bear Sharpe 10) | **완료** | training.py L746: `_calculate_composite_score` = 0.40*acc + 0.40*f1 + 0.20*class_balance | Regression→Classification 전환으로 해결 |
| 2.2 | Feature mismatch (학습 27 vs 추론 25) | **완료** | 학습: training.py L556 `feature_set="base"`. 추론: trading_strategy_sync.py L229 `feature_set="base"` | 둘 다 27개 base feature 사용 |
| 2.3 | Position sizing `base_qty = 1` 고정 | **완료** | trading_strategy_sync.py L557: `risk_manager.calculate_position_size()` + Kelly L777 | ATR + 포트폴리오 리스크 + Kelly 동적 사이징 |
| 2.4 | Sentiment/Fundamentals 이중 스케일링 | **완료** | trading_strategy_sync.py L387-487: 신뢰도 modifier로 분리 적용 | 학습에서 제외, 추론에서만 confidence 조정 |
| 2.5 | Walk-Forward scaler look-ahead bias | **완료** | training.py L554: `fit_scaler=True` 학습 fold에서만 | 폴드별 scaler fitting |

## Section 3: 높은 우선순위 (P1) — 1건 미완료

| ID | 원본 이슈 | 상태 | 증거 | 잔여 우선순위 | 비고 |
|---|---|---|---|---|---|
| 3.1 | Regime 분류 O(N) 비효율 | **완료** | training.py L211: SPY 바 단위 사전 계산 + `pd.merge_asof` | — | |
| 3.2 | PredictorService 싱글톤 thread safety | **완료** | predictor.py L30: `threading.RLock`, L162: atomic reload | — | |
| 3.3 | RiskManager in-memory only | **완료** | risk_manager.py L370: Redis → in-memory fallback 패턴 | — | |
| 3.4 | REGIME_STRATEGY_WEIGHTS 미사용 | **완료** | trading_strategy_sync.py L426에서 활발히 사용 | — | |
| 3.5 | Trailing stop 미작동 | **완료** | tasks/trading.py L75-214: ATR 기반 완전 구현 | — | |
| 3.6 | Backtest 엔진 현행 모델 미사용 | **부분** | ml_strategy.py L139: `predict_next()` (회귀) 사용 중 | **P1** | `predict_class()` 전환 필요 |

## Section 4: 중간 우선순위 (P2)

| ID | 원본 이슈 | 상태 | 잔여 우선순위 | 비고 |
|---|---|---|---|---|
| 4.1 | HTTP middleware 이중 로깅 | **완료** | — | 단일 metrics_middleware만 존재 |
| 4.2 | Transaction isolation 미설정 | **부분** | P2 | 분산 락 사용하나 DB isolation level 미명시 |
| 4.3 | Kelly Criterion mock data fallback | **부분** | P2 | 실제 OHLCV + SMA crossover 시뮬레이션 (합리적이나 합성 데이터) |
| 4.4 | Correlation matrix 날짜 정렬 | **완료** | — | 날짜 인덱스 기반 concat + dropna |
| 4.5 | Optuna Sharpe만 최적화 | **완료** | — | 복합 메트릭 (accuracy + F1 + class balance) |

## Section 5: 낮은 우선순위 (P3)

| ID | 원본 이슈 | 상태 | 잔여 우선순위 | 비고 |
|---|---|---|---|---|
| 5.1 | Dead code (strategies.py) | **부분** | P3 | strategies.py 301줄 전부 미사용 (import 0건). 삭제 권장 |
| 5.2 | Hardcoded 경로 | **부분** | P3 | features.py, predictor.py는 env var 사용. training.py만 하드코딩 |
| 5.3 | Celery error recovery | **완료** | — | max_retries + backoff + Discord 알림 |
| 5.4 | Test coverage (~45%) | **미해결** | **P2** | 분류 시스템 테스트 0건. P2로 상향 권장 |

## Section 6: 투자 전략 이슈

| ID | 원본 이슈 | 상태 | 잔여 우선순위 | 비고 |
|---|---|---|---|---|
| 6.1 | 15분봉 회귀 근본 문제 | **완료** | — | 전체 파이프라인 일봉 전환 완료 |
| 6.2 | 전략 오버홀 권장 | **완료** | — | Session 5에서 정확히 구현 |
| 6.3 | 모델 아키텍처 (SHAP, purged CV) | **부분** | P2 | Classification ✓, SHAP ✗, Purged CV ✗ |

---

## 신규 이슈 (전환 과정에서 발견)

| ID | 이슈 | 우선순위 | 설명 |
|---|---|---|---|
| N1 | Backtest `predict_next()` → `predict_class()` 미전환 | **P1** | backtest/ml_strategy.py가 회귀 인터페이스 사용. 프로덕션과 불일치 |
| N2 | `predictor.retrain()` 레거시 `EnsembleWrapper` 사용 | **P2** | Classification 파이프라인에 `EnsembleClassifierWrapper` 사용해야 함 |
| N3 | portfolio_optimizer docstring "15-min bars" 잔존 | **P3** | 코드는 정상 (1d), 문서만 구버전 |

---

## 권장 작업 순서

### P1 (즉시)
1. **Backtest 엔진 정렬** (3.6 + N1) — `predict_class()` + 분류 로직으로 전환

### P2 (단기)
2. **Test coverage 확보** (5.4) — 분류, confidence 조정, Kelly, trailing stop 테스트
3. **`predictor.retrain()` 수정** (N2) — `EnsembleClassifierWrapper`로 변경
4. **SHAP 통합** (6.3) — 학습 후 feature importance 분석
5. **Purged CV** (6.3) — 학습/검증 간 갭 추가

### P3 (유지보수)
6. **Dead code 제거** (5.1) — strategies.py 삭제
7. **하드코딩 경로** (5.2) — training.py에 env var 적용
8. **문서 수정** (N3) — docstring 업데이트
9. **Transaction isolation** (4.2) — 거래 쿼리에 명시적 격리 수준 설정
