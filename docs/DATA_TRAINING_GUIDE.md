# 데이터 수집 및 모델 학습 상세 가이드

**날짜**: 2025-12-29

---

## 📊 DB에 수집되는 데이터

### 1. OHLCV 데이터 (stock_ohlcv)

#### 수집 시점
```python
# app/services/data_provider.py 의 get_ohlcv_bars() 메서드
- 호출 위치: TradingStrategyEngine.analyze_and_execute()
- 자동 호출: Celery Beat 시장 스캔 시 (매시간)
```

#### 수집 기간
```python
# 기본값: 100일 (약 3개월)
look_back_days = 100

# 코드 위치: app/services/trading_strategy.py
end_date = datetime.now()
start_date = end_date - timedelta(days=look_back_days)

# Alpaca API 호출
bars = await self.data_provider.get_ohlcv_bars(
    symbol, 
    start_date, 
    end_date
)
```

#### 저장되는 데이터
```sql
-- TimescaleDB 하이퍼테이블
CREATE TABLE stock_ohlcv (
    id INTEGER,
    symbol VARCHAR(20),
    date_time TIMESTAMP WITH TIME ZONE,  -- 파티셔닝 컬럼
    open FLOAT,
    high FLOAT,
    low FLOAT,
    close FLOAT,
    volume FLOAT,
    adj_close FLOAT,
    PRIMARY KEY (id, date_time)
);

-- 7일 청크로 자동 파티셔닝
-- 예: 1년 = 52개 청크
```

#### 실제 저장량
```
종목당 저장: 100일 ≈ 100 rows (일봉 기준)
10개 종목 × 100일 = 1,000 rows
실시간 업데이트로 계속 증가

예상 크기:
- 100 rows/종목 × 10 종목 = 1,000 rows ≈ 100KB
- 1년 후: 365 rows/종목 × 10 종목 = 3,650 rows ≈ 300KB
```

---

### 2. 재무 데이터 (stock_fundamentals)

#### 수집 시점
```python
# Celery Beat 스케줄
'daily-fundamentals': {
    'task': 'app.tasks.data_tasks.collect_fundamentals',
    'schedule': crontab(hour=18, minute=0),  # 매일 18:00
}

# 또는 수동 API 호출
POST /api/v1/operations/collect-data
```

#### 수집 기간
```python
# 매일 최신 데이터 1건만 수집 (당일 기준)
date = date.today()

# YFinance에서 실시간 정보 가져옴
yf_ticker = yf.Ticker(symbol)
info = yf_ticker.info
```

#### 저장되는 데이터
```sql
CREATE TABLE stock_fundamentals (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(20),
    date DATE,
    per FLOAT,          -- Price-to-Earnings
    pbr FLOAT,          -- Price-to-Book
    roe FLOAT,          -- Return on Equity
    market_cap FLOAT,   -- 시가총액
    sector VARCHAR(100) -- 섹터
);

-- 매일 1 row/종목 추가
-- 1년 = 365 rows/종목
```

#### 실제 저장량
```
종목당 저장: 1 row/day
10개 종목 × 365일 = 3,650 rows/year ≈ 50KB/year
```

---

### 3. 포지션 & 거래 로그

#### positions (현재 포지션)
```sql
-- 활성 포지션만 저장 (평균 5-10개)
SELECT COUNT(*) FROM positions WHERE status = 'OPEN';
-- 예상: 5-10 rows
```

#### trade_logs (거래 내역)
```sql
-- 모든 매매 기록 저장
-- 예상: 하루 10-20 거래
-- 1년 = 10 거래/일 × 250 거래일 = 2,500 rows
```

---

## 🤖 모델 학습 데이터

### 1. 학습 데이터 구성

#### 소스 데이터
```python
# app/ml/predictor.py (또는 regime-aware 버전)
# 학습 시 OHLCV 데이터 사용

1. OHLCV 데이터: stock_ohlcv 테이블에서 조회
2. 기간: 최소 100일, 권장 1년 (365일)
3. 종목: 활성화된 모든 종목
```

#### 특징 생성
```python
# app/ml/features.py
from app.ml.features import FeatureEngineer

engineer = FeatureEngineer()
features = engineer.create_features(ohlcv_df)

# 생성되는 17개 특징:
- SMA (20, 50일)
- EMA (12, 26일)
- RSI (14일)
- MACD, MACD Signal
- Bollinger Bands (상/중/하)
- ATR
- ADX
- CCI
- MOM
- ROC
- Price change %
```

#### 타겟 생성 (예측 대상)
```python
# 다음 날 수익률
features['target'] = features['close'].pct_change().shift(-1)

# 또는 방향 (상승/하락)
features['target'] = (features['close'].shift(-1) > features['close']).astype(int)
```

---

### 2. 학습 프로세스

#### A. 단순 모델 (현재 구현)
```python
# app/ml/predictor.py
class PredictorService:
    def retrain(self, X, y):
        model = EnsembleWrapper()  # CatBoost + LGBM + XGBoost
        model.train(X, y)
        
        # 저장
        joblib.dump(model, "model_artifacts/ensemble_model.pkl")
```

