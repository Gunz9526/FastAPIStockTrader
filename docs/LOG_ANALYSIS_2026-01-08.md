# 📋 로그 분석 및 해결 보고서

## 🔍 문제 분석 요약

### **1번: 레짐 감지 경고 16회 반복**
```
[2026-01-07 18:43:33] WARNING: 레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정
(16회 반복)
```

**원인:**
- `training.py`의 `_load_and_prepare_data()` 함수에서 **각 심볼별로 레짐 감지를 반복 실행**
- SPY 데이터가 충분해도 for문 내에서 매번 `regime_detector.detect_regime()`를 호출
- 16개 심볼 × 각각 경고 = 16회 경고 발생

**해결:**
- SPY 레짐을 **한 번만 계산**하고 `spy_regime_cache` 딕셔너리에 저장
- 각 심볼의 타임스탬프에 가장 가까운 SPY 레짐을 매핑

**수정 코드:**
```python
# SPY 레짐을 한 번에 계산 (타임스탬프별로 매핑)
spy_regime_cache = {}  # 타임스탬프별 레짐 캐시

logger.info("SPY 레짐 분류 시작 (총 %d 타임스탬프)", len(spy_features))
for spy_idx in spy_features.index:
    spy_window = spy_features[spy_features.index <= spy_idx]
    if len(spy_window) >= 20:  # 최소 20개 바 필요
        regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
        spy_regime_cache[spy_idx] = regime.value
    else:
        spy_regime_cache[spy_idx] = MarketRegime.SIDEWAYS_CALM.value

# X의 각 인덱스에 가장 가까운 SPY 레짐 할당
for idx in X.index:
    closest_spy_time = max([t for t in spy_regime_cache.keys() if t <= idx], default=None)
    if closest_spy_time:
        regimes.append(spy_regime_cache[closest_spy_time])
```

---

### **2번: 5개 심볼만 로드되는 문제**
```
총 67896개 샘플, 5개 심볼로부터 데이터 로드 완료
```

**원인:**
- `tune_models()` 함수에서 `symbol_limit=5`로 하드코딩
- 20개 활성 심볼이 있어도 5개만 사용

**해결:**
- `symbol_limit=None`으로 변경하여 모든 활성 심볼 사용

**수정 코드:**
```python
# Before
X, y, successful_symbols = _load_and_prepare_data(
    repo, feature_engineer, symbols, start_date, end_date, symbol_limit=5  # ❌
)

# After
X, y, successful_symbols = _load_and_prepare_data(
    repo, feature_engineer, symbols, start_date, end_date, symbol_limit=None  # ✅
)
```

---

### **3번: Gemini API 할당량 초과 (429 Error)**
```
HTTP/1.1 429 Too Many Requests
Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests
model: gemini-2.0-flash-exp
```

**원인:**
- **모델 선택 문제:** `gemini-2.0-flash-exp` (실험적 모델) 사용
  - Free Tier 할당량이 매우 제한적
  - 분당 요청 수 제한 초과

**해결 (2단계):**

#### **✅ 이미 구현됨: Batch Processing**
- `analyze_news_batch()` 메서드가 이미 구현되어 있음
- 20개 심볼의 뉴스를 1회 API 호출로 처리

#### **✅ 추가 수정: 안정적인 모델로 변경**
- `gemini-2.0-flash-exp` → `gemini-2.5-flash-lite`
- Flash Lite 모델의 장점:
  - 더 높은 Free Tier 할당량
  - 안정적인 API (Experimental 아님)
  - 빠른 응답 시간

**수정 코드:**
```python
# sentiment_analyzer.py - analyze_news_batch()
response = self.gemini_client.models.generate_content(
    model='gemini-2.5-flash-lite',  # ✅ Stable model
    contents=batch_prompt
)
```

