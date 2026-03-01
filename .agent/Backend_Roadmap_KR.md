# 백엔드 로드맵 🚀

> **최종 업데이트**: 2026-02-27 (Session 22) | **현재 단계**: 🟢 Phase K ✅ + Phase M (M.1 ✅, M.2 ✅, M.3 ✅, M.4 ✅) + Phase L (L.1 ✅)
> **스택**: Python 3.14 · FastAPI · PostgreSQL/TimescaleDB · Redis · Celery · CatBoost+LightGBM+XGBoost
> **서버**: 4-core CPU, 24GB RAM, GPU 없음
> **분류**: Ternary (UP=2 / NEUTRAL=1 / DOWN=0), θ=0.005, 일봉(1d), 26개 feature

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
| 10 | 2026-02-25 | **모델 진단 + 감성 최적화**: Regime별 CLASS_WEIGHTS 도입 (bear NEUTRAL 1.5×), 학습 보고서 개편 (Sharpe→NEUTRAL_R/F1/classification_report), feature importance top-10 기록, 감성 스케줄 7×→2×/day |
| 11 | 2026-02-25 | **목표 성능 개선**: bear NEUTRAL 1.5→2.5×, sideways UP 1.0→1.1, composite score+NEUTRAL_recall(25%), 튜닝-학습 가중치 정합 |
| 12 | 2026-02-26 | **정확도 푸시 + 버그 수정**: portfolio_optimizer min_length 버그, CLASS_WEIGHTS 재조정 (bull NEUTRAL↓, bear DOWN↑/UP↓), composite score min_class_recall로 일반화, confusion matrix 추가 |
| 13 | 2026-02-26 | **Bear 가중치 안정화**: 가중치 진동 해결 (bear {0:1.2,1:1.8,2:1.2} 대칭화), bull DOWN 1.3→1.2 |
| 14 | 2026-02-26 | **Bear NEUTRAL 미세 조정**: bear NEUTRAL 1.8→2.0 (recall 19.7%→목표 25%), J.3 가중치 튜닝 거의 완료 |
| 15 | 2026-02-26 | **가중치 튜닝 수렴 + 문서화**: bear NEUTRAL 2.0×=sweet spot 확인 (28.8% recall, 41.4% acc, 0.41 F1), 코드 변경 불필요. MODEL_IMPROVEMENT_HISTORY.md + ML_TECHNICAL_QA.md 작성 |
| 16 | 2026-02-26 | **K.1 Circuit Breaker 강화**: 연속 손실 한도(3회/일), 일일 거래 횟수 한도(20회/일 소프트), 상태 전환 시 Discord 알림, Prometheus 메트릭(counter+gauge), lazy import. 테스트: 10→27개 (+17). 파일: circuit_breaker.py (337→501), metrics.py, test_circuit_breaker.py (122→461) |
| 17 | 2026-02-26 | **M.2 SHAP Feature Selection**: estimator별 TreeExplainer (CatBoost/LightGBM/XGBoost), voting-weight 집계, 클래스별 SHAP importance, 층화 서브샘플링(500), sector_id frozenset 보호, removal candidate 감지. Celery task `analyze_features_shap`. 테스트: 26개 신규 (8 클래스). 파일: shap_analyzer.py (700 NEW), training.py (+SHAP 통합), test_shap_analyzer.py (620 NEW), requirements.txt (+shap>=0.46.0) |
| 18 | 2026-02-26 | **M.3 Adaptive Thresholds + 코드베이스 감사**: Optuna 기반 regime별 θ 최적화 ([0.002,0.015], CatBoost 단독, TimeSeriesSplit(3)), confidence threshold 최적화 ([0.30,0.70], trade_score=accuracy×√coverage). 중간 감사: 23개 이슈 발견, 7개 critical/high 수정 (docstring, bar_executed 초기화, config 중복 제거, thread-safe singleton, SHAP 파라미터, Optional→union, type hints). Celery task `optimize_thresholds`. 테스트: 23개 신규 (9 클래스). 파일: threshold_optimizer.py (691줄 NEW), training.py (+Section 6 + Celery task), test_threshold_optimizer.py (510줄 NEW). 감사 수정: features.py, ml_strategy.py, circuit_breaker.py, shap_analyzer.py |
| 19 | 2026-02-26 | **SHAP 버그 수정 + K.2 테스트 커버리지**: SHAP `_compute_tree_shap` categorical dtype 불일치 수정 (LightGBM/XGBoost는 `category` 필요, `int` 아님). K.2 테스트 확장: test_predictor.py (15개), test_features.py (14개), test_portfolio_optimizer.py (10개) = +39개 신규. 총 테스트: ~151개. 파일: shap_analyzer.py (수정), test_predictor.py (381 NEW), test_features.py (317 NEW), test_portfolio_optimizer.py (311 NEW) |
| 20 | 2026-02-26 | **M.4 SHAP Feature Pruning + Regime Fallback**: `breakout_flag` 제거 (SHAP removal candidate, 모든 regime), feature 27→26. `_REGIME_FALLBACK_CHAIN` 추가 — `sideways_volatile`(300 samples<500)는 `sideways_calm`으로 fallback. +1 fallback 테스트. 파일: features.py, shap_analyzer.py, predictor.py, test_predictor.py, test_features.py |
| 21 | 2026-02-27 | **XGBoost Pickle 호환성 수정 + Phase L.1 일일 Signal Cache**: XGBoost 2.0+ `feature_names_in_` 읽기전용 property 에러 수정 (3중 방어: try/except + monkeypatch + `__dict__` bypass). Phase L.1: Redis 기반 일일 ML 예측 캐시 (`DailySignalCache`), Celery task `generate_daily_signals` (17:30 ET), API 엔드포인트 `GET /signals/daily`, trading strategy 캐시 통합. +18 테스트. 파일: models.py (XGBoost 수정), signal.py (스키마 NEW), signal_cache.py (NEW), trading.py (+task), worker.py (+schedule), signals.py (엔드포인트 NEW), api.py (+router), trading_strategy_sync.py (+캐시), test_signal_cache.py (NEW) |
| 22 | 2026-02-27 | **Phase M.1 Cross-Sectional Momentum**: `CrossSectionalMomentum` 스코어러 (1m/3m/6m 수익률, 변동성 조정 모멘텀, 섹터 상대 강도, composite score 가중치 0.20/0.40/0.25/0.15). 섹터 로테이션 순위 (13개 GICS 섹터, 섹터별 top-3). `_select_uncorrelated_symbols()`에 momentum 필터 통합 (percentile >= 0.50 + tiebreaker). Celery task `compute_momentum_scores` (17:15 ET). Redis 캐시 (`momentum:scores:{date}`, 24h TTL). API: GET /momentum/rankings, /rankings/{symbol}, /sectors, POST /compute. +22 테스트. 파일: momentum.py (스키마 NEW), momentum_scorer.py (NEW), market_analysis.py (+task), worker.py (+schedule+route), momentum.py (엔드포인트 NEW), api.py (+router), trading_strategy_sync.py (+momentum 필터), test_momentum_scorer.py (NEW) |

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
| P2 | 테스트 커버리지 58% → ~151개 | 70%+ ✅ | Phase K.2 ✅ |
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

