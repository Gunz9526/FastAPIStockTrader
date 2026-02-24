# 백엔드 로드맵 🚀

> **최종 업데이트**: 2026-02-24 (Session 9) | **현재 단계**: 🟡 Phase J (데이터 백필 & 모델 학습 — J.1/J.2 ✅, J.2.1 버그 수정 ✅, J.3.1 검증 무결성 ✅, J.3 재실행 대기)
> **스택**: Python 3.14 · FastAPI · PostgreSQL/TimescaleDB · Redis · Celery · CatBoost+LightGBM+XGBoost
> **서버**: 4-core CPU, 24GB RAM, GPU 없음
> **분류**: Ternary (UP=2 / NEUTRAL=1 / DOWN=0), θ=0.005, 일봉(1d), 27개 feature

---

## ✅ 완료된 단계 (A–I 요약)

| Phase | 이름 | 주요 산출물 | 완료일 |
|-------|------|------------|--------|
| A | 핵심 거래 시스템 | FastAPI + PostgreSQL/TimescaleDB + Alpaca API + TA-Lib | 2025 |
| B | 안정성 & 자동화 | Celery/Redis task queue, Beat scheduler, Docker 보안 | 2025 |
| C | 성능 최적화 | TimescaleDB hypertables, Redis 캐싱, Prometheus 메트릭 | 2025 |
| D | ML 코어 | Ensemble (CatBoost+LGBM+XGB), Optuna 튜닝, Backtrader 엔진 | 2025 |
| CI/CD | 빌드 파이프라인 | Conda env, GitHub Actions 3분 빌드, Docker multi-stage, Python 3.14 | 2026-01-22 |
| E | 프로덕션 강화 | 기본 circuit breaker, Redis persistence, 테스트 인프라 (44개, 58%) | 부분 완료 |
| F | 고급 AI | Sentiment (Gemini+Finnhub), Fundamentals (yfinance), VIX regime, Monte Carlo | 2026-01-05 |
| G | 일봉 + 분류 시스템 | 14+ 파일 일봉 전환, VotingClassifier(soft), regime별 confidence_threshold | 2026-02-24 |
| H | 시장 국면 인식 | 4-regime 감지 (SPY), regime별 classifier, bull fallback, 27 features | 2026-02-24 |
| I | 리스크 & 포지션 방어 | 최소 보유 2일, cooldown 1일, Kelly sizing, MPT 최적화, 5-포지션 포트폴리오 | 2026-01-05 |

---

## 📊 세션 이력

| 세션 | 날짜 | 요약 |
|------|------|------|
| 1 | 2026-01-05 | 핵심 인프라: Sentiment(Gemini+Finnhub), Fundamentals(yfinance), VIX, Feature Engineering, Monte Carlo |
| 2 | 2026-01-06 | Gemini SDK 마이그레이션, Feature pipeline 수정, SQL 로깅, Sector lookup |
| 3 | 2026-01-19 | 8개 감사 수정: Redis persistence, Optuna 다중목적, Trailing stops, Backtest regime, Kelly+Correlation 수정 |
| 4 | 2026-02-23 | 7개 감사 수정: Signal 정규화, Thread safety, Transaction 격리, 하드코딩 경로, Celery retry, 전략 분석 |
| 5 | 2026-02-24 | 일봉 전환 (14+ 파일), Ternary Classification (models.py/training.py/predictor.py/config.py/trading_strategy_sync.py) |
| 6 | 2026-02-24 | Classification 정리: Backtest ml_strategy.py, RAG endpoint, predictor.retrain(), strategies.py, Rule personas, Roadmap 재작성 |
| 7 | 2026-02-24 | Phase J 준비: 60종목 GICS 확장, backfill_ohlcv 일봉 지원, sector_map 17→62 (Unknown 99→12), Native categorical encoding (CatBoost/LightGBM/XGBoost + Ensemble), symbol_limit 10→None |
| 8 | 2026-02-24 | **8개 핵심 버그 수정**: LightGBM categorical predict 불일치, feature_set legacy→base, CatBoost 빈 데이터 연쇄, regime params JSON 구조, 단일 scaler→regime별 scaler, tuning feature_set 오류, regime param 파일 미로드, relative_volume 항상 1.0. + 추론 경로 scaler-regime 정합성 (6 파일, 0 에러) |
| 9 | 2026-02-24 | **학습 결과 분석 + 4개 수정**: Data Leakage 제거 (holdout 검증), NEUTRAL 클래스 복구 (CLASS_WEIGHTS 0.5→1.0), θ 조정 (0.003→0.005), min_samples 300→500. Dead code 정리 (미사용 import 6개 제거) |

