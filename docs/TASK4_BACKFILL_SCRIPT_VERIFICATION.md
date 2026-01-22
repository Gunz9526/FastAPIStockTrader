# Task 4: backfill_ohlcv.py 스크립트 검증

## 작업 날짜
2026-01-15

## 목표
15분봉 2년치 데이터 수집 로직 검증

---

## 스크립트 개요

**파일 위치:** `scripts/backfill_ohlcv.py`

**주요 기능:**
- Alpaca API로 과거 OHLCV 데이터 수집
- TimescaleDB에 중복 없이 저장
- 개별 bar 단위 중복 체크
- Rate limiting 준수

---

## 핵심 로직 검증

### 1. 기간 계산 (Lines 62-63)
```python
BACKFILL_YEARS = 2  # Line 31

# Line 62
start_date = end_date - timedelta(days=years * 365)
```

**검증 결과:**
- ✅ 2년 = 730일 (365 * 2)
- ✅ 윤년 미고려 (Alpaca API가 유효한 거래일만 반환하므로 문제없음)
- ✅ end_date는 현재 시각 (pd.Timestamp.now(tz='UTC'))

**계산 예시:**
```
end_date = 2026-01-15 00:00:00+00:00
start_date = 2024-01-15 00:00:00+00:00 (정확히 2년 전)
```

---

### 2. 15분봉 요청 (Lines 81-87)
```python
request = StockBarsRequest(
    symbol_or_symbols=symbol,
    timeframe=TimeFrame(15, TimeFrameUnit.Minute),  # 15분봉
    start=start_date,
    end=end_date,
    limit=10000,
    adjustment='split'
)
```

**검증 결과:**
- ✅ Timeframe: 15분봉 정확히 명시
- ✅ Limit: 10,000개 (Alpaca API 최대 제한)
- ✅ Adjustment: 'split' (주식 분할 조정)
- ✅ start, end 날짜: 2년치

**15분봉 예상 개수 계산:**
```
거래시간: 09:30 ~ 16:00 (6.5시간 = 390분)
15분봉 개수/일: 390 / 15 = 26개
2년 거래일: 약 504일 (252일/년 * 2년)
예상 총 개수: 26 * 504 = 13,104개
```

**문제점:**
- ⚠️ Limit이 10,000개로 설정되어 있어 13,104개 중 일부만 가져올 가능성 있음
- ⚠️ Alpaca API는 limit에 도달하면 가장 최근 10,000개만 반환

---

### 3. 중복 체크 로직 (Lines 104-127)
```python
for bar in bars:
    bar_time = bar.timestamp.replace(tzinfo=UTC)
    
    # 개별 bar 중복 체크
    existing = db.query(StockOHLCV).filter(
        StockOHLCV.symbol == symbol,
        StockOHLCV.date_time == bar_time,
        StockOHLCV.timeframe == '15m'
    ).first()
    
    if existing:
        logger.debug(f"{symbol} 이미 존재하는 15m bar: {bar_time}")
        continue
    
    # 새로운 bar 저장
    new_bar = StockOHLCV(
        symbol=symbol,
        date_time=bar_time,
        open=bar.open,
        high=bar.high,
        low=bar.low,
        close=bar.close,
        volume=bar.volume,
        vwap=bar.vwap,
        timeframe='15m'
    )
    db.add(new_bar)
    count += 1
```

**검증 결과:**
- ✅ 개별 bar 단위 중복 체크 (symbol + date_time + timeframe)
- ✅ 중복된 bar는 skip (logger.debug로 기록)
- ✅ 새로운 bar만 DB에 추가
- ✅ TimescaleDB 하이퍼테이블 최적화 활용

**장점:**
- 부분 실패 시 재실행 가능 (멱등성)
- 네트워크 오류 복구 가능

**단점:**
- 개별 쿼리로 인한 성능 저하 가능 (bulk insert 대비)

---

### 4. Rate Limiting (Lines 131-132)
```python
logger.info(f"{symbol} 15m 백필 완료: {count}개 저장")
time.sleep(0.5)  # Rate limiting
```

**검증 결과:**
- ✅ 각 심볼 처리 후 0.5초 대기
- ✅ Alpaca API rate limit 준수 (200 req/min = 분당 120개 가능)

**실제 속도:**
```
종목당 대기시간: 0.5초
100개 종목 처리: 50초
```

---

## 전체 워크플로우

### 1. 초기화 (Lines 36-59)
```python
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()
api_key = os.getenv("ALPACA_API_KEY")
api_secret = os.getenv("ALPACA_SECRET_KEY")
```
- ✅ 환경 변수 로드
- ✅ Alpaca API 인증

### 2. 종목 선택 (Lines 139-147)
```python
active_symbols = repo.get_active_symbols()
if not active_symbols:
    logger.error("활성 종목이 없습니다")
    return

logger.info(f"총 {len(active_symbols)}개 종목 백필 시작 (각 {years}년치)")
```
- ✅ `stock_tickers` 테이블에서 `is_active=True` 종목 조회
- ✅ 없으면 에러 출력 및 종료

