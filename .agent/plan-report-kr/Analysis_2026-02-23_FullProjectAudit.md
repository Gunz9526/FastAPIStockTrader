# 프로젝트 전체 분석 및 개선점 보고서

**날짜**: 2026-02-23  
**범위**: 전체 코드베이스 분석 — 아키텍처, ML 파이프라인, 트레이딩 로직, 인프라  
**상태**: 분석 완료

---

## 1. 시스템 개요

### 1.1 기술 스택
| 레이어 | 기술 | 버전 |
|--------|------|------|
| 언어 | Python | 3.14 |
| 웹 프레임워크 | FastAPI | ≥0.128.0 |
| 데이터베이스 | PostgreSQL + TimescaleDB | - |
| ORM | SQLAlchemy (async + sync 분리) | ≥2.0.46 |
| 마이그레이션 | Alembic | ≥1.18.1 |
| 태스크 큐 | Celery + Redis | ≥5.6.2 |
| ML 모델 | CatBoost, LightGBM, XGBoost (Ensemble) | - |
| 하이퍼파라미터 튜닝 | Optuna | ≥4.7.0 |
| 피처 엔지니어링 | TA-Lib (15+ 기술 지표) | ≥0.6.8 |
| 백테스팅 | Backtrader | 1.9.78.123 |
| 브로커 API | Alpaca (Paper/Live) | alpaca-py ≥0.43.2 |
| 감성 분석 AI | Google Gemini (genai) | ≥1.60.0 |
| 뉴스 API | Finnhub | ≥2.4.26 |
| 펀더멘털 | yfinance | ≥1.0 |
| 모니터링 | Prometheus + Grafana | - |
| 알림 | Discord Webhook | httpx 기반 |
| 컨테이너화 | Docker + Docker Compose | Multi-stage build |

### 1.2 아키텍처 패턴
- **레이어드 아키텍처** (엄격한 Clean Architecture는 아님)
  - `api/` → FastAPI 엔드포인트
  - `domain/models/` → SQLAlchemy ORM 모델
  - `domain/schemas/` → Pydantic 스키마
  - `repositories/` → 데이터 접근 레이어
  - `services/` → 비즈니스 로직
  - `ml/` → ML 파이프라인 (features, models, predictor)
  - `tasks/` → Celery 비동기 태스크
  - `core/` → 설정, DB, 캐시, 보안

### 1.3 트레이딩 시스템 설계
- **15분봉 인트라데이** 트레이딩 전략
- **4가지 시장 레짐**: Bull Trending, Bear Trending, Sideways Volatile, Sideways Calm
- **앙상블 ML 모델**: 레짐별 CatBoost + LightGBM + XGBoost
- **멀티 포지션 포트폴리오** (최대 5개 동시 포지션)
- **리스크 관리**: Circuit Breaker, 쿨다운, 최소 보유 시간, 스톱로스

---

## 2. 치명적 이슈 (반드시 수정 필요)

### 2.1 [치명] 모델 성능 — 근본적으로 결함 있음
**현재 성능:**
| 레짐 | 정확도 | Sharpe | 판정 |
|------|--------|--------|------|
| bull_trending | 48.78% | -0.42 | ❌ 랜덤보다 나쁨 (fallback 사용 중) |
| bear_trending | 52.49% | 10.04 | ❌ 심각한 과적합 |
| sideways_calm | 53.08% | 5.99 | ⚠️ 의심스러운 Sharpe |
| sideways_volatile | N/A | N/A | ❌ 비활성화 (70개 샘플) |

**근본 원인:**
1. **타겟 변수 설계 결함**: 타겟이 `다음 바의 수익률` (close pct_change shift -1)입니다. 15분봉의 수익률 예측은 **극도로 노이즈가 높은 신호**이며, 본질적으로 signal-to-noise ratio가 매우 낮습니다. 모델이 사실상 랜덤워크 노이즈를 예측하려고 시도하는 것입니다.
2. **방향 정확도 ≈ 동전 던지기**: 48-53%의 방향 예측 정확도는 모델이 거의 zero edge를 제공한다는 뜻입니다.
3. **Sharpe Ratio 이상치**: bear_trending의 Sharpe 10.04는 **실전에서 불가능한 수치** — 전형적인 인샘플 데이터 과적합입니다.
4. **학습/추론 피처 불일치**: 학습 시 `base_feature_columns` (27 피처)를 사용하지만, 실시간 추론 시 `legacy` (sentiment/PE 포함 25 피처)를 사용합니다. 모델이 학습한 피처와 예측 시 전달받는 피처가 완전히 다릅니다.