---

## 🔍 감사 상태

### 해결: 19/19 P0 + 16/19 전체 원본 항목 ✅

| ID | 이슈 | 해결 방법 | 세션 |
|----|------|----------|------|
| P0-2.1 | 모델 성능 | Daily + Ternary Classification | 5 |
| P0-2.2 | Feature 불일치 | base_feature_columns 27=27 (학습=추론) | 5 |
| P0-2.3 | Position sizing | 동적 Kelly / confidence 기반 | 3+5 |
| P0-2.4 | Signal 정규화 | Classification이 signal weighting 대체 | 4+5 |
| P0-2.5 | Scaler look-ahead bias | Walk-Forward에서 fold별 scaling | 5 |
| P1-3.1 | Regime O(N) | Vectorized | 3 |
| P1-3.2 | Thread safety | RLock + 원자적 reload | 4 |
| P1-3.3 | RiskManager persistence | Redis | 3 |
| P1-3.4 | Dead code REGIME_STRATEGY_WEIGHTS | Dual-Timeframe 향후 사용 위해 보존 | 4 |
| P1-3.5 | Trailing stops | ATR 기반 전체 구현 | 3 |
| P1-3.6 | Backtest 정렬 | predict_class() + confidence | 6 |
| P2-4.1 | 이중 로깅 | 제거 | 3 |
| P2-4.2 | Transaction 격리 | 전체 주문 경로에 Distributed locks | 4 |
| P2-4.3 | Kelly mock 데이터 | SMA crossover 전략 | 3 |
| P2-4.4 | Correlation 정렬 | Date-indexed | 3 |
| P2-4.5 | Optuna 다중목적 | Composite score (Sharpe+Accuracy+MaxDD) | 3 |
| P3-5.2 | 하드코딩 경로 | env var (`MODEL_SAVE_PATH`) | 4 |
| P3-5.3 | Celery retry | 5개 task에 autoretry | 4 |

### 미해결 (3개 항목)

| ID | 이슈 | 목표 | 우선순위 |
|----|------|------|----------|
| P2 | 테스트 커버리지 58% | 70%+ | 중간 (Phase K.2) |
| P3 | DB index 최적화 | Partial indexes | 낮음 (Phase N) |
| P3 | Dead code 검토 | 미사용 코드 완전 제거 | 낮음 (Phase N) |

---

## 🔜 Phase J: 데이터 백필 & 모델 학습 (바로 다음 작업)

> **전제 조건**: 일봉 분류 모델이 아직 존재하지 않음. 다른 모든 작업보다 먼저 완료 필수.

### J.1 일봉 OHLCV 백필 ✅ (Session 7)
- [x] `scripts/backfill_ohlcv.py`에 `timeframe='1d'` 지원 추가 (default)
- [x] `--timeframe` CLI 인수 추가: `1d` | `15m` | `1h`
- [x] 심볼 유니버스 확장: 10 → 60 종목 (11개 GICS 섹터 + 시장 지수 ETF 2개)
- [x] 목표: 종목당 2년 이상 일봉 데이터 (≈500 바)
- [x] 섹터 분산: 11개 GICS 섹터 모두 포함
- [x] `verify_backfill()` 효율적 SQL COUNT/MIN/MAX 쿼리
- **파일**: `scripts/backfill_ohlcv.py`, `scripts/add_symbols.py`
- **실행**: `python scripts/add_symbols.py` → `python scripts/backfill_ohlcv.py --years 2 --timeframe 1d`