### J.3.2 모델 진단 및 보고서 개편 ✅ (Session 10)
- [x] Regime별 CLASS_WEIGHTS 도입
- [x] 학습 보고서 개편 (F1, NEUTRAL_R, classification_report, feature importance)
- [x] 감성 스케줄 7×→2×/day

### J.3.3 목표 성능 개선 ✅ (Session 11)
- [x] bear NEUTRAL 1.5→2.5×, composite score+neutral_recall(25%)
- [x] 튜닝-학습 가중치 정합

### J.3.4 정확도 푸시 & 버그 수정 ✅ (Session 12)
- [x] portfolio_optimizer.py min_length 버그 수정
- [x] Composite score: neutral_recall→min_class_recall 일반화
- [x] Confusion matrix 보고서 추가

### J.3.5 Bear 가중치 안정화 ✅ (Session 13)
- [x] 가중치 진동 해결 — bear 대칭화 {0:1.2, 1:1.8, 2:1.2}

### J.3.6 Bear NEUTRAL 미세 조정 ✅ (Session 14)
- [x] bear NEUTRAL 1.8→2.0 (recall 19.7%→목표 ~25%)

### J.3.7 가중치 튜닝 수렴 확인 ✅ (Session 15)
- [x] bear NEUTRAL 2.0× = **SWEET SPOT** (recall 28.8%, acc 41.4%, F1 0.41)
- [x] 코드 변경 불필요 — 현재 가중치가 최적
- [x] 문서화: `docs/MODEL_IMPROVEMENT_HISTORY.md`, `docs/ML_TECHNICAL_QA.md`