### 2.2 [치명] 학습과 추론 간 피처 불일치
**파일**: `app/services/trading_strategy_sync.py` 약 210행

```python
# 학습 시: base_feature_columns (모멘텀 피처 포함 27개)
all_X.append(features_df[feature_engineer.base_feature_columns])

# 추론 시: "legacy" (감성/PE/PB/ROE 포함 25개, 모멘텀 없음)
scaled_features = self.feature_engineer.extract_feature_vector(
    current_features, fit_scaler=False, feature_set="legacy"
)
```

모델은 모멘텀 피처(`momentum_5`, `momentum_10`, `rsi_momentum`, `trend_strength`, `price_position`, `breakout_flag`)로 학습하지만, 추론 시에는 `sentiment_score`, `pe_ratio`, `pb_ratio`, `roe`를 대신 받습니다. **모델은 학습 시 본 적 없는 완전히 다른 피처로 예측하고 있습니다.**

### 2.3 [치명] 포지션 사이징이 사실상 1주 고정
**파일**: `app/services/trading_strategy_sync.py` 약 432행

```python
base_qty = 1
qty = max(1, int(base_qty * position_scale))
```

`position_scale` 0.5일 때 `max(1, int(0.5)) = 1주`. 1.0일 때도 `max(1, 1) = 1주`. 가용 자본, Kelly Criterion, 포트폴리오 최적화와 **무관하게 항상 정확히 1주만 매수**합니다. 정교하게 구축된 포트폴리오 최적화 프레임워크가 완전히 무효화됩니다.

### 2.4 [치명] 감성분석 & 펀더멘털의 영향력이 사실상 0
**파일**: `app/services/trading_strategy_sync.py`

```python
sentiment_adjustment = sentiment_score * 0.005  # 최대 ±0.005
fundamentals_adjustment = -0.003 또는 +0.002    # 고정된 아주 작은 값

adjusted = (
    ml_prediction * 0.75 +           # 지배적
    sentiment_adjustment * 0.15 +     # 최대: ±0.00075
    fundamentals_adjustment * 0.10    # 최대: ±0.0003
)
```

감성과 펀더멘털 조정값이 **이중으로 곱해집니다** (스케일 계수 × 가중치). 감성의 최대 기여: `0.005 × 0.15 = 0.00075`. ML 예측값의 일반적 범위 0.001~0.01 대비 이 조정값은 노이즈 수준입니다.

### 2.5 [치명] Walk-Forward 검증에서 미래 데이터 사용 (Look-Ahead Bias)
**파일**: `app/tasks/training.py` 약 720행

레짐별 학습 시 피처 스케일링을 **전체 레짐 데이터셋에 먼저 적용(fit)**한 뒤 TimeSeriesSplit을 수행합니다:

```python
X_regime_scaled = feature_engineer.extract_feature_vector(
    X_regime, fit_scaler=True, market_avg_volume=market_avg_volume
)
# 이후 이미 스케일링된 데이터에 TimeSeriesSplit 적용
```

스케일러가 이미 미래 데이터 분포를 본 상태이므로, 인샘플/아웃오브샘플 성능 지표가 모두 부풀려집니다. 실전 성능과 백테스트 성능의 괴리 원인입니다.

---

## 3. 높은 우선순위 이슈

### 3.1 레짐 분류 연산이 치명적으로 비효율적
**파일**: `app/tasks/training.py` 약 180행

```python
for idx, timestamp in enumerate(X.index):
    spy_window = spy_features[spy_features.index <= timestamp].tail(200)
    regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
```

학습 데이터의 **모든 샘플**(10만개 이상 가능)에 대해 개별적으로 pandas 필터링 + 정렬 + 피처 추출을 수행합니다. 또한 사용되는 VIX 값이 **현재 캐시된 단일 값**으로, 해당 타임스탬프의 과거 VIX가 아닙니다 — 또 다른 look-ahead bias입니다.

### 3.2 PredictorService 싱글턴 안티패턴
**파일**: `app/ml/predictor.py`

- 클래스 레벨 `_models = {}`를 공유 — 쓰레드 안전성 없음
- `reload_models()` 비원자적 — 리로드 중 예측 시 불일치 모델 사용
- `retrain_weighted()` 내 `self._model_path` 속성 미존재 (호출 시 크래시)
- 워커 재시작 없이 모델 업데이트 불가

### 3.3 RiskManager 상태가 메모리 전용 — 재시작 시 소실
**파일**: `app/services/risk_manager.py`