### 3. 반복 처리 (Lines 149-157)
```python
for symbol in active_symbols:
    try:
        backfill_symbol(symbol, years=BACKFILL_YEARS)
    except Exception as e:
        logger.error(f"{symbol} 백필 실패: {e}", exc_info=True)
        continue  # 다음 종목 계속 진행
```
- ✅ 개별 종목 실패 시 다음 종목 계속 진행
- ✅ 상세 에러 로깅 (exc_info=True)

---

## 검증 결과 요약

### ✅ 정확성
1. **기간 계산:** 2년 = 730일 정확
2. **15분봉 요청:** TimeFrame(15, TimeFrameUnit.Minute) 정확
3. **중복 체크:** symbol + date_time + timeframe 복합 키로 정확

### ⚠️ 문제점

#### 1. Limit 제한 (CRITICAL)
- **문제:** `limit=10,000`으로 설정되어 있지만, 2년치 15분봉은 약 13,104개
- **결과:** Alpaca API가 최근 10,000개만 반환 (약 1.5년치)
- **해결 방안:**
  ```python
  # 페이지네이션 구현 필요
  # Option 1: 기간을 나눠서 요청 (6개월씩 4번)
  # Option 2: Alpaca API의 next_page_token 사용
  ```

#### 2. 성능 (MINOR)
- **문제:** 개별 bar 중복 체크 쿼리 (N번 SELECT)
- **해결 방안:**
  ```python
  # Bulk check: 전체 기간의 기존 bar를 한 번에 조회
  existing_bars = db.query(StockOHLCV).filter(
      StockOHLCV.symbol == symbol,
      StockOHLCV.timeframe == '15m',
      StockOHLCV.date_time.between(start_date, end_date)
  ).all()
  
  # Set으로 변환 후 O(1) lookup
  existing_times = {bar.date_time for bar in existing_bars}
  ```

---

## 권장 수정 사항

### 수정안 1: 페이지네이션 구현 (CRITICAL)
```python
def backfill_symbol(symbol: str, years: int = 2):
    # 6개월 단위로 나눠서 요청
    periods = [
        (end_date - timedelta(days=365*2), end_date - timedelta(days=365*1.5)),
        (end_date - timedelta(days=365*1.5), end_date - timedelta(days=365)),
        (end_date - timedelta(days=365), end_date - timedelta(days=182)),
        (end_date - timedelta(days=182), end_date)
    ]
    
    for start, end in periods:
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(15, TimeFrameUnit.Minute),
            start=start,
            end=end,
            limit=10000
        )
        # ... (기존 로직)
```

### 수정안 2: Bulk 중복 체크 (MINOR)
```python
# 기존 bar를 한 번에 조회
existing_bars = db.query(StockOHLCV).filter(
    StockOHLCV.symbol == symbol,
    StockOHLCV.timeframe == '15m',
    StockOHLCV.date_time >= start_date,
    StockOHLCV.date_time <= end_date
).all()

existing_times = {bar.date_time for bar in existing_bars}

# 개별 bar 처리
for bar in bars:
    bar_time = bar.timestamp.replace(tzinfo=UTC)
    
    if bar_time in existing_times:
        continue
    
    # ... (저장 로직)
```

---

## 테스트 시나리오

### 시나리오 1: 새 종목 백필
```bash
python scripts/backfill_ohlcv.py
```
**예상 결과:**
- 각 종목당 최근 10,000개 bar 저장 (약 1.5년치)
- ⚠️ 2년치가 아닌 1.5년치만 저장됨

### 시나리오 2: 기존 종목 재실행
```bash
python scripts/backfill_ohlcv.py
```
**예상 결과:**
- 중복된 bar는 skip
- 새로운 bar만 추가 (멱등성 보장)

### 시나리오 3: 일부 실패 후 재실행
```bash
# 네트워크 오류로 50개 종목 중 30개만 성공
python scripts/backfill_ohlcv.py
```
**예상 결과:**
- 성공한 30개는 skip
- 실패한 20개만 다시 시도

---

## 결론

### ✅ 현재 상태
- 15분봉 요청 로직 정확
- 중복 체크 로직 정확 (멱등성 보장)
- Rate limiting 준수
- 에러 핸들링 적절

### ⚠️ 수정 필요 (CRITICAL)
- **Limit 10,000개 제한:** 2년치(13,104개) 중 1.5년치만 가져옴
- **해결:** 페이지네이션 구현 (6개월 단위로 4번 요청)

### ⚠️ 수정 권장 (MINOR)
- **성능 개선:** Bulk 중복 체크로 DB 쿼리 횟수 감소

**프로덕션 배포 전 필수 수정:**
1. 페이지네이션 구현 (6개월씩 4번 요청)
2. 실제 데이터 개수 검증 (2년치 = 약 13,104개)

**다음 작업:** 수정안 구현 및 실제 데이터 검증