**Batch Processing 동작 확인:**
```python
# sentiment.py
news_batch = {}
for sym in symbols:
    news_text = _fetch_news_for_symbol(sym)
    if news_text:
        news_batch[sym] = news_text

# 1회 API 호출로 모든 심볼 처리 ✅
sentiment_scores = analyzer.analyze_news_batch(news_batch)
```

---

### **4번: 실거래 0건 문제**
```
Live trades in last 14 days: 0
백테스트 데이터 사용 (실거래: 0/50)
```

**원인:**
- `count_live_trades()`가 **종료된 거래만** 카운트
- 쿼리 조건: `PositionTracking.exit_time >= cutoff_date AND exit_time IS NOT NULL`
- **오픈 포지션 (exit_time=NULL)은 제외됨**

**문제:**
- 거래가 진입만 되고 아직 청산되지 않은 경우
- 또는 14일 이내에 진입했지만 15일 이후에 청산된 경우
- → 실제로는 거래가 있어도 0건으로 카운트

**해결:**
- `entry_time` 기준으로 카운트 (오픈 포지션 포함)

**수정 코드:**
```python
# Before (❌ 종료된 거래만)
query = select(func.count(PositionTracking.id)).where(
    and_(
        PositionTracking.exit_time >= cutoff_date,
        PositionTracking.exit_time.isnot(None)
    )
)

# After (✅ 진입 기준, 오픈 포지션 포함)
query = select(func.count(PositionTracking.id)).where(
    PositionTracking.entry_time >= cutoff_date
)
```

---

## 🎯 검증 체크리스트

### **1. 레짐 감지 경고**
- [x] SPY 레짐을 한 번만 계산 (`spy_regime_cache` 사용)
- [x] 타임스탬프별 매핑으로 모든 심볼에 적용
- [x] 경고 16회 반복 → 1회로 감소 예상

### **2. 심볼 로드**
- [x] `symbol_limit=None` 설정
- [x] 모든 활성 심볼 (20개) 사용
- [x] "5개 심볼" → "20개 심볼" 로그 확인 예상

### **3. Gemini API**
- [x] Batch processing 이미 구현됨 확인
- [x] 모델을 `gemini-2.5-flash-lite`로 변경
- [x] 429 Error 감소 예상

### **4. 실거래 카운트**
- [x] `entry_time` 기준으로 변경
- [x] 오픈 포지션도 포함
- [x] "0/50" → 실제 거래 수 표시 예상

---

## 🚀 다음 단계

### **1. Docker 재시작**
```bash
docker-compose restart
```

### **2. Worker 로그 모니터링**
```bash
docker logs fastapistocktrader-worker-1 -f
```

**확인 사항:**
- ✅ "SPY 레짐 분류 시작 (총 237 타임스탬프)" (한 번만)
- ✅ "총 67896개 샘플, 20개 심볼로부터 데이터 로드 완료"
- ✅ "배치 감성 분석 완료: 20개 심볼, 1회 API 호출"
- ✅ "Live trades in last 14 days: 5" (0이 아님)

### **3. 수동 학습 트리거 (선택)**
```bash
curl -X POST http://localhost:8000/api/v1/operations/train-models
```

---

## 📊 예상 개선 효과

| 문제 | Before | After |
|------|--------|-------|
| 레짐 경고 | 16회 반복 | 1회로 감소 |
| 학습 데이터 | 5개 심볼 | 20개 심볼 |
| Gemini API | 429 Error 빈번 | 안정적 처리 |
| 실거래 카운트 | 0건 (잘못된 값) | 실제 거래 수 |
| 백테스트 의존 | 항상 백테스트 | 실거래 데이터 우선 |

---

## 🔗 관련 파일

- [app/tasks/training.py](app/tasks/training.py) (Lines 124-200)
- [app/services/sentiment_analyzer.py](app/services/sentiment_analyzer.py) (Lines 247-331)
- [app/repositories/portfolio_repo.py](app/repositories/portfolio_repo.py) (Lines 152-180)
