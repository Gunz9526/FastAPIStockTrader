# 🔍 Task 1: 여러 포지션 동시 구매 로직 검증 결과

## ✅ 검증 완료

### **핵심 로직: `process_portfolio()` (trading_strategy_sync.py Line 407)**

```python
def process_portfolio(self, symbols: List[str]):
    """
    여러 심볼에 대한 다중 포지션 포트폴리오 거래 처리 (Phase I.2).
    
    전략:
    1. 시장 레짐 감지
    2. 현재 활성 포지션 가져오기
    3. 각 심볼에 대한 Kelly 포지션 크기 계산
    4. 비상관 심볼 선택 (최대 5개 포지션)
    5. 신호 및 포트폴리오 최적화 기반으로 BUY/SELL 주문 실행
    """
```

---

## 📋 동시 구매 가능 여부: **YES ✅**

### **1. 최대 포지션 제한**
```python
self.max_positions = 5  # Line 50
```

### **2. 현재 활성 포지션 조회**
```python
active_positions = self.portfolio_repo.get_all_active_positions()  # Line 428
active_symbols = {pos['symbol'] for pos in active_positions}
logger.info("활성 포지션: %d / %d", len(active_positions), self.max_positions)
```

### **3. 비상관 심볼 선택 (핵심 필터링)**
```python
selected_symbols = self._select_uncorrelated_symbols(
    signals, 
    corr_matrix, 
    active_symbols,
    max_new_positions=self.max_positions - len(active_positions)  # Line 505
)
```

**필터링 기준:**
- 신호 강도: `signal > 0.005` (0.5% 이상 예상 수익)
- 상관관계: `max_corr < 0.7` (기존 포지션과 상관계수 0.7 미만)
- 최대 신규: `max_positions - len(active_positions)` (예: 2개 보유 시 3개까지 신규 구매 가능)

### **4. 거래 실행 로직**
```python
for symbol in signals.keys():
    signal_data = signals[symbol]
    
    if symbol in active_symbols:
        # 이미 보유 → SELL 확인
        self._process_sell_signal(symbol, signal_data)
    elif symbol in selected_symbols and len(active_positions) < self.max_positions:
        # 신규 → BUY 확인 (Line 517)
        self._process_buy_signal(symbol, signal_data, portfolio_value)
```

**조건:**
- `symbol in selected_symbols`: 비상관 필터 통과
- `len(active_positions) < self.max_positions`: 최대 포지션 미만

---

## 🎯 동시 구매 시나리오

### **시나리오 1: 포지션 0개 보유**
- 필터 통과 종목: AAPL, MSFT, JPM, JNJ, XOM (5개)
- 상관관계 체크: 모두 0.7 미만
- **결과: 5개 모두 동시 구매 가능** ✅

### **시나리오 2: 포지션 2개 보유 (AAPL, MSFT)**
- 필터 통과 종목: GOOGL, JPM, JNJ, XOM, WMT (5개)
- 상관관계 체크:
  - GOOGL vs AAPL: 0.82 ❌ (AAPL과 높은 상관)
  - JPM vs AAPL/MSFT: 0.42 ✅
  - JNJ vs AAPL/MSFT: 0.38 ✅
  - XOM vs AAPL/MSFT: 0.25 ✅
- **결과: JPM, JNJ, XOM 3개 동시 구매 가능** ✅ (최대 5 - 현재 2 = 3개)

### **시나리오 3: 포지션 4개 보유**
- 필터 통과 종목: 여러 개
- **결과: 1개만 추가 구매 가능** ✅ (최대 5 - 현재 4 = 1개)

---

## ⚠️ 주의사항

### **1. Risk Manager 추가 검증**
```python
can_enter, reason = self.risk_manager.can_enter_position(symbol)
if not can_enter:
    logger.info("%s BUY 차단: %s", symbol, reason)
    return
```

**RiskManager 체크 항목:**
- 쿨다운: 60분 이내 재진입 금지
- Circuit Breaker: 일일 거래 10회 제한, 손실 $1000 제한
- 포트폴리오 리스크: 2% VaR 초과 시 차단

### **2. Kelly Criterion 포지션 크기**
```python
kelly_fraction = signal_data['kelly']
position_value = portfolio_value * kelly_fraction
qty = int(position_value / current_price)

if qty < 1:
    logger.info("%s BUY 건너뛰기: Kelly 크기 너무 작음", symbol)
    return
```

**최소 1주 이상만 구매**

---

## ✅ 결론

**동시 구매 가능: YES**

**조건:**
1. 최대 5개 포지션 제한
2. 비상관 필터 통과 (corr < 0.7)
3. 신호 강도 충족 (signal > 0.005)
4. RiskManager 승인
5. Kelly 크기 >= 1주

**예시:**
- 0개 보유 → **최대 5개 동시 구매** ✅
- 2개 보유 → **최대 3개 동시 구매** ✅
- 4개 보유 → **최대 1개 구매** ✅
- 5개 보유 → **구매 불가, SELL만 가능** ⚠️
