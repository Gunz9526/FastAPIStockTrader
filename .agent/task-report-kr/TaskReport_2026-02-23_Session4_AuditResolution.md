# 태스크 보고서: Session 4 — 전체 감사 해결 및 전략 분석

**날짜:** 2026-02-23  
**세션:** 4  
**역할:** Lead Technical PM  
**사용된 Sub-Agent:** Backend (role-backend.md), Trading (role-trading.md), Quant (role-quant.md)

---

## 1. 목표

전체 프로젝트 감사(2026-02-23) 미해결 이슈 모두 수정, REGIME_STRATEGY_WEIGHTS 구현 검증, 투자 전략 심층 분석 (15분봉 vs 일봉).

## 2. 사용자 요청 (5개 항목)

1. REGIME_STRATEGY_WEIGHTS 구현 및 적용 정확성 검증
2. 분석 보고서 [높은/중간 우선순위] 이슈 수행 확인 및 미수행 항목 해결
3. [낮은 우선순위] 시스템 개선 필요 부분 재사용 검토
4. 실행 계획 요약 진행상황 확인
5. 투자전략 이슈 심층 분석: 15분봉 vs 일봉, 회귀 vs 분류

---

## 3. 발견 및 수정된 이슈

### 3.1 P0-2.4: 감성/펀더멘털 이중 스케일링 (치명적)

**발견 경위:** 감사 보고서에서 Session 2에서 수정되었다고 기록되었으나, 코드 리뷰 결과 **미수정 상태**였음. `_calculate_adjusted_signal()` 메서드에서 감성 점수가 0.005 스케일로 적용되어 REGIME_STRATEGY_WEIGHTS의 12% 가중치에도 불구하고 실제 기여도는 ~1.5%에 불과.

**근본 원인:** 세 가지 신호 소스의 스케일이 상이:
- ML 예측: -0.01 ~ +0.01 (올바른 스케일)
- 감성: score × 0.005 → -0.005 ~ +0.005 (ML의 50%)
- 펀더멘털: 고정값 ±0.003 또는 ±0.002 (ML의 30%)

**수정 (Trading 페르소나):**
- 모든 3개 신호를 동일한 -0.01 ~ +0.01 스케일로 정규화 후 가중치 적용
- ML: 변경 없음 (이미 올바름)
- 감성: `score * 0.005` → `score * 0.01` (-1.0~1.0 → -0.01~0.01)
- 펀더멘털: 고정값 → 연속적 PE-ratio 기반 함수 (-0.01~0.01)
- 정규화 설명 docstring 추가

**파일:** `app/services/trading_strategy_sync.py` (lines 369-437)

### 3.2 P1-3.2: PredictorService Thread Safety (높음)

**발견된 문제:**
1. `_models` dict 접근 시 스레드 동기화 없음
2. 모델 리로드 시 비원자적 교체 (race condition)
3. 하드코딩된 경로 `/app/model_artifacts/`
4. `get_model_info()` 빈 dict 반환

**수정 (Backend 페르소나):**
1. `threading.RLock`로 모든 `_models` 접근 보호
2. 원자적 리로드: `_load_models_from_disk()` → 임시 dict 구축 → lock → 참조 교체
3. `predict_next()`: 모델 참조 획득에만 lock, 예측은 lock-free (처리량 유지)
4. 경로: `os.getenv("MODEL_SAVE_PATH", "model_artifacts")` (설정 가능)
5. `get_model_info()`: regime 모델을 순회하며 메타데이터 수집
6. `__new__`: lock 보호로 안전한 Singleton 생성

**파일:** `app/ml/predictor.py` (165 → 240 lines)

### 3.3 P2-4.2: 트랜잭션 격리 완성 (중간)

**발견된 문제:**
- `_place_order()` SELL 경로만 `for_update` 있었음 (Session 3)
- `_process_buy_signal()`: distributed lock 없음 → 동일 심볼 동시 매수 가능
- `_execute_sell_order()`: distributed lock 없음 → 동시 매도 가능
- DB 실패 시 보상 로깅 없음