### J.2 심볼 유니버스 확장 ✅ (Session 7)
- [x] 60종목: Tech(12), CommSvc(4), ConsCycl(6), ConsDef(5), Fin(6), Health(6), Energy(4), Ind(6), BasMat(3), RE(3), Util(3), MktIdx(2)
- [x] `sector_map.py`: 17→62 항목, SECTOR_TO_ID 연속 0–12, `NUM_SECTORS=13`
- [x] GOOGL/META → Communication Services, AMZN → Consumer Cyclical (GICS 정확)
- [x] **Native categorical encoding**: CatBoost (Ordered Target Stats), LightGBM (native categorical), XGBoost (enable_categorical)
- [x] Ensemble `_train_with_categorical()`: estimator별 적절한 dtype/params로 개별 학습
- [x] `training.py` symbol_limit: 10 → None (전체 활성 심볼 사용)
- **파일**: `scripts/add_symbols.py`, `app/ml/sector_map.py`, `app/ml/features.py`, `app/ml/models.py`, `app/tasks/training.py`

### J.3 최초 모델 학습 (예상: 1일)
- [ ] `train_models` Celery task로 일봉 데이터 학습 실행
- [ ] `ensemble_classifier_{regime}.pkl` 생성 (4개 파일)
- [ ] 검증: regime별 accuracy ≥ 55%, F1 macro ≥ 0.45
- [ ] 클래스 분포 로깅 (불균형 확인: DOWN/NEUTRAL/UP 비율)
- [ ] CPU 예상: 4-core 서버에서 학습 1회당 ~2–4시간
- **파일**: `app/tasks/training.py`, `model_artifacts/`

---

## 📋 Phase K: 프로덕션 강화 (Phase J 이후)

### K.1 Circuit Breaker 강화 (예상: 2–3일)
- [ ] 일일 손실 한도: -3% 또는 -$500 (먼저 도달한 것)
- [ ] 연속 손실 한도: 1일 3회 → 거래 일시 중지
- [ ] API 지연 모니터링: >3000ms → 중단
- [ ] 트리거 시 Discord/Slack 알림
- **파일**: `app/services/circuit_breaker.py`

### K.2 테스트 커버리지 → 70% (예상: 3–4일)
- [ ] 우선 대상: `predictor.py` (predict_class), `features.py`, `portfolio_optimizer.py`
- [ ] 분류 전용 테스트 추가: class weights, confidence scores, feature 27개 검증
- [ ] `test_training_integration.py` classifier pipeline 업데이트
- [ ] 현재: 44개 테스트 (~58%) → 목표: ~65개 테스트 (70%+)

---

## 📋 Phase L: Dual-Timeframe Hybrid (중기, Phase K 이후)

> **의존성**: Phase J (모델 학습 완료) + Phase K (프로덕션 안전)

### L.1 Daily ML Signal Cache (예상: 1–2일)
- [ ] Redis 캐시: 일봉 예측 (class + confidence + probs), 24h TTL
- [ ] Celery task: `generate_daily_signals` — 17:30 ET (데이터 수집 후)

### L.2 15분 Rule-Based 진입 레이어 (예상: 5–7일)
- [ ] 진입 규칙: RSI < 35 + MACD cross-up (daily signal = UP일 때)
- [ ] 청산 규칙: Trailing stop 또는 즉시 (daily signal = DOWN일 때)
- [ ] 신규 클래스: `DualTimeframeOrchestrator`
- [ ] 필요: 15분 데이터 수집 재활성화 (장중만)

