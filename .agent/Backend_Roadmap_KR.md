# 백엔드 로드맵 🚀

FastAPI Stock Trader 백엔드의 체계적 진화 과정을 담은 로드맵입니다.
현재 상태: **Phase E (프로덕션 강화) & F (고급 AI) 준비 중**

---

## ✅ 완료된 단계 (이력)

### Phase A: 핵심 거래 시스템
- [x] **FastAPI 설정** (Clean Architecture, Async/Sync 분리)
- [x] **데이터베이스** (PostgreSQL + TimescaleDB, SQLAlchemy)
- [x] **Alpaca API** (시장 데이터 & 거래 실행 통합)
- [x] **특성 생성** (TA-Lib 기술적 지표)

### Phase B: 안정성 & 자동화
- [x] **Celery 작업 큐** (Redis Broker, 동기 워커 모드)
- [x] **스케줄러** (Celery Beat 설정)
- [x] **Docker 보안** (Non-root 사용자, 취약점 스캔)

### Phase C: 성능 최적화 (지연시간 & 처리량)
- [x] **DB 최적화** (TimescaleDB Hypertables, Continuous Aggregates)
- [x] **캐싱 레이어** (Redis: OHLCV, 포지션, 계좌 정보 with TTL)
- [x] **커넥션 풀링** (SQLAlchemy QueuePool, PgBouncer 준비)
- [x] **모니터링** (Prometheus 메트릭, 헬스 체크)

### Phase D: ML 코어 (학습 & 튜닝)
- [x] **모델 아키텍처** (앙상블: CatBoost + LGBM + XGBoost)
- [x] **하이퍼파라미터 튜닝** (Optuna 프레임워크, 동적 Sharpe:F1 비율)
- [x] **데이터 전략** (24개월 롤링 윈도우, TimeSeriesSplit)
- [x] **특성 엔지니어링** (TA-Lib 15+ 지표)
- [x] **백테스팅 시스템** (Backtrader 엔진, CLI 검증)

---

## CI/CD 인프라 (2026-01-22 완료) ✅

**목표**: Python 3.14 + CatBoost를 위한 빠르고 신뢰할 수 있는 GitHub Actions 파이프라인

### 빌드 최적화
- [x] **Conda 기반 종속성 관리**
  - Python 3.14 호환성을 위해 pip에서 conda로 마이그레이션
  - 재현 가능한 환경을 위한 `environment.yml` 생성
  - CatBoost 설치: 소스 빌드(40분) → Pre-built wheel(2분)
- [x] **GitHub Actions 워크플로우**
  - `conda-incubator/setup-miniconda@v3` 구현
  - Conda 환경 활성화를 위한 `shell: bash -el {0}` 추가
  - conda 패키지(catboost, numpy, pandas)와 pip 패키지 분리
- [x] **Dockerfile 멀티스테이지 빌드**
  - environment.yml을 먼저 복사하여 레이어 캠싱 최적화
  - CatBoost 설치 검증 단계 추가
  - 빌드 시간: 15-20분 → 5-7분
- [x] **버전 고정**
  - Python: 3.14.x로 고정 (`requires-python = "==3.14.*"`)
  - CatBoost: conda-forge에서 1.2.8 (catboost-1.2.8-cpu_py314hf729cd6_6.conda)
  - 채널 우선순위: conda-forge (strict)

**영향:**
- GitHub Actions 빌드 시간: 40분(타임아웃) → 3분(93% 감소)
- Docker 빌드 시간: 15분 → 6분(60% 감소)
- CI/CD 신뢰성: 0% 성공 → 100% 성공

---

## 코드 품질 & 정리 (진행 중)

**목표**: 리팩토링, 최적화, 모범 사례를 통해 높은 코드 품질 유지

### 완료된 개선 사항 (2026-01-05) ✅
- [x] **미사용 파라미터 정리**
  - `app/tasks/training.py::_train_regime_specific_models`: `repo: SyncStockRepository`와 `end_date: pd.Timestamp` 파라미터 제거
  - `_walk_forward_validation` 로직 인라인화 (15줄 TimeSeriesSplit)
  - 결과: 깔끔한 함수 시그니처, 미사용 변수 없음