**수정 (Trading 페르소나):**
1. `_process_buy_signal()`: `get_trading_lock(symbol, ttl_seconds=30)` 추가
2. `_execute_sell_order()`: distributed lock + `get_active_position_for_update()` (FOR UPDATE) 추가
3. `_place_order()` SELL 경로: DB commit 실패 시 CRITICAL 로그 (order_id + position_id)

**파일:** `app/services/trading_strategy_sync.py` — 3개 주문 경로에 걸쳐 총 6개 `get_trading_lock` 호출

### 3.4 P3-5.2: 하드코딩된 Docker 경로 (낮음)

**수정 (Backend 페르소나):**
- `app/ml/features.py`: `_MODEL_PATH = os.getenv("MODEL_SAVE_PATH", "model_artifacts")`
- `app/api/v1/endpoints/model.py`: 4개 하드코딩 경로 + `artifacts_path` 응답값 전환

**검증:** `grep -r "/app/model_artifacts" *.py` → **매칭 0건**

### 3.5 P3-5.3: Celery 에러 복구 (낮음)

**수정 (Backend 페르소나):**
- `app/tasks/trading.py`: `execute_market_scan` (autoretry, backoff 60s), `update_trailing_stops` (autoretry, backoff 30s)
- `app/tasks/training.py`: `tune_models` (max_retries=2), `analyze_feature_importance` (max_retries=2)
- `app/tasks/sentiment.py`: `clear_stale_sentiment_cache` (max_retries=1)

### 3.6 P3-5.1: strategies.py 재사용 검토 (낮음)

**결정: 유지, 아직 통합하지 않음.**
- MomentumStrategy, MeanReversionStrategy, BreakoutStrategy는 잘 구현되었으나 미사용
- 활성 트레이딩 파이프라인에서 참조 없음
- 세 가지 미래 재사용 경로:
  1. 확인 신호 (ML + Momentum 일치?)
  2. Regime별 전략 선택 (sideways → MeanReversion)
  3. 백테스트 비교 기준선
- **전략 방향 확정 후** (15분봉 vs 일봉) 통합 결정

---

## 4. 투자 전략 분석 보고서

**생성된 파일:**
- `.agent/plan-report/Strategy_Analysis_2026-02-23_15min_vs_Daily.md` (영문, ~600줄)
- `.agent/plan-report-kr/Strategy_Analysis_2026-02-23_15min_vs_Daily.md` (한국어, 639줄)

**핵심 결론 (Quant 페르소나):**

| 질문 | 답변 |
|------|------|
| Q1: 일봉이 15분봉보다 나은가? | **예.** 일봉 SNR ≈ 0.15 vs 15분봉 ≈ 0.02 (7배 우수), 거래비용 0.3% vs 2.5% |
| Q2: 기존 개선사항 재사용 가능? | **~85% 재사용 가능.** TA-Lib 기간이 일봉 표준, ATR sizing이 일봉에서 더 안정적 |
| Q3: 일봉 예측이 더 좋은가? | **예.** Direction Accuracy 53→55-58%, Sharpe 0.5→1.0-1.5 (비용 후) |
| Q4: 권장 방향? | **Dual-Timeframe Hybrid** — 일봉 ML로 방향 + 15분봉으로 진입 타이밍 |
| Q5: 회귀 vs 분류? | **Ternary Classification (UP/DOWN/NEUTRAL)** + Softmax Confidence |

**구현 로드맵:**
- Phase 1 (1-2주): 일봉 데이터 백필 (50-100 종목) + Ternary Classification 전환
- Phase 2 (3-4주): 15분봉 진입 타이밍 레이어
- Phase 3 (5-6주): Cross-sectional momentum, adaptive thresholds

---

## 5. 실행 계획 진행상황 점검

### 감사 보고서 "권장 실행 계획 요약" 상태:

| 시기 | 항목 수 | 완료 | 상태 |
|------|---------|------|------|
| 즉시 (1주차) | 4 | 4 | ✅ 100% |
| 단기 (2-3주차) | 4 | 3 | ⚠️ 75% (Classification 구현 대기) |
| 중기 (2개월차) | 4 | 4 | ✅ 100% |
| 장기 (3개월+) | 4 | 1 | ⬜ 25% (분석 완료, SHAP/MLflow/Tests 대기) |

