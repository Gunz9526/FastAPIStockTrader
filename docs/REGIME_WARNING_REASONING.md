# 🔍 레짐 감지 경고 원인 분석 (Reasoning)

## ❗ 문제 상황
```
[2026-01-08 09:30:13,566: WARNING/ForkPoolWorker-3] 레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정
```

## 🧐 Reasoning Process

### **1단계: 경고 발생 위치 추적**

**파일:** `app/services/regime.py` (Line 44)
```python
def detect_regime(self, df: pd.DataFrame, vix_value: Optional[float] = None) -> MarketRegime:
    if df.empty or len(df) < 50:
        logger.warning("레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정")
        return MarketRegime.SIDEWAYS_CALM
```

**조건:** `df.empty` OR `len(df) < 50`

---

### **2단계: 호출 경로 분석**

#### **경로 A: training.py → _load_and_prepare_data()**
```python
# Line 187: SPY 레짐 분류 시작
for spy_idx in spy_features.index:
    spy_window = spy_features[spy_features.index <= spy_idx]
    if len(spy_window) >= 20:  # ⚠️ 최소 20개
        regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
        spy_regime_cache[spy_idx] = regime.value
    else:
        spy_regime_cache[spy_idx] = MarketRegime.SIDEWAYS_CALM.value
```

**문제점:**
- `spy_window`는 **점진적으로 증가**하는 윈도우
- 첫 19개 인덱스는 `len(spy_window) < 20`이므로 `detect_regime()` **호출하지 않음**
- 20번째부터 호출 시작

**그런데 왜 경고가 뜰까?**
→ 20번째 인덱스에서 `len(spy_window) = 20`일 때 `detect_regime()` 호출
→ 하지만 `detect_regime()` 내부에서 **len(df) < 50** 체크!
→ **20 < 50이므로 경고 발생** ✅

---

### **3단계: 수정했는데도 경고가 나는 이유**

**이전 수정:**
```python
# SPY 레짐을 한 번만 계산하고 캐싱
spy_regime_cache = {}
for spy_idx in spy_features.index:
    spy_window = spy_features[spy_features.index <= spy_idx]
    if len(spy_window) >= 20:
        regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
```

**여전히 문제인 이유:**
1. `spy_features`가 1000개 타임스탬프라면
2. 인덱스 0~19: `len(spy_window) < 20` → 호출 안 함
3. 인덱스 20~49: `len(spy_window) = 20~49` → **호출하지만 경고 발생** (< 50)
4. 인덱스 50~999: `len(spy_window) >= 50` → 정상 작동

**결과:** 30개 타임스탬프 (20~49)에서 경고 30회 발생 ❌

---

### **4단계: 근본 원인 정리**

| 조건 | spy_window 크기 | detect_regime() 호출 | 결과 |
|------|----------------|---------------------|------|
| idx < 20 | < 20 | ❌ 호출 안 함 | 캐시에 SIDEWAYS_CALM 직접 저장 |
| 20 ≤ idx < 50 | 20~49 | ✅ 호출 | **경고 발생** (len < 50) ⚠️ |
| idx ≥ 50 | ≥ 50 | ✅ 호출 | 정상 작동 ✅ |

**핵심:**
- `training.py`에서 `len(spy_window) >= 20`으로 체크
- 하지만 `regime.py`에서 `len(df) < 50`으로 체크
- **임계값 불일치** (20 vs 50) → 30개 타임스탬프에서 경고 발생

---

### **5단계: 해결 방안**

#### **Option 1: training.py 임계값 상향 (권장 ✅)**
```python
# Before
if len(spy_window) >= 20:  # ❌
    regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)

# After
if len(spy_window) >= 50:  # ✅
    regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
```

**장점:**
- 경고 완전히 제거
- `detect_regime()` 내부 로직과 일치

**단점:**
- 처음 49개 타임스탬프는 SIDEWAYS_CALM으로 고정

---

#### **Option 2: regime.py 임계값 하향**
```python
# Before
if df.empty or len(df) < 50:  # ❌
    logger.warning("레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정")

# After
if df.empty or len(df) < 20:  # ✅
    logger.warning("레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정")
```

**장점:**
- 더 적은 데이터로도 레짐 감지 가능

**단점:**
- ADX, ATR 계산에 최소 50개 바 권장 (부정확할 수 있음)

---

## ✅ 최종 추천: Option 1

**이유:**
- 15분봉 50개 = 12.5시간 데이터 (충분한 통계량)
- ADX (14 period), ATR (14 period), SMA (50 period) 모두 정확히 계산 가능
- 처음 49개 타임스탬프는 전체 데이터에서 극히 일부

**수정 코드:**
```python
# app/tasks/training.py Line 187
if len(spy_window) >= 50:  # 50개 바 확보 시에만 레짐 감지
    regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
    spy_regime_cache[spy_idx] = regime.value
else:
    spy_regime_cache[spy_idx] = MarketRegime.SIDEWAYS_CALM.value
```

---

## 📊 검증 방법

### **Before (현재)**
```
[경고 30회 반복]
레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정
레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정
...
```

### **After (수정 후)**
```
SPY 레짐 분류 시작 (총 237 타임스탬프)
레짐 분포: {'bull_trending': 5784, 'sideways_calm': 56296, ...}
```

**경고 0회 예상** ✅
