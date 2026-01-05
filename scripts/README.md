# Scripts 디렉토리 파일 설명

**경로**: `scripts/`

---

## 📝 파일 목록 및 용도

### 1. `create_rag_user.sql`

**용도**: RAG 서비스용 READ ONLY 데이터베이스 계정 생성

**기능**:
- `rag_reader` 사용자 생성
- 모든 테이블에 대한 SELECT 권한 부여
- INSERT/UPDATE/DELETE 권한 없음 (보안)

**실행 방법**:
```bash
docker-compose exec db psql -U postgres -d stocktrader \
  -f /app/scripts/create_rag_user.sql
```

**RAG 서비스 연동 시 사용**:
```python
# RAG Service Connection String
DATABASE_URL=postgresql://rag_reader:password@db:5432/stocktrader
```

---

### 2. `verify_alpaca.py`

**용도**: Alpaca API 연결 및 기능 검증 스크립트

**기능**:
1. **계정 연결 확인**: TradingClient 초기화 및 계좌 상태 확인
2. **데이터 조회 테스트**: SPY 현재가 조회
3. **Paper Trading 테스트**: AAPL 1주 매수 주문 (테스트용)

**실행 방법**:
```bash
# 로컬에서 실행
python scripts/verify_alpaca.py

# Docker 컨테이너 내부에서 실행
docker-compose exec app python scripts/verify_alpaca.py
```

**예상 출력**:
```
--- Verifying Alpaca Integration ---
Base URL: https://paper-api.alpaca.markets
[PASS] Account Status: ACTIVE
       Buying Power: $100000.00
[PASS] Current Price for SPY: $475.23
Attempting Paper Trade on AAPL...
[PASS] Order Placed. ID: 123abc-456def
```

**주의사항**:
- `.env` 파일에 `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` 필요
- 시장이 닫혀 있으면 주문이 실패할 수 있음

---

### 3. `fetch_fundamentals.py`

**용도**: YFinance를 통해 종목 재무 데이터 수집 및 DB 업데이트

**기능**:
1. **활성 종목 조회**: `stock_tickers` 테이블에서 `is_active=true`인 종목 가져오기
2. **YFinance 데이터 수집**:
   - PER (Price-to-Earnings Ratio)
   - PBR (Price-to-Book Ratio)
   - ROE (Return on Equity)
   - Market Cap (시가총액)
   - Sector (섹터)
3. **DB 업데이트**:
   - `stock_fundamentals` 테이블에 저장/업데이트
   - `stock_tickers` 테이블의 sector 업데이트

**실행 방법**:
```bash
# 로컬에서 실행
python scripts/fetch_fundamentals.py

# Docker 컨테이너 내부에서 실행
docker-compose exec app python scripts/fetch_fundamentals.py

# Celery Task로 자동 실행 (매일)
# worker.py의 beat schedule 참조
```

**예상 출력**:
```
Starting Fundamentals Fetcher...
Processing AAPL...
Updated AAPL: Sector=Technology, PER=28.5
Processing MSFT...
Updated MSFT: Sector=Technology, PER=32.1
Fundamentals Fetch Completed.
```

**필수 조건**:
- `yfinance` 패키지 설치 (requirements.txt에 포함)
- `stock_tickers` 테이블에 종목 데이터 존재
- 인터넷 연결 (YFinance API 접근)

---

## 🔧 사용 시나리오

### 1. 초기 설정 시
```bash
# 1. RAG 계정 생성
docker-compose exec db psql -U postgres -d stocktrader \
  -f /app/scripts/create_rag_user.sql

# 2. Alpaca 연동 검증
docker-compose exec app python scripts/verify_alpaca.py

# 3. 재무 데이터 수집
docker-compose exec app python scripts/fetch_fundamentals.py
```

### 2. 자동화 (Celery Beat)
`app/worker.py`에서 스케줄링:
```python
'daily-fundamentals': {
    'task': 'app.tasks.data_tasks.collect_fundamentals',
    'schedule': crontab(hour=18, minute=0),  # 매일 18:00
}
```

### 3. RAG 서비스 연동 시
`create_rag_user.sql` 실행 후:
```bash
# RAG 서비스에서 사용
export DATABASE_URL=postgresql://rag_reader:password@db:5432/stocktrader

# Python 예시
from sqlalchemy import create_engine
engine = create_engine("postgresql://rag_reader:password@db:5432/stocktrader")
df = pd.read_sql("SELECT * FROM stock_fundamentals WHERE symbol='AAPL'", engine)
```

---

## 📊 데이터 흐름

```
YFinance API
    ↓
fetch_fundamentals.py
    ↓
stock_fundamentals 테이블
    ↓
RAG Service (rag_reader 계정)
    ↓
/rag/fundamentals/{symbol} API
```

---

## ⚠️ 주의사항

1. **`verify_alpaca.py`**:
   - Paper Trading 환경에서만 실행
   - 실제 주문이 발생하므로 주의
   - 시장 개장 시간에만 정상 작동

2. **`fetch_fundamentals.py`**:
   - YFinance API 속도 제한 있음 (너무 많은 종목 동시 조회 금지)
   - 종목이 많으면 시간 오래 걸림
   - 일부 종목은 데이터 없을 수 있음 (NULL 허용)

3. **`create_rag_user.sql`**:
   - **프로덕션에서 비밀번호 변경 필수**
   - READ ONLY 권한만 부여되므로 안전

---

## 🔗 관련 파일

- `app/tasks/data_tasks.py`: fetch_fundamentals를 Celery Task로 래핑
- `app/services/data_provider.py`: verify_alpaca와 유사한 로직
- `docs/RAG_INTEGRATION.md`: RAG 연동 전체 가이드