```python
self.position_entry_times: dict[str, datetime] = {}  # In-memory cache
self.symbol_cooldowns: dict[str, datetime] = {}      # Redis-backed (future) ← 미구현
```

모든 쿨다운 및 포지션 추적 상태가 인메모리입니다. Celery 워커가 재시작되면 모든 쿨다운이 소실되어, 쿨다운 기간 내의 포지션에도 즉시 재진입이 가능해집니다.

### 3.4 `REGIME_STRATEGY_WEIGHTS`는 데드 코드
**파일**: `app/services/regime.py`

`REGIME_STRATEGY_WEIGHTS` 딕셔너리는 각 레짐별 전략 가중치(Momentum, MeanReversion, Breakout, MLEnsemble)를 정의하지만, **어디서도 사용되지 않습니다**. 실제 트레이딩은 ML 앙상블 예측만 사용합니다. `strategies.py`의 전통적 전략들(MomentumStrategy, MeanReversionStrategy, BreakoutStrategy)도 트레이딩 파이프라인에서 전혀 사용되지 않습니다.

### 3.5 Trailing Stop 시스템 미작동
**파일**: `app/tasks/trading.py` 약 79행

```python
logger.warning("트레일링 스톱 업데이트는 일시 비활성화되어 있습니다 - 동기 리팩토링 필요")
```

15분마다 스케줄링되어 있음에도 아무 작업도 하지 않습니다. `Position` 테이블의 stop_loss_price, take_profit_price, trailing_stop_price 필드는 결코 채워지지 않습니다.

### 3.6 백테스트 엔진이 현재 모델을 사용하지 않음
**파일**: `app/backtest/engine.py`

백테스트 엔진은 `MLStrategy`를 사용하지만, 최근의 레짐 감지, 피처 불일치, 앙상블 예측 등이 반영되지 않습니다. 백테스트 결과가 실제 라이브 트레이딩 동작을 반영하지 않습니다.

---

## 4. 중간 우선순위 이슈

### 4.1 HTTP 미들웨어 이중 로깅
**파일**: `app/main.py`

두 개의 `@app.middleware("http")` 핸들러가 모든 요청에 대해 중복 로깅을 수행합니다:
- `metrics_middleware`: `[{method}] {endpoint} - IP: {client_ip}`
- `log_requests`: `Incoming: {method} {url}` + `Response: {status_code}`

### 4.2 포지션 업데이트 시 트랜잭션 격리 부재
**파일**: `app/services/trading_strategy_sync.py`

`_place_order` 메서드가 분산 락은 획득하지만, Alpaca API 호출 + DB 업데이트를 하나의 트랜잭션으로 감싸지 않습니다. API 호출은 성공했지만 DB 커밋이 실패하면, 시스템이 포지션을 추적하지 못합니다.

### 4.3 Kelly Criterion의 백테스트 폴백이 Mock 데이터
**파일**: `app/services/portfolio_optimizer.py` 약 290행

라이브 거래 데이터가 부족하면 (< 10 건), 시스템이 가짜 Mock 데이터에 기반하여 포지션 사이징을 결정합니다.

### 4.4 상관행렬 계산 시 날짜 정렬 미수행
**파일**: `app/services/portfolio_optimizer.py` 약 86행

가장 짧은 시리즈 길이로 단순 truncation — 실제 날짜 기반 정렬이 아님. 완전히 다른 날짜 범위의 데이터가 혼합될 수 있습니다.

### 4.5 Optuna 튜닝이 Sharpe만 최적화
**파일**: `app/tasks/training.py`

모델 복잡도에 대한 정규화 페널티 없음, 드로다운 제약 없음, 최소 정확도 검증 없음. bear_trending이 Sharpe=10을 달성하면서도 과적합인 이유입니다.

---

## 5. 낮은 우선순위 이슈

### 5.1 미사용 코드 및 데드 코드
- `strategies.py`: `BreakoutStrategy` 구현되었으나 미사용
- `regime.py`: `REGIME_STRATEGY_WEIGHTS`, `REGIME_RISK_PARAMS` 미참조
- `worker.py`: Debug용 `print()` 문 잔존

### 5.2 하드코딩된 경로
- `/app/model_artifacts/` — Docker 내부 경로가 하드코딩되어 로컬 개발 불가
- `settings` 또는 환경 변수 사용 필요

### 5.3 Celery 태스크 에러 복구 부재
- `train_models`에 체크포인트 없음 — 2시간 학습 후 실패 시 전체 작업 소실
- Alpaca API 호출에 exponential backoff 없음

