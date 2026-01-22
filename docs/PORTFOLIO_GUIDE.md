# 포트폴리오 종목 추가 가이드

## 📊 **상관관계 기준**

### **낮은 상관관계가 좋은 이유**

```
포트폴리오 분산 효과 = 위험 감소 + 수익률 안정화
```

**핵심 원리:**
- **높은 상관관계 (>0.7)**: 같이 오르고 같이 내림 → 분산 효과 없음
- **낮은 상관관계 (<0.3)**: 독립적 움직임 → 위험 분산
- **음의 상관관계 (<0)**: 반대로 움직임 → 헷지 효과

**예시:**
```python
# 나쁜 포트폴리오 (높은 상관관계)
AAPL, MSFT, GOOGL, NVDA  # 모두 Tech → 상관관계 0.8+
→ Tech 섹터 폭락 시 전체 손실

# 좋은 포트폴리오 (낮은 상관관계)
AAPL (Tech), JPM (Finance), JNJ (Healthcare), XOM (Energy)
→ 섹터 분산 → 상관관계 0.2~0.4
→ 한 섹터 하락해도 다른 섹터가 방어
```

---

## 🎯 **추천 종목 구성**

### **1. 필수 ETF (시장 지수)**
- **SPY**: S&P 500 (대형주 지수, 레짐 감지용)
- **QQQ**: NASDAQ 100 (Tech 중심)
- **IWM**: Russell 2000 (소형주)
- **DIA**: Dow Jones (가치주 중심)

**용도:**
- 시장 레짐 감지 (SPY)
- 섹터 로테이션 전략
- 상대 강도 비교

### **2. 섹터별 대표주 (낮은 상관관계)**

#### **Technology (25%)**
- AAPL, MSFT, NVDA (이미 보유)
- 추가: CRM (Salesforce), ORCL (Oracle)

#### **Consumer (20%)**
- 기존: TSLA (Consumer Cyclical)
- 추가: WMT (Walmart), PG (P&G), KO (Coca-Cola)

#### **Healthcare (20%)**
- JNJ (Johnson & Johnson)
- UNH (UnitedHealth)
- PFE (Pfizer)

#### **Financial (20%)**
- JPM (JPMorgan)
- BAC (Bank of America)
- V (Visa)

#### **Energy (15%)**
- XOM (Exxon Mobil)
- CVX (Chevron)

---

## 📈 **상관관계 매트릭스 예시**

```
        AAPL  MSFT  JPM   JNJ   XOM
AAPL    1.00  0.85  0.42  0.38  0.25
MSFT    0.85  1.00  0.45  0.40  0.28
JPM     0.42  0.45  1.00  0.35  0.50
JNJ     0.38  0.40  0.35  1.00  0.20
XOM     0.25  0.28  0.50  0.20  1.00
```

**해석:**
- AAPL-MSFT: 0.85 (높음) → 같은 섹터
- AAPL-XOM: 0.25 (낮음) → 다른 섹터
- **목표: 평균 상관관계 < 0.5**

---

## 🛠️ **종목 추가 절차**

### **Step 1: 스크립트 실행**
```bash
# Docker 컨테이너에서 실행
docker exec fastapistocktrader-app-1 python /app/scripts/add_symbols.py
```

**결과:**
- SPY, QQQ 등 18개 종목 추가
- `is_active=True` 설정
- 기존 종목은 건너뛰기

### **Step 2: 데이터 백필**
```bash
# 90일치 15분봉 데이터 수집
docker exec fastapistocktrader-app-1 python /app/scripts/backfill_ohlcv.py --days 90
```

**예상 시간:** 약 10-15분 (18개 종목 × 90일)

### **Step 3: 학습 재실행**
```bash
# 수동 트리거 (또는 다음 일요일 자동 실행)
docker exec fastapistocktrader-app-1 python -c "
from app.tasks.training import train_ml_ensemble
train_ml_ensemble.delay()
"
```

**결과:**
- SPY 데이터 포함 → 레짐 분류 정상 동작
- 다양한 섹터 → 모델 일반화 성능 향상

---

## ✅ **검증 방법**

### **1. DB 확인**
```bash
docker exec fastapistocktrader-postgres-1 psql -U stockuser -d stockdb -c "
SELECT symbol, name, sector, is_active FROM stock_ticker WHERE is_active = true ORDER BY symbol;
"
```

### **2. 학습 로그 확인**
```bash
docker logs fastapistocktrader-worker-1 --tail 100 | grep "레짐 분포"
```

**기대 결과:**
```
레짐 분포: {'bull_trending': 15000, 'sideways_calm': 50000, ...}
```
(더 이상 100% SIDEWAYS_CALM 아님)

### **3. 상관관계 매트릭스 확인**
```python
# Python 스크립트
from app.services.portfolio_optimizer import PortfolioOptimizer
optimizer = PortfolioOptimizer()
corr_matrix = optimizer.calculate_correlation_matrix(repo, symbols)
print(corr_matrix)
```

**목표:**
- 대각선 제외 평균 상관관계: 0.3~0.5
- 최대 상관관계: < 0.7

---

## 🚀 **최적 포트폴리오 예시**

### **보수적 (낮은 변동성)**
```python
symbols = ['SPY', 'JNJ', 'PG', 'KO', 'WMT']  # 평균 상관관계 0.35
```

### **균형형 (중간 위험)**
```python
symbols = ['SPY', 'AAPL', 'JPM', 'JNJ', 'XOM']  # 평균 상관관계 0.42
```

### **공격적 (높은 성장)**
```python
symbols = ['QQQ', 'NVDA', 'TSLA', 'CRM', 'ADBE']  # 평균 상관관계 0.65
# 주의: 높은 상관관계 → 위험 집중
```

---

## 📝 **추가 고려사항**

1. **시가총액**: 대형주 위주 (유동성 확보)
2. **거래량**: 일평균 1천만주 이상
3. **변동성**: ATR 2-5% 범위
4. **뉴스 커버리지**: Finnhub API 지원 종목

**현재 설정:**
- 최대 동시 포지션: 5개
- 포트폴리오 최적화: Kelly Criterion
- 상관관계 필터: < 0.7 (코드 설정)