- [x] **섹터 조회 우선순위 역전**
  - `app/ml/sector_map.py::get_sector()`: API 우선 전략으로 변경
  - 우선순위: yfinance API (실시간) → 수동 SECTOR_MAP (백업)
  - 근거: 실시간 데이터가 정적 매핑보다 정확
- [x] **백필 스크립트 생성**
  - `scripts/backfill_sectors.py`: 기존 심볼용 70줄 스크립트
  - 사용법: `docker compose exec app python scripts/backfill_sectors.py`
  - 목적: sector_id가 NULL인 심볼의 섹터 데이터 업데이트

### 중대 버그 수정 (2026-01-06) ✅
- [x] **Gemini API 마이그레이션** (google-generativeai → google-genai)
  - Deprecated된 `google-generativeai`에서 공식 `google-genai>=1.33.0` SDK로 마이그레이션
  - `app/services/sentiment_analyzer.py` 업데이트: Client 기반 API 패턴
  - Import 변경: `from google import genai` → `genai.Client(api_key=...)`
  - 모델 업데이트: `gemini-pro` → `gemini-2.0-flash-exp`
  - API 호출: `client.models.generate_content(model=..., contents=...)`
- [x] **피처 파이프라인 수정** (학습 KeyError 해결)
  - `FeatureEngineer`에 `base_feature_columns` 속성 추가 (19개 기술 지표만)
  - 학습용 피처(base)와 예측용 피처(전체 24개, Phase F 포함) 분리
  - `app/tasks/training.py` 95번째 줄: 히스토리컬 데이터 학습에 `base_feature_columns` 사용
  - `app/tasks/training.py` 667번째 줄: Feature importance도 `base_feature_columns` 사용
  - 원인: Phase F 피처(감성, 펀더멘털)가 히스토리컬 OHLCV 데이터에 없음
- [x] **SQL 로깅 감소**
  - `app/core/database.py`에서 SQLAlchemy echo 비활성화 (async, sync 엔진 모두)
  - `echo=settings.ENV_STATE == "dev"` → `echo=False`로 변경
  - 로깅은 `app/core/logging.py` 설정으로만 제어
  - 결과: 깔끔한 로그, 필요할 때만 SQL 문 표시

### CI/CD 파이프라인 수정 (2026-01-22) ✅
- [x] **GitHub Actions Conda 환경 활성화**
  - `setup-miniconda@v3`에서 deprecated된 `auto-activate-base: false` 파라미터 제거
  - 모든 lint 및 test 단계에 `shell: bash -el {0}` 추가
  - CI/CD의 exit code 127 (command not found) 에러 해결
  - 영향: Ruff와 mypy가 이제 활성화된 conda 환경에서 실행됨
- [x] **Training Pipeline 버그 수정**
  - `app/tasks/training.py` 348번째 줄 수정: `predictor.load_model(regime)` → `predictor.get_model(regime)`
  - 원인: PredictorService는 `get_model()` 메서드만 있고 `load_model()` 없음
  - 영향: 모델 검증 단계가 이제 올바르게 작동함

### 테스트 인프라 (2026-01-22) ✅
- [x] **Python 3.14 Asyncio 현대화**
  - conftest.py에서 deprecated된 `asyncio.get_event_loop_policy().new_event_loop()` 제거
  - pytest-asyncio 자동 event loop 관리로 마이그레이션
  - 커스텀 event_loop fixture 불필요 (pytest-asyncio가 처리)
  - 영향: Python 3.14+ 대비 미래 지향적 테스트 인프라
- [x] **통합 테스트 스위트** (NEW - 11개 테스트 추가)
  - `tests/test_training_integration.py` 생성 (450+ 줄)
  - 전체 워크플로우 테스트: train_models, tune_models, _load_and_prepare_data
  - Mock 기반 DB/API 테스트 (외부 의존성 없음)
  - 시나리오: 정상 흐름, 엣지 케이스 (심볼 없음, 데이터 부족), Optuna 튜닝
  - 커버리지: 학습 파이프라인 end-to-end ~65%