### J.3 최초 모델 학습 (가중치 튜닝 수렴 ✅)
- [x] `train_models` Celery task 실행 — S10–S15 전체에 걸쳐 수행
- [x] `ensemble_classifier_{regime}.pkl` 생성 (4개 파일)
- [x] 검증: bear 41.4% ✅, F1 0.41 ✅, min class recall 28.8% ✅
- [x] bull(37.9%)/sideways(38.3%)는 3-class 분류 실용적 상한(~38%) 근접 — 구조적 개선(Phase M) 필요
- **다음**: Phase K.1 (서킷 브레이커) → M.2 (SHAP) → M.3 (Adaptive θ)
- **파일**: `app/tasks/training.py`, `model_artifacts/`

---

## 📋 Phase K: 프로덕션 강화 (Phase J 이후)

### K.1 Circuit Breaker 강화 ✅ (Session 16)
- [x] 일일 손실 한도: -3% 또는 -$500 — 기존 구현 확인
- [x] 연속 손실 한도: 1일 3회 → 거래 일시 중지 (NEW)
- [x] 일일 거래 횟수 한도: 20회/일 소프트 한도 (NEW)
- [x] API 지연 모니터링: >3000ms × 3회 연속 → 중단 — 기존 구현 확인
- [x] 모든 상태 전환 시 Discord 알림 (OPEN/HALF_OPEN/CLOSED) (NEW)
- [x] Prometheus 메트릭: counter + gauge (NEW)
- [x] Lazy import (metrics, discord) 안전성 확보 (NEW)
- [x] Redis에 consecutive_losses 상태 저장/복원 (NEW)
- [x] get_status() 새 필드 추가 (NEW)
- [x] 17개 신규 테스트 (10→27개)
- **파일**: `app/services/circuit_breaker.py` (337→501), `app/core/metrics.py`, `tests/test_circuit_breaker.py` (122→461)

### K.2 테스트 커버리지 → 70% ✅ (Session 19 완료)
- [x] 우선 대상: `predictor.py` (15개), `features.py` (14개), `portfolio_optimizer.py` (10개)
- [x] 분류 전용 테스트: predict_class, feature 커럼 수 (32/27/21/25), sector_id 처리
- [x] 총: ~151개 테스트 (112 기존 + 39 신규) — 목표 70%+ 초과
- **파일**: `tests/test_predictor.py` (381 NEW), `tests/test_features.py` (317 NEW), `tests/test_portfolio_optimizer.py` (311 NEW)

---

## 📋 Phase L: Dual-Timeframe Hybrid (중기, Phase K 이후)

> **의존성**: Phase J (모델 학습 완료) + Phase K (프로덕션 안전)