### 5.4 테스트 커버리지 부족
- 44개 테스트, ~45% 커버리지
- 미테스트 대상: `portfolio_optimizer.py`, `circuit_breaker.py`, `trading_strategy_sync.py`

---

## 6. 투자 전략 이슈

### 6.1 근본적 전략 문제
현재 시스템은 **정확한 15분 수익률을 예측**하려고 합니다 — 양적 금융에서 가장 어려운 문제 중 하나입니다. 수십억 달러의 R&D 예산을 가진 헤지펀드조차 이 타임프레임에서 작은 edge만을 확보합니다.

주요 문제점:
1. **Signal-to-Noise Ratio**: 15분 수익률은 마이크로스트럭처 노이즈에 지배됩니다
2. **레짐 감지 해상도**: SPY ADX/ATR로 15분 레짐 감지는 너무 거칩니다
3. **리스크-리워드 비대칭 없음**: 모든 포지션이 1주로 대칭적 임계값 사용
4. **Mean Reversion vs Momentum 전환 없음**: 정의된 전략이 있으나 항상 ML regression만 사용

### 6.2 권장: 전략 개편
1. **정확한 수익률 예측 → 분류(Classification) 전환**: UP/DOWN/NEUTRAL을 confidence와 함께 예측
2. **레짐-전략 매핑 활용**: 이미 정의된(미사용 중인) REGIME_STRATEGY_WEIGHTS 활성화
3. **비대칭 포지션 사이징**: 높은 신뢰도 신호에 더 큰 포지션
4. **최소 기대수익 필터**: 예측 움직임 < 거래비용 + 슬리피지이면 미거래
5. **일봉 기반 ML 예측**: 15분봉은 실행 타이밍에만 사용

### 6.3 모델 아키텍처 권장사항
1. **회귀(Regression) → 분류(Classification)**: Binary (UP ≥ 0.2% / DOWN) 또는 neutral zone 포함 ternary
2. **피처 선택(Feature Selection)**: SHAP 값으로 노이즈 피처 제거 (27개는 신호 품질 대비 과다)
3. **Purged Cross-Validation**: embargo 기간으로 시계열 데이터 누수 방지
4. **앙상블 다양성 확인**: CatBoost/LightGBM/XGBoost가 동일한 예측을 하지 않는지 확인
5. **일봉 먼저 학습**: 더 깨끗한 신호에서 baseline 확보 후 15분봉 시도

---

## 7. 권장 실행 계획 요약

### 즉시 (1주차)
| 우선순위 | 이슈 | 영향 |
|----------|------|------|
| P0 | 학습/추론 피처 불일치 수정 | 현재 예측이 무의미함 |
| P0 | 포지션 사이징 수정 (Kelly/포트폴리오 가치 사용) | 현재 항상 1주 |
| P0 | 학습 시 스케일러 look-ahead bias 수정 | 부풀려진 백테스트 지표 |
| P1 | RiskManager 쿨다운 Redis 영속화 | 안전 메커니즘 미작동 |

### 단기 (2-3주차)
| 우선순위 | 이슈 | 영향 |
|----------|------|------|
| P1 | 타겟 변수 재설계 (Classification) | 핵심 모델 개선 |
| P1 | Purged CV 구현 | 신뢰할 수 있는 성능 지표 |
| P1 | 감성/펀더멘털 이중 스케일링 수정 | Phase F 피처 무의미 |
| P1 | 레짐 분류 최적화 (벡터화) | 학습 시간 수시간 → 수분 |

### 중기 (2개월차)
| 우선순위 | 이슈 | 영향 |
|----------|------|------|
| P2 | Trailing Stop 정상 구현 | 리스크 관리 공백 |
| P2 | 전략 프레임워크 연결 (레짐→전략) | 다양한 알파 소스 |
| P2 | 현재 모델 기반 백테스팅 구현 | 평가 불가 |
| P2 | Thread-safe PredictorService | 프로덕션 안정성 |

### 장기 (3개월+)
| 우선순위 | 이슈 | 영향 |
|----------|------|------|
| P3 | 일봉 타임프레임 ML 검토 | 더 깨끗한 신호 |
| P3 | SHAP 기반 Feature Selection | 노이즈 피처 제거 |
| P3 | MLflow 모델 레지스트리 | 모델 버전 관리 |
| P3 | 테스트 스위트 강화 (70%+ 커버리지) | 회귀 방지 |

---

*Lead Technical PM 작성 — 2026-02-23*