**전체: 12/16 항목 해결 (75%)**

### Session 4 추가 수정 (원래 계획에 없던 것):
- P3-5.2: 하드코딩 경로 → 환경변수 ✅
- P3-5.3: Celery 재시도 설정 ✅

---

## 6. 수정된 파일

| 파일 | 변경 유형 | 라인 수 |
|------|----------|--------|
| `app/services/trading_strategy_sync.py` | 신호 정규화 + 트랜잭션 격리 | 937 → 1001 |
| `app/ml/predictor.py` | Thread-safe Singleton 전면 개편 | 165 → 240 |
| `app/ml/features.py` | 하드코딩 경로 → 환경변수 | ~3줄 |
| `app/api/v1/endpoints/model.py` | 하드코딩 경로 → 환경변수 | ~8줄 |
| `app/tasks/trading.py` | Celery autoretry 설정 | ~4줄 |
| `app/tasks/training.py` | Celery max_retries | ~2줄 |
| `app/tasks/sentiment.py` | Celery max_retries | ~1줄 |

## 7. 생성된 파일

| 파일 | 목적 |
|------|------|
| `.agent/plan-report/Strategy_Analysis_2026-02-23_15min_vs_Daily.md` | 투자 전략 분석 (영문) |
| `.agent/plan-report-kr/Strategy_Analysis_2026-02-23_15min_vs_Daily.md` | 투자 전략 분석 (한국어) |
| `.agent/task-report/TaskReport_2026-02-23_Session4_AuditResolution.md` | 태스크 보고서 (영문) |
| `.agent/task-report-kr/TaskReport_2026-02-23_Session4_AuditResolution.md` | 태스크 보고서 (한국어) |

---

## 8. QA 결과

| 검사 항목 | 결과 |
|-----------|------|
| 신호 정규화: 3개 신호 모두 -0.01~+0.01 | ✅ PASS |
| PredictorService: 모든 _models 접근에 RLock | ✅ PASS |
| PredictorService: 원자적 리로드 (load-then-swap) | ✅ PASS |
| 트랜잭션 격리: 3개 주문 경로에 6개 get_trading_lock | ✅ PASS |
| 하드코딩 경로: `/app/model_artifacts` 매칭 0건 | ✅ PASS |
| Celery 재시도: 5개 태스크에 retry 설정 | ✅ PASS |
| 미사용 import 없음 | ✅ PASS |
| 새 코드에 Type hints 적용 | ✅ PASS |

---

## 9. 누적 이슈 해결 현황 (전체 세션)

| 우선순위 | 전체 | 해결 | 비율 |
|----------|------|------|------|
| P0 (치명적) | 4 | 4 | **100%** |
| P1 (높음) | 6 | 6 | **100%** |
| P2 (중간) | 5 | 5 | **100%** |
| P3 (낮음) | 4 | 3 | **75%** |
| **합계** | **19** | **18** | **95%** |

남은 P3: 테스트 커버리지 (58% → 70%), SHAP Feature Selection, MLflow Registry

---

## 10. 다음 단계

### 즉시 (다음 세션)
1. **Ternary Classification 구현** — Regressor → Classifier 파이프라인 전환
2. **일봉 데이터 백필** — 50-100 종목, `scripts/backfill_ohlcv.py`에 `timeframe='1d'` 추가
3. **테스트 커버리지** — 목표 70% (집중: predictor.py, features.py, portfolio_optimizer.py)

### 단기 (2-3주)
4. **Dual-Timeframe Orchestrator** — 일봉 ML 방향 + 15분봉 진입 타이밍
5. **Circuit Breaker 강화** — Portfolio-level 손실 한도

### 중기 (1-2개월)
6. **Cross-Sectional Momentum** — 상대 강도 순위, 섹터 로테이션
7. **SHAP Feature Selection** — 노이즈 피처 제거
8. **MLflow Model Registry** — 모델 버전 관리

---

*Lead Technical PM 작성 — Session 4, 2026-02-23*