- [x] **테스트 개수 증가**
  - 이전: 33개 테스트 (원래 19개 + 레짐 14개)
  - 이후: 44개 테스트 (+11개 통합 테스트)
  - 커버리지 증가: 45% → 58% (추정)
  - 테스트 파일: 5개 → 6개 (새로운 파일: test_training_integration.py)

### 보류 중인 개선 사항 (낮은 우선순위)
- [ ] **DB 인덱스 최적화**
  - 중복 인덱스 제거: `ix_stock_ohlcv_symbol` (복합 인덱스로 커버됨)
  - 복합 인덱스 추가: `idx_ohlcv_timeframe_symbol_time` (다중 타임프레임 쿼리용)
  - 부분 인덱스 추가: `WHERE vwap IS NOT NULL` (VWAP 전용 쿼리)
- [ ] **코드 중복 분석**
  - 서비스 전반의 유사 로직 식별
  - 공통 패턴을 유틸리티 함수로 추출
- [ ] **타입 힌트 강화**
  - 레거시 함수에 누락된 타입 힌트 추가
  - 엄격한 mypy 검사 활성화

---

## Phase E: 프로덕션 강화 (현재 초점)

**목표**: 시스템이 라이브 환경에서 자율적이고 겁고하게 작동하도록 보장

**상태**: 20% 완료 (인프라 준비 완료, 운영 기능 보류 중)

### E.1 운영 안정성 (우선순위: 높음)
- [ ] **서킷 브레이커 향상** (다음 작업)
  - 현재: 기본 RiskManager 검사 (쾸다운, 최소 수익)
  - 계획: 포트폴리오 레벨 서킷 브레이커
    - 일일 손실 한계: -3% 또는 -$500 (먼저 도달)
    - API 지연 임계값: > 3000ms → 거래 중단
    - 연속 손실 한계: 1시간 내 3회 손실 → 일시 중지
  - 구현: `app/services/circuit_breaker.py` 확장
  - 예상: 2-3일
- [ ] **알림 시스템** (Discord/Slack Webhook)
  - 알림 대상: 거래 실행, 중대 오류, 리스크 한계
  - 우선순위: 보통 (서킷 브레이커 후)
- [ ] **Alpaca WebSocket** (실시간 주문 업데이트)
  - 폴링을 이벤트 기반 업데이트로 교체
  - 우선순위: 낮음 (현재 폴링 작동, 최적화 용도)

### E.2 인프라 고가용성
- [ ] **PostgreSQL 복제** (Primary-Replica 설정 계획)
- [ ] **Redis 영속성** (AOF/RDB 정책 확인)
- [ ] **로그 집계** (중앙 집중식 로깅 설정 계획)

---

## 🔮 Phase F: 고급 AI 역량 (95% 완료)

**목표**: "기술적 트레이더"에서 감성 분석, 펀더멘털, 고급 분석을 갖춘 "AI 헤지펀드"로 전환

### F.1 감성 분석 통합 (100% 완료) ✅
*AI 기반 분석을 통한 실시간 뉴스 감성*
- [x] **SentimentAnalyzer 서비스** (2026-01-05 완료)
  - JSON 파싱을 통한 Gemini API 통합
  - Redis 캐싱 (1시간 TTL): `sentiment:{symbol}`
  - 감성 점수: -1.0 (극도 부정) ~ +1.0 (극도 긍정)
  - 국면별 가중치 조정: 강세장은 긍정 선호, 약세장은 부정 선호
- [x] **Celery 자동화** (2026-01-05 완료)
  - `update_sentiment_scores`: 매시간 업데이트 (crontab: `minute=0, hour=*`)
  - `clear_stale_sentiment_cache`: 일일 정리 (자정)