#### B. Regime 모델 (Phase D, 미구현)
```python
# app/ml/predictor.py의 RegimeAwarePredictor
# 4개 독립 모델
for regime in [BULL, BEAR, SIDEWAYS_V, SIDEWAYS_C]:
    # 해당 regime 데이터만 필터링
    mask = (regimes == regime)
    X_regime = X[mask]
    y_regime = y[mask]
    
    # 독립 학습
    model = CatBoostWrapper()
    model.train(X_regime, y_regime)
    model.save(f"model_artifacts/model_{regime}.cbm")
```

---

### 3. 학습 데이터 양

#### 최소 요구사항
```python
# 단일 모델
최소: 100일 × 1종목 = 100 샘플
권장: 365일 × 10종목 = 3,650 샘플

# Regime 모델 (국면별)
각 regime당 최소 100 샘플 필요
총 4개 regime × 100 샘플 = 400 샘플 최소
권장: 1,000+ 샘플 (약 3-4개월 데이터)
```

#### 현재 상태
```sql
-- DB에서 확인
SELECT 
  symbol,
  COUNT(*) as data_count,
  MIN(date_time) as first_date,
  MAX(date_time) as last_date
FROM stock_ohlcv
GROUP BY symbol;

-- 예상 출력 (초기 상태)
symbol | data_count | first_date          | last_date
-------|-----------|--------------------|-----------------
AAPL   | 100       | 2024-09-20 09:30   | 2024-12-29 16:00
MSFT   | 100       | 2024-09-20 09:30   | 2024-12-29 16:00
```

---

## 📅 데이터 축적 타임라인

### 초기 (현재)
```
✅ OHLCV: 100일 (자동 수집)
✅ 재무: 1일 (수동 수집)
❌ 모델 학습: 불가 (데이터 부족)
```

### 1주일 후
```
✅ OHLCV: 100일 (지속 업데이트)
✅ 재무: 7일
⚠️ 모델 학습: 가능하지만 성능 낮음
```

### 1개월 후
```
✅ OHLCV: 100-130일
✅ 재무: 30일
✅ 모델 학습: 실용 가능
```

### 3개월 후
```
✅ OHLCV: 100-190일 
✅ 재무: 90일
✅ Regime 모델: 학습 가능 (국면별 50+ 샘플)
```

### 1년 후
```
✅ OHLCV: 365일 (충분)
✅ 재무: 365일
✅ Regime 모델: 최적 성능
```

---

## 🔧 데이터 수집 방법

### 1. 자동 수집 (Celery Beat)
```python
# OHLCV: 매시간 (시장 개장 시간)
'market-scan': {
    'task': 'app.tasks.trading.execute_market_scan',
    'schedule': crontab(
        hour='9-16',  # 9:00-16:00
        minute=30,    # 30분마다
        day_of_week='1-5'  # 월-금
    )
}

# 재무: 매일
'daily-fundamentals': {
    'task': 'app.tasks.data_tasks.collect_fundamentals',
    'schedule': crontab(hour=18, minute=0)  # 18:00
}
```

### 2. 수동 수집 (API)
```bash
# 재무 데이터
curl -X POST http://SERVER:8000/api/v1/operations/collect-data

# OHLCV (시장 스캔으로 자동 수집)
curl -X POST http://SERVER:8000/api/v1/operations/execute-scan
```

### 3. 초기 대량 수집 (권장)
```python
# scripts/backfill_ohlcv.py (생성 필요)
# 과거 1년 데이터를 한번에 수집

from datetime import datetime, timedelta
from app.services.data_provider import AlpacaDataProvider

provider = AlpacaDataProvider()
symbols = ['AAPL', 'MSFT', 'GOOGL', ...]

for symbol in symbols:
    start = datetime.now() - timedelta(days=365)
    end = datetime.now()
    
    bars = provider.get_ohlcv_bars(symbol, start, end)
    # DB에 저장
```

---

## 📊 데이터 확인

### DB 쿼리
```sql
-- OHLCV 데이터 현황
SELECT 
  symbol,
  COUNT(*) as bars,
  MIN(date_time) as from_date,
  MAX(date_time) as to_date,
  ROUND(EXTRACT(EPOCH FROM (MAX(date_time) - MIN(date_time))) / 86400) as days
FROM stock_ohlcv
GROUP BY symbol
ORDER BY symbol;

-- 재무 데이터 현황  
SELECT 
  symbol,
  COUNT(*) as entries,
  MIN(date) as from_date,
  MAX(date) as to_date
FROM stock_fundamentals
GROUP BY symbol;

-- 총 데이터 크기
SELECT 
  pg_size_pretty(pg_total_relation_size('stock_ohlcv')) as ohlcv_size,
  pg_size_pretty(pg_total_relation_size('stock_fundamentals')) as fundamentals_size;
```

---

## ⚠️ 중요 사항

1. **초기 데이터 부족**: 처음에는 100일치만 있음 → 모델 학습 불가
2. **점진적 축적**: 매일 자동 수집으로 데이터 증가
3. **백필 권장**: 초기에 1년 데이터 수동 백필 강력 권장
4. **Regime 모델**: 최소 3개월 데이터 필요 (국면별 100+ 샘플)
5. **디스크 관리**: TimescaleDB 압축으로 30% 절감

---

## 다음 단계

1. **초기 데이터 백필** (1년치)
2. **1개월 데이터 축적** 대기
3. **첫 모델 학습** (단순 Ensemble)
4. **3개월 후 Regime 모델 활성화**