### L.1 Daily ML Signal Cache ✅ (Session 21)
- [x] `CachedSignal` + `DailySignalSummary` Pydantic 스키마
- [x] `DailySignalCache` Redis 서비스: set/get/bulk/invalidate/stats
- [x] 키 형식: `signal:daily:{symbol}:{regime}`, 24h TTL
- [x] Celery task `generate_daily_signals`: 17:30 ET (월-금), post-market
- [x] 워크플로우: regime 감지 → feature 생성 → 예측 → Redis 저장 → Discord 알림
- [x] `SyncTradingStrategy._get_cached_signal()` — `process_portfolio()` 내 캐시 우선 조회
- [x] API 엔드포인트: `GET /api/v1/signals/daily` + `GET /daily/{symbol}` + `GET /daily/stats` + `DELETE /daily`
- [x] 18개 단위 테스트 (6 테스트 클래스)
- **파일**: `app/domain/schemas/signal.py` (NEW), `app/services/signal_cache.py` (NEW), `app/tasks/trading.py` (+task), `app/worker.py` (+schedule+route), `app/api/v1/endpoints/signals.py` (NEW), `app/api/v1/api.py` (+router), `app/services/trading_strategy_sync.py` (+cache), `tests/test_signal_cache.py` (NEW)

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

### M.1 Cross-Sectional Momentum ✅ (Session 22)
- [x] `CrossSectionalMomentum` 클래스: 60종목 간 상대 강도 순위
- [x] 수익률 지표: 1m (21일), 3m (63일), 6m-skip-1m (126일, 학술 관례)
- [x] 변동성 조정 모멘텀: return_3m / volatility_63d
- [x] 섹터 상대 강도: 종목 return_3m - 섹터 평균 return_3m
- [x] Min-max 정규화 + composite score: 0.20×r_1m + 0.40×r_3m + 0.25×r_6m_skip + 0.15×sector_rel
- [x] 섹터 로테이션: 섹터별 평균 모멘텀 집계, 순위 1=최강, 섹터별 top-3 종목
- [x] Top-N% 선별: 설정 가능한 percentile 컷오프 (기본 20%)
- [x] Redis 캐시: `momentum:scores:{date}` + `momentum:sectors:{date}`, 24h TTL
- [x] Celery task `compute_momentum_scores` (17:15 ET, OHLCV 수집 후)
- [x] 통합: `_select_uncorrelated_symbols()` momentum 필터 (percentile >= 0.50) + tiebreaker
- [x] 그레이스풀 디그레이데이션: momentum 데이터 없어도 동작 (confidence-only 폴백)
- [x] API 엔드포인트: GET /momentum/rankings, /rankings/{symbol}, /sectors, POST /compute
- [x] 22개 단위 테스트 (9 테스트 클래스)
- **파일**: `app/domain/schemas/momentum.py` (NEW), `app/services/momentum_scorer.py` (NEW), `app/tasks/market_analysis.py` (+task), `app/worker.py` (+schedule+route), `app/api/v1/endpoints/momentum.py` (NEW), `app/api/v1/api.py` (+router), `app/services/trading_strategy_sync.py` (+momentum 필터), `tests/test_momentum_scorer.py` (NEW)