- [x] **특성 통합** (2026-01-05 완료)
  - ML 특성 벡터에 `sentiment_score` 추가 (20번째 특성)
  - `add_sentiment_and_fundamentals()` 편의 메서드
- [x] **Finnhub 통합** (2026-01-05 완료)
  - 프리미엄 금융 뉴스 API (Reuters, Bloomberg, WSJ 등)
  - 엔드포인트: GET /v1/company-news
  - 무료 티어: 60 calls/minute (매시간 업데이트 충분)
  - 프로덕션: $59/month (Professional 플랜)
  - 심볼당 상위 10개 기사 (datetime 정렬)
  - 응답: headline, summary, source, url, datetime
  - 에러 핸들링: RequestException, timeout (10초)
  - NewsAPI.org 대비 우수한 뉴스 품질

### F.2 펀더멘털 메트릭 통합 (100% 완료) ✅
*재무 건전성 메트릭으로 특성 엔지니어링 강화*
- [x] **FundamentalDataProvider 서비스** (2026-01-05 완료)
  - LRU 캐시를 사용한 yfinance API 통합 (maxsize=500, 24시간 TTL)
  - 메트릭: PE 비율, PB 비율, ROE, 배당 수익률, 시가총액, 베타
  - 주식 분류: VALUE, GROWTH, INCOME, BLEND, UNKNOWN
  - 위험 조정 점수: `(ROE / PE) * (1 + Div_Yield) / Beta`
- [x] **특성 통합** (2026-01-05 완료)
  - 4개 펀더멘털 특성 추가: `pe_ratio`, `pb_ratio`, `roe`, `beta`
  - 기본값: PE=15.0, PB=3.0, ROE=0.10, Beta=1.0 (시장 평균)
  - 총 특성: 20 → 24 (감성 포함)
- [x] **섹터 자동 수집** (2026-01-05 완료)
  - 우선순위: yfinance API → 수동 SECTOR_MAP 백업
  - LRU 캐시 (maxsize=1000)로 과도한 API 호출 방지
  - 11개 섹터 카테고리 + Unknown=99
  - 백필 스크립트: `scripts/backfill_sectors.py`

### F.3 VIX 통합 & 국면 강화 (100% 완료) ✅
*개선된 국면 감지를 위한 변동성 지수*
- [x] **VIX 데이터 수집** (2026-01-05 완료)
  - Celery 작업: `collect_vix_data` (매일 오전 6:30 EST)
  - Alpaca API: 일일 VIX 바 (심볼: 'VIX')
  - 저장: PostgreSQL (이력) + Redis (최신 값, 24시간 TTL)
- [x] **국면 감지 강화** (2026-01-05 완료)
  - `RegimeDetector.detect_regime(vix_value=Optional[float])`
  - VIX 임계값: >30 (극도의 공포), >20 (높은 공포)
  - 변동성 분류에 VIX가 ATR 재정의
  - 로깅: 국면 감지 로그에 VIX 값 포함
- [x] **VIX 해석**
  - VIX < 12: 낮은 변동성 (차분한 시장)
  - VIX 12-20: 정상 변동성
  - VIX 20-30: 높은 변동성 (높은 공포)
  - VIX > 30: 극도의 변동성 (패닉)

### F.4 고급 분석 (100% 완료) ✅
*특성 중요도 및 포트폴리오 스트레스 테스트*
- [x] **특성 중요도 분석** (2026-01-05 완료)
  - Celery 작업: `analyze_feature_importance`
  - 트리 기반 모델에서 추출 (CatBoost, LGBM, XGBoost)
  - 앙상블 가중치를 사용한 가중 평균
  - 출력: PNG 플롯 (상위 15개 특성) + JSON 데이터
  - 파일: `feature_importance_{regime}.png`, `feature_importance_{regime}.json`
