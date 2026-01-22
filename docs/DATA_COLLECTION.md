# 데이터 수집 가이드

## 1. 재무 데이터 수집 (YFinance)

### API를 통한 수동 트리거
```bash
curl -X POST http://SERVER_IP:8000/api/v1/operations/collect-data \
  -H "X-API-Key: your-api-key"
```

**수집 데이터**:
- PER (Price-to-Earnings Ratio)
- PBR (Price-to-Book Ratio)
- ROE (Return on Equity)
- Market Cap (시가총액)
- Sector (섹터)

**저장 테이블**: `stock_fundamentals`, `stock_tickers`

---

### 직접 실행 (Python 스크립트)
```bash
# Docker 컨테이너 내부에서
docker-compose exec app python scripts/fetch_fundamentals.py
```

---

### 자동 스케줄 (Celery Beat)
```python
# app/worker.py
'daily-fundamentals': {
    'task': 'app.tasks.data_tasks.collect_fundamentals',
    'schedule': crontab(hour=18, minute=0),  # 매일 18:00
}
```

---

## 2. OHLCV 데이터 수집 (Alpaca)

**자동 수집**: 
- 거래 실행 시 자동으로 100일 분량의 OHLCV 데이터를 가져와 `stock_ohlcv` 테이블에 저장됩니다.
- `TradingStrategyEngine.analyze_and_execute()` 호출 시 자동 처리

**저장 테이블**: `stock_ohlcv` (TimescaleDB 하이퍼테이블)

---

## 3. 수동 데이터 검증

### Alpaca 연동 테스트
```bash
docker-compose exec app python scripts/verify_alpaca.py
```

**실행 결과**:
- Account Status 확인
- SPY 현재가 조회
- AAPL 테스트 주문 (Paper Trading)

---

## 4. 전체 데이터 흐름

```
1. 종목 추가
   ↓
2. 재무 데이터 수집 (YFinance API → /operations/collect-data)
   ↓
3. OHLCV 데이터 (Alpaca, 거래 시 자동)
   ↓
4. 기술 지표 계산 (17개)
   ↓
5. 시장 국면 감지
   ↓
6. ML 예측 & 전략 실행
```

---

## 5. 초기 데이터 수집 권장 순서

```bash
# 1. 종목 추가
docker-compose exec db psql -U postgres -d stocktrader
INSERT INTO stock_tickers (symbol, name, market, is_active)
VALUES
  ('AAPL', 'Apple Inc.', 'NASDAQ', true),
  ('MSFT', 'Microsoft Corporation', 'NASDAQ', true),
  ('GOOGL', 'Alphabet Inc.', 'NASDAQ', true),
  ('TSLA', 'Tesla Inc.', 'NASDAQ', true),
  ('NVDA', 'NVIDIA Corporation', 'NASDAQ', true);
\q

# 2. 재무 데이터 수집
curl -X POST http://SERVER:8000/api/v1/operations/collect-data \
  -H "X-API-Key: your-key"

# 3. OHLCV는 자동 (또는 수동 스캔 트리거)
curl -X POST http://SERVER:8000/api/v1/operations/execute-scan \
  -H "X-API-Key: your-key"

# 4. 모델 학습 (충분한 데이터 수집 후)
curl -X POST http://SERVER:8000/api/v1/operations/train-models-regime \
  -H "X-API-Key: your-key"
```

---

## 6. 데이터 확인

```sql
-- PostgreSQL 접속
docker-compose exec db psql -U postgres -d stocktrader

-- 재무 데이터 확인
SELECT symbol, date, per, pbr, market_cap 
FROM stock_fundamentals 
ORDER BY date DESC 
LIMIT 10;

-- OHLCV 데이터 확인
SELECT symbol, date_time, close, volume 
FROM stock_ohlcv 
ORDER BY date_time DESC 
LIMIT 10;

-- 데이터 카운트
SELECT 
  (SELECT COUNT(*) FROM stock_fundamentals) as fundamentals_count,
  (SELECT COUNT(*) FROM stock_ohlcv) as ohlcv_count;
```

---

## 7. 트러블슈팅

### 재무 데이터 수집 실패
```bash
# 로그 확인
docker-compose logs app | grep fundamentals

# YFinance API 응답 느림 → 정상 (종목별 순차 처리)
# 종목이 많으면 시간 오래 걸림
```

### OHLCV 데이터 없음
```bash
# Alpaca API 키 확인
docker-compose logs app | grep Alpaca

# 수동 검증
docker-compose exec app python scripts/verify_alpaca.py
```

### 데이터베이스 연결 에러
```bash
# DB 상태 확인
docker-compose ps db

# 재시작
docker-compose restart db app
```