### L.3 백테스팅 검증 (예상: 3–4일)
- [ ] Dual-timeframe backtest engine
- [ ] 비교: Daily-only vs Hybrid 성능
- [ ] 거래 비용 민감도 분석

---

## 📋 Phase M: 고급 ML (장기)

### M.1 Cross-Sectional Momentum (예상: 5–7일)
- [ ] 50–100 종목 간 상대 강도 순위
- [ ] 섹터 로테이션 신호
- [ ] 상위 N% 종목 선택

### M.2 SHAP Feature Selection (예상: 2–3일)
- [ ] SHAP 값 기반 노이즈 feature 제거
- [ ] Regime별 feature importance

### M.3 Adaptive Thresholds (예상: 3–4일)
- [ ] Optuna로 regime별 CLASSIFICATION_THRESHOLD (θ) 자동 튜닝
- [ ] 동적 confidence_threshold 조정

---

## 📋 Phase N: 인프라 & DevOps (지속)

| 작업 | 설명 | 우선순위 |
|------|------|----------|
| N.1 MLflow | 모델 레지스트리, 버전 관리, A/B 테스트 | 중간 |
| N.2 Grafana | 대시보드: Sharpe, drawdown, win rate, latency | 중간 |
| N.3 PostgreSQL HA | Primary-Replica 복제 | 낮음 |
| N.4 mypy strict | 80% → 100% 타입 커버리지 | 낮음 |
| N.5 DB Indexes | Partial index 최적화 (VWAP, composite) | 낮음 |
| N.6 Dead Code | 미사용 레거시 코드 제거, Swagger 업데이트 | 낮음 |

---

## 🔧 기술 부채

| 영역 | 현재 | 목표 | 비고 |
|------|------|------|------|
| 테스트 커버리지 | ~58% (44개 테스트) | 70%+ (~65개 테스트) | Phase K.2 |
| mypy | 80% | 100% strict | Phase N.4 |
| Dead Code | predictor.py의 `predict_next()` | L.2 이후 제거 | 레거시, 하위 호환성 보존 |
| DB Indexes | 기본 | Partial indexes | Phase N.5 |
| Swagger 문서 | 부분 완료 | 분류 API 전체 | Phase N.6 |
| strategies.py | Rule-based personas | L.2에서 통합 | Momentum/MeanReversion/Breakout |

---

## 📐 시스템 아키텍처 (현재)

```
┌─────────────────────────────────────────────────────┐
│                    FastAPI (main.py)                 │
│  ┌─────────┐  ┌──────────┐  ┌────────────────────┐ │
│  │ RAG API │  │ Trade API │  │ Portfolio API       │ │
│  └────┬────┘  └────┬─────┘  └────────┬───────────┘ │
│       │            │                  │             │
│  ┌────▼────────────▼──────────────────▼───────────┐ │
│  │         trading_strategy_sync.py                │ │
│  │  predict_class() → (class, confidence, probs)   │ │
│  └──────────┬──────────────────┬──────────────────┘ │
│       ┌─────▼─────┐    ┌──────▼──────┐             │
│       │ Predictor  │    │ RiskManager │             │
│       │ (4 regime  │    │ (Kelly/MPT  │             │
│       │ classifiers│    │  VaR/ATR)   │             │
│       └─────┬─────┘    └─────────────┘             │
│       ┌─────▼─────┐                                │
│       │ ML Models  │ ensemble_classifier_{regime}.pkl│
│       │ CatBoost   │ VotingClassifier(soft)         │
│       │ LightGBM   │ 27 features, 3 classes         │
│       │ XGBoost    │ θ=0.003                        │
│       └───────────┘                                │
├─────────────────────────────────────────────────────┤
│  Celery Workers: daily_ohlcv, train_models,         │
│  market_scan, trailing_stops, sentiment, rebalance  │
├─────────────────────────────────────────────────────┤
│  PostgreSQL/TimescaleDB │ Redis │ Alpaca API        │
└─────────────────────────────────────────────────────┘
```