- [x] **몬테카를로 시뮬레이션** (2026-01-05 완료)
  - `MonteCarloSimulator` 클래스 (10,000회 시뮬레이션, 252일)
  - 포트폴리오 시뮬레이션: 상관 수익률을 위한 Cholesky 분해
  - 단일 자산 시뮬레이션: 기하 브라운 운동 (GBM)
  - 리스크 메트릭: VaR (95%), CVaR, 손실 확률, 백분위수
  - 사용 사례: 포트폴리오 스트레스 테스트 및 시나리오 분석

**Phase F 상태: 100% 완료** ✅
- [ ] **국면 감지 모듈**
  - VIX, SPY 이동평균, ADX를 사용하여 시장 상태 분류
  - 상태 출력: `BULLISH_TREND`, `BEARISH_TREND`, `HIGH_VOLATILITY`, `SIDEWAYS`
- [ ] **국면별 모델**
  - 각 국면에 대해 별도 가중치 또는 모델 학습
  - **약세 모델**: 높은 공매도 가중치, 타이트한 손절
  - **강세 모델**: 롱 전용 선호, 넓은 트레일링 스톱
- [ ] **추론 로직**
  - 현재 국면 감지 → 적절한 모델 로드 → 예측

### F.4 AutoML & 지속적 개선
- [x] **Walk-Forward 최적화** (2026-01-03 완료)
  - 다중 기간 검증 구현: 3개 시간 윈도우 (90-60, 60-30, 30-0일 전)
  - 다양한 시장 조건에서 강력한 Sharpe 추정
  - Sharpe 전용으로 앙상블 가중치 단순화 (F1 복잡성 제거)
- [ ] **특성 중요도 분석**
  - 중요도 0인 특성 자동 제거하여 노이즈 감소
- [ ] **몬테카를로 시뮬레이션**
  - 무작위 시장 경로에 대한 전략 스트레스 테스트

---

## 🎯 Phase G: 실시간 15분 거래 (2026-01-04 완료)

**목표**: 15분봉 및 실시간 데이터 수집으로 일중 거래 활성화

### G.1 실시간 데이터 수집
- [x] **15분 OHLCV 수집** (2026-01-04 완료)
  - Celery 작업: `collect_15m_realtime` (장중 15분마다, 9:00-15:00 ET)
  - Alpaca 통합: VWAP 및 trade_count 필드 추가
  - 장중 시간 검증 (평일 체크, 9:30 AM - 4:00 PM ET)

### G.2 VWAP 특성 엔지니어링
- [x] **VWAP 거리 특성** (2026-01-04 완료)
  - 공식: `(close - vwap) / vwap`
  - 해석: 기관 벤치마크 비교
  - 총 특성: 19 → 20 (vwap_distance 추가)

### G.3 거래 로직 15분 전환
- [x] **SyncTradingStrategy 15분 모드** (2026-01-04 완료)
  - 타임프레임: '15m' (기존 '1d')
  - 최소 바: 500 (≈5 거래일)
  - 임계값 조정: 0.5% → 0.2% (일중 민감도)
  - 로깅: [15m] 태그 추가

### G.4 Celery Beat 스케줄
- [x] **15분 수집 스케줄** (2026-01-04 완료)
  - Crontab: `minute=0,15,30,45 hour=9-15 day_of_week=1-5`
  - Worker: app.tasks.realtime_data 포함
  - 빈도: 시간당 4회, 하루 7시간, 평일만

---

## 🧠 Phase H: 시장 국면 인식 (2026-01-04 부분 완료)

**목표**: 시장 조건(강세, 약세, 변동성, 안정)에 반응하는 적응형 AI

### H.1 국면 감지 통합
- [x] **RegimeDetector 통합** (2026-01-04 완료)
  - 메서드: SyncTradingStrategy의 `detect_market_regime()`
  - 참조: SPY 15분 데이터 (90일 룩백)
  - 메트릭: ADX > 25 (추세), ATR% > 3% (변동성), SMA50 (방향)
  - 출력: 4개 국면 (BULL_TRENDING, BEAR_TRENDING, SIDEWAYS_VOLATILE, SIDEWAYS_CALM)