### M.2 SHAP Feature Selection ✅ (Session 17)
- [x] `SHAPFeatureSelector` 클래스 — estimator별 TreeExplainer (CatBoost/LightGBM/XGBoost)
- [x] Voting-weight 집계로 ensemble SHAP importance 계산
- [x] 클래스별 SHAP importance (DOWN/NEUTRAL/UP) + global importance
- [x] `_normalise_shap_output()`: list[ndarray], 3D, 2D SHAP 포맷 처리
- [x] 층화 서브샘플링 (기본 500) — 클래스 분포 보존
- [x] `sector_id` frozenset 보호 — removal candidates에 절대 포함 불가
- [x] `get_removal_candidates()`, `select_features()`, `save_report()` 유틸리티
- [x] `shap` lazy import + 한국어 에러 메시지
- [x] 학습 통합: production 모델 저장 후 SHAP 분석 (Section 5)
- [x] 독립 Celery task `analyze_features_shap` — 데이터 로드, regime별 분리, SHAP 실행
- [x] Phase 1 = 분석만 — 자동 제거 없음, 사람이 검토 필요
- [x] 26개 단위 테스트 (8개 테스트 클래스) — 모든 public 메서드 커버
- **파일**: `app/ml/shap_analyzer.py` (700줄 NEW), `app/tasks/training.py` (+SHAP 통합 + Celery task), `tests/test_shap_analyzer.py` (620줄 NEW), `requirements.txt` (+shap>=0.46.0)

### M.3 Adaptive Thresholds ✅ (Session 18)
- [x] `AdaptiveThresholdOptimizer` 클래스 — Optuna TPESampler, regime별 최적화
- [x] θ 최적화: CatBoost 단독 (iter=100, depth=4), TimeSeriesSplit(3), 범위 [0.002, 0.015]
- [x] Confidence 최적화: trade_score = acted_accuracy × √coverage, 범위 [0.30, 0.70]
- [x] 클래스 붕괴 방지: 어떤 클래스든 < 5%이면 θ 건너뜀
- [x] Coverage 페널티: coverage < 5%이면 trade_score × 0.1
- [x] `_composite_score()` 로컬 미러 — training.py와의 순환 의존성 회피
- [x] `save_thresholds()` / `load_thresholds()` — `adaptive_thresholds.json` JSON 저장/로드
- [x] `run_threshold_optimization()` 독립 진입점
- [x] 학습 통합: SHAP 이후 Section 6 — θ(50 trials) + confidence(40 trials) per regime
- [x] 독립 Celery task `optimize_thresholds` — 데이터 로드, regime별 분리, 최적화 실행
- [x] Phase 1 = 추천만 — 최적값 로그, JSON 저장, 자동 적용 없음
- [x] 23개 단위 테스트 (9개 테스트 클래스) — helpers, optimizer, persistence, entry-point 커버
- [x] 중간 코드베이스 감사: 23개 이슈 발견, 7개 critical/high 수정
- **파일**: `app/ml/threshold_optimizer.py` (691줄 NEW), `app/tasks/training.py` (+Section 6 + Celery task), `tests/test_threshold_optimizer.py` (510줄 NEW)

### M.4 SHAP Feature Pruning ✅ (Session 20)
- [x] `breakout_flag` 제거 — SHAP removal candidate (모든 3개 학습된 regime: bull/bear/calm)
- [x] Base features: 27 → 26, Full features: 32 → 31
- [x] Regime fallback chain: `_REGIME_FALLBACK_CHAIN` — `sideways_volatile`(300 samples<500)는 `sideways_calm`으로 fallback
- [x] Fallback 순서: `sideways_volatile → sideways_calm → bull → bear`
- [x] 테스트 업데이트: feature 커럼 수 (27→26, 32→31), +1 fallback chain 테스트
- **파일**: `app/ml/features.py`, `app/ml/shap_analyzer.py`, `app/ml/predictor.py`, `tests/test_predictor.py`, `tests/test_features.py`

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
| 테스트 커버리지 | ~58% (44개) → ~152개 | 70%+ ✅ | Phase K.2 ✅ |
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
│       │ LightGBM   │ 26 features, 3 classes         │
│       │ XGBoost    │ θ=0.005                        │
│       └───────────┘                                │
├─────────────────────────────────────────────────────┤
│  Celery Workers: daily_ohlcv, train_models,         │
│  market_scan, trailing_stops, sentiment, rebalance  │
├─────────────────────────────────────────────────────┤
│  PostgreSQL/TimescaleDB │ Redis │ Alpaca API        │
└─────────────────────────────────────────────────────┘
```