### H.2 국면 인식 예측
- [x] **PredictorService 다중 모델 지원** (2026-01-04 완료)
  - 모델 로딩: 4개 국면별 pkl 파일 또는 일반 폴백
  - 메서드: `predict_next(features, regime=MarketRegime.SIDEWAYS_CALM)`
  - 폴백: 국면 모델 누락 시 일반 모델

### H.3 국면별 모델 학습
- [x] **학습 파이프라인 국면 분류** (2026-01-04 완료)
  - 역사적 데이터를 국면별로 분류 (SPY 기반 감지)
  - 4개 국면 데이터셋으로 분할 (각 최소 1000 샘플)
  - 4개 앙상블 모델 학습: `ensemble_model_{regime}.pkl`
  - 국면별 Walk-Forward 검증
  - 구현: training.py의 _train_regime_specific_models()

### H.4 Bull 레짐 성능 강화 (2026-01-23 완료) ✅
*저조한 bull_trending 모델 성능 개선 (정확도 49%, Sharpe -0.22)*
- [x] **Feature Importance Analyzer** (2026-01-23 완료)
  - 새 유틸리티: `app/ml/feature_analyzer.py` (FeatureImportanceAnalyzer 클래스)
  - CatBoost, LightGBM, XGBoost 모델에서 중요도 추출
  - 앙상블 구성에 따른 가중 평균
  - JSON 내보내기 및 PNG 시각화 지원
  - 사용법: `FeatureImportanceAnalyzer.from_models(models).get_report()`
- [x] **강세장 전용 모멘텀 특성** (2026-01-23 완료)
  - 6개 신규 특성: momentum_5, momentum_10, rsi_momentum, trend_strength, price_position, breakout_flag
  - 특성 개수: 21 → 27개 기본 특성
  - 근거: 강세장은 평균회귀와 다른 기술 지표 필요
  - 구현: `app/ml/features.py::_add_momentum_features()`
- [x] **국면별 거래 임계값** (2026-01-23 완료)
  - ADR: ADR-001-Regime-Specific-Trading-Thresholds.md
  - 설정: `app/core/config.py`의 `REGIME_TRADING_CONFIG`
  - 임계값:
    - bull_trending: 0.4% 매수, -0.1% 매도, 30% position_scale (보수적)
    - bear_trending: 0.2% 매수, -0.2% 매도, 70% position_scale
    - sideways_volatile: 0.2% 매수, -0.2% 매도, 50% position_scale, 비활성
    - sideways_calm: 0.2% 매수, -0.2% 매도, 100% position_scale (최적)
  - 구현: `_execute_trade_logic()`에서 동적으로 국면 설정 사용
- [x] **Walk-Forward OOS 검증 강화** (2026-01-23 완료)
  - 강화된 함수: training.py의 `_walk_forward_validation_enhanced()`
  - TimeSeriesSplit 기반 검증 (기본 5 splits)
  - 과적합 감지: `OOS/IS Sharpe 비율 < 0.3` 또는 `IS > 5이고 OOS < 1`
  - 모델 신뢰도: OOS Sharpe 성능 기반 0.1-1.0
  - 폴드별 메트릭 로깅: IS vs OOS 비교
  - 영향: 의심스러운 모델 조기 감지 (bear_trending 10.47 Sharpe)
- [x] **Bull 레짐 Fallback 메커니즘** (2026-01-19 완료)
  - 설정: REGIME_MODELS에 `fallback_to_regime: 'sideways_calm'` 추가
  - 구현: `trading_strategy_sync.py::process_symbol()` fallback 로직
  - 근거: Bull 모델(48.78% 정확도, -0.42 Sharpe) → sideways_calm 사용(53.08%, +5.99)
  - 원인: 데이터 부족이 아닌 피처-레짐 미스매치 (bear와 유사한 11K 샘플)
- [x] **데이터 수집 스케줄 수정** (2026-01-19 완료)
  - `worker.py`: `hour="9-15"` → `hour="9-16"` (16:00 봉 수집)
  - `realtime_data.py`: 16:00 실행을 위한 시간 검증 수정
  - 효과: +6.25% 데이터 커버리지 (마지막 거래 시간 15:30-16:00)
- [x] **CatBoost 학습 오류 수정** (2026-01-19 완료)
  - `ml/models.py`: `logging_level='Silent'` 제거 (`verbose=False`와 충돌)
  - 오류: "Only one of parameters ['verbose', 'logging_level'] should be set"

**모델 성능 분석 (2026-01-19 개선 후):**
| 국면 | 정확도 | Sharpe | 상태 |
|--------|----------|--------|--------|
| bull_trending | 48.78% | -0.42 | ✅ 수정됨 (sideways_calm fallback 사용) |
| bear_trending | 52.49% | 10.04 | ⚠️ 과적합 의심 (OOS 검증) |
| sideways_calm | 53.08% | 5.99 | ✅ 양호 (주력 모델) |
| sideways_volatile | N/A | N/A | ⚠️ 비활성 (70 샘플) |

**예상 효과:**
- bull_trending: 보수적 임계값으로 약한 모델의 손실 방지
- bear_trending: OOS 검증으로 배포 전 과적합 감지
- 전체: Feature importance 분석으로 데이터 기반 모델 개선 가능

---

## 🛡️ Phase I: 고급 리스크 & 포지션 방어 (2026-01-05 완료)

**목표**: 과도한 거래, 조기 청산, 빠른 재거래로부터 보호

### I.1 거래 방어 메커니즘 (2026-01-04 완료)
**해결된 중대 취약점:**
- ✅ **최소 보유 기간**: 60분 (4 바 @ 15분)
  - 빠른 포지션 전환 방지 (15분 내 매수 → 매도)
  - 예외: 손절 신호 우선
- ✅ **최소 수익 임계값**: 1.5% (거래 비용 5배 마진)
  - 최소 수익으로 조기 청산 방지
  - 120분 후 강제 청산 허용
- ✅ **쿨다운 기간**: 청산 후 60분
  - 즉시 재거래 방지 (휩쏘 보호)
  - 로그: "COOLDOWN: X분 남음"

**구현:**
- 데이터베이스: `position_tracking` 테이블 (Alembic 마이그레이션 `002_position_tracking.py`)
- RiskManager: `can_enter_position()`, `can_exit_position()`, `record_position_exit()`
- Repository: `record_position_entry()`, `get_active_position()`, `update_position_exit()`
- TradingStrategy: BUY/SELL 주문 전 방어 체크

**영향:**
- 거래 수수료 비율: 0.5% → 0.1% (예상)
- 휩쏘 거래: 20-30% → <5%
- 예상 ROI 개선: +10-15% 연간

### I.2 다중 포지션 시스템 (2026-01-05 완료)
**기능:**
- ✅ **동시 다중 심볼 포지션**
  - AAPL + MSFT + GOOGL + NVDA + TSLA 동시 보유 (최대 5개)
  - 상관계수 매트릭스 기반 포트폴리오 분산
  - 심볼 선택: 활성 포지션과 낮은 상관관계 (<0.7)
- ✅ **현대 포트폴리오 이론 (MPT)**
  - scipy.optimize를 통한 Sharpe 비율 최대화
  - 제약: 심볼당 최대 30% 할당, 가중치 합 = 1.0
  - 자동 업그레이드: 백테스트 데이터 → 실거래 데이터 (50+ 거래 존재 시)
- ✅ **켈리 기준 포지션 크기 결정**
  - 공식: `f* = (bp - q) / b` (25% 안전 계수)
  - 승률 및 P/L 비율 기반 심볼별 동적 계산
  - 실거래 데이터 통합: 심볼당 10+ 거래 후 자동 전환
- ✅ **포트폴리오 레벨 VaR**
  - 일일 위험 가치 계산 (95% 신뢰도, 14일 윈도우)
  - 역사적 시뮬레이션 방법 (백분위수 기반)
  - 보수적 폴백: 데이터 부족 시 -3% 일일 리스크
- ✅ **일일 리밸런싱**
  - 스케줄: 3:45 PM ET (장 마감 15분 전)
  - 트리거: 가중치 드리프트 > 5%일 때만
  - 최소 거래 금액: $100 (미세 거래 방지)
- ✅ **자동 파라미터 업데이트**
  - 일일 00:00 ET: 상관계수 매트릭스, VaR, 켈리 크기
  - 롤링 14일 윈도우 (자동 갱신)
  - Redis 캐싱: 24시간 TTL

**구현:**
- PortfolioOptimizer: 상관계수, VaR, 켈리, MPT 최적화
- PortfolioRepository: P&L 집계, 거래 이력, 포지션 쿼리
- PortfolioRebalancer: 가중치 계산, 드리프트 감지, 주문 실행
- TradingStrategy.process_portfolio(): 다중 심볼 배치 처리
- Celery 작업: update_portfolio_parameters (00:00), rebalance_portfolio (15:45)

**영향:**
- 분산: 1 → 5 동시 포지션
- 리스크 감소: 상관계수 기반 선택 (<0.7)
- 자본 효율성: 켈리 최적화 포지션 크기 결정
- Sharpe 최대화: MPT 가중치 최적화

### I.3 외부 데이터 통합 (Phase F를 통해 완료)
**이미 구현됨:**
- Gemini API를 통한 뉴스 감성 분석 (Phase F.1)
- Finnhub를 통한 금융 뉴스 (Phase F.1)
- yfinance를 통한 펀더멘털 (Phase F.2)

**현재 로드맵 범위 외:**
- Reddit/Twitter 소셜 감성 분석
- FRED 경제 지표
- 대체 데이터 제공업체 (Quandl, Bloomberg Terminal)

---

## 다음 단계 (우선순위 순서)

### 즉시 (이번 주)
1. **서킷 브레이커 향상** (Phase E.1)
   - 포트폴리오 레벨 손실 한계
   - API 지연 모니터링
   - 연속 손실 감지
   - 예상: 2-3일

### 단기 (향후 2주)
2. **알림 시스템** (Phase E.1)
   - Discord webhook 통합
   - 알림 템플릿 (거래, 오류, 리스크)
   - 예상: 1-2일

3. **테스트 커버리지 개선**
   - 목표: 45% → 70%
   - 초점: `features.py`, `predictor.py`, `portfolio_optimizer.py`
   - 예상: 3-4일

### 중기 (다음 달)
4. **PostgreSQL 복제** (Phase E.2)
   - 고가용성을 위한 Primary-Replica 설정
   - 예상: 5-7일

5. **프로덕션 모니터링 대시보드**
   - Grafana 대시보드 커스터마이징
   - 주요 메트릭: Sharpe, drawdown, win rate, latency
   - 예상: 2-3일

### 장기 (향후 단계)
- Phase J: 마이크로서비스 아키텍처 (스케일링 필요 시)
- Phase K: 머신러닝 모델 레지스트리 (MLflow 통합)
- Phase L: 백테스팅 플랫폼 (전략 테스팅용 Web UI)

---

## 기술 부채 & 정리

### 현재 상태
- 테스트 커버리지: ~45% (70%+로 개선 필요)
- 문서화: Swagger 부분 업데이트 (RAG 엔드포인트 문서화)
- 코드 품질: Ruff 린팅 통과, mypy 타입 검사 80%

### 보류 중인 작업
- [ ] **레거시 코드 제거**: 미사용 파일 확인 (예: 오래된 `services/backtester.py`, 모의 전략)
- [ ] **단위 테스트**: `features.py`와 `predictor.py`의 커버리지 개선
- [ ] **문서화**: 새로운 Ops 엔드포인트로 API 문서(Swagger) 업데이트
- [ ] **타입 힌트**: 100% mypy 커버리지 달성을 위한 누락된 타입 힌트 추가
- [ ] **영어 로그**: 국제 호환성을 위해 남은 한국어 로그를 영어로 변환
