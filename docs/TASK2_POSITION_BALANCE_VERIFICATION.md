# 🔍 Task 2: 포지션과 잔고 조회 로직 검증 결과

## ✅ 검증 완료

### **핵심: Alpaca API에서 직접 조회** ✅

---

## 📊 1. 포지션 조회

### **방법 A: 모든 포지션 조회**
**파일:** `portfolio_rebalancer.py` (Line 96)
```python
def _get_current_positions(self) -> Dict[str, Dict]:
    """
    Alpaca API에서 현재 포지션 조회
    
    Returns:
        Dict of {symbol: {'qty': int, 'market_value': float}}
    """
    positions = self.api.get_all_positions()  # ✅ Alpaca API 직접 호출
    current = {}
    
    for pos in positions:
        current[pos.symbol] = {
            'qty': int(pos.qty),
            'market_value': float(pos.market_value),
            'avg_entry_price': float(pos.avg_entry_price)
        }
    
    return current
```

### **방법 B: 특정 심볼 포지션 조회**
**파일:** `trading_strategy_sync.py` (Line 338)
```python
def _has_position(self, symbol: str) -> bool:
    """특정 심볼의 포지션 존재 여부 확인"""
    try:
        self.api.get_open_position(symbol)  # ✅ Alpaca API 직접 호출
        return True
    except Exception:
        return False
```

---

## 💰 2. 잔고 조회

### **계좌 정보 조회**
**파일:** `trading_strategy_sync.py` (Line 361, 434)
```python
# 1. BUY 전 매수 가능 금액 확인 (Line 361)
account = self.api.get_account()  # ✅ Alpaca API
buying_power = float(account.buying_power)

# 2. 포트폴리오 처리 시 (Line 434)
account = self.api.get_account()  # ✅ Alpaca API
portfolio_value = float(account.portfolio_value)
buying_power = float(account.buying_power)
```

### **Account 객체 속성**
```python
{
    'portfolio_value': 100000.00,  # 전체 계좌 가치
    'buying_power': 50000.00,      # 매수 가능 금액
    'cash': 30000.00,              # 현금 잔액
    'equity': 100000.00,           # 순자산 (현금 + 포지션)
    'last_equity': 98000.00,       # 전일 순자산
    'profit_loss': 2000.00,        # 손익
    'profit_loss_pct': 0.0204      # 손익률 (2.04%)
}
```

---

## 🔄 3. DB vs Alpaca API 역할 구분

### **Alpaca API (실시간 조회용 ✅)**
- **포지션:** `get_all_positions()`, `get_open_position(symbol)`
- **계좌:** `get_account()`
- **주문:** `submit_order()`, `get_orders()`

**장점:**
- 실시간 정확성
- 브로커 시스템과 동기화
- 페이퍼 트레이딩 지원

### **DB (기록 및 분석용 📝)**
**파일:** `app/domain/models/stock.py`

#### **Position 테이블**
```python
class Position(Base):
    __tablename__ = "positions"
    
    id: Mapped[int]
    symbol: Mapped[str]
    quantity: Mapped[int]
    entry_price: Mapped[float]
    current_price: Mapped[Optional[float]]
    entry_time: Mapped[datetime]
    exit_time: Mapped[Optional[datetime]]  # NULL = 활성 포지션
```

**용도:**
- 포지션 이력 분석
- 백테스트 비교
- 손익 통계

#### **PositionTracking 테이블**
```python
class PositionTracking(Base):
    __tablename__ = "position_tracking"
    
    id: Mapped[int]
    symbol: Mapped[str]
    entry_time: Mapped[datetime]
    entry_price: Mapped[float]
    exit_time: Mapped[Optional[datetime]]
    exit_price: Mapped[Optional[float]]
    quantity: Mapped[int]
    pnl: Mapped[Optional[float]]
```

**용도:**
- 일별 P&L 계산
- Kelly Criterion 학습
- 포트폴리오 최적화

---

## 🎯 4. 실제 사용 패턴

### **거래 실행 시 (trading_strategy_sync.py)**
```python
# 1. Alpaca에서 현재 상태 조회 ✅
account = self.api.get_account()
portfolio_value = float(account.portfolio_value)
buying_power = float(account.buying_power)

# 2. Alpaca에서 활성 포지션 조회 ✅
active_positions = self.portfolio_repo.get_all_active_positions()
# 내부적으로 DB 조회 → Alpaca API로 변경 필요 ⚠️
```

**문제점 발견:** `portfolio_repo.get_all_active_positions()`가 **DB 조회**

### **올바른 패턴 (수정 필요 ⚠️)**
```python
# Before (DB 조회 ❌)
active_positions = self.portfolio_repo.get_all_active_positions()

# After (Alpaca API ✅)
alpaca_positions = self.api.get_all_positions()
active_positions = [
    {
        'symbol': pos.symbol,
        'quantity': int(pos.qty),
        'entry_price': float(pos.avg_entry_price),
        'current_price': float(pos.current_price)
    }
    for pos in alpaca_positions
]
```

---

## 📋 5. 조회 API 엔드포인트 (필요 시 추가)

### **현재 포지션 조회 API** (구현 필요)
```python
# app/api/v1/endpoints/positions.py
@router.get("/positions")
async def get_current_positions():
    """
    현재 모든 활성 포지션 조회 (Alpaca API)
    
    Returns:
        - symbol: 종목 심볼
        - qty: 보유 수량
        - avg_entry_price: 평균 진입 가격
        - current_price: 현재 가격
        - market_value: 시가 총액
        - unrealized_pl: 미실현 손익
        - unrealized_plpc: 미실현 손익률
    """
    positions = trading_client.get_all_positions()
    return {
        "count": len(positions),
        "positions": [
            {
                "symbol": pos.symbol,
                "qty": int(pos.qty),
                "avg_entry_price": float(pos.avg_entry_price),
                "current_price": float(pos.current_price),
                "market_value": float(pos.market_value),
                "unrealized_pl": float(pos.unrealized_pl),
                "unrealized_plpc": float(pos.unrealized_plpc)
            }
            for pos in positions
        ]
    }
```

### **계좌 잔고 조회 API** (구현 필요)
```python
@router.get("/account")
async def get_account_info():
    """
    계좌 정보 조회 (Alpaca API)
    
    Returns:
        - portfolio_value: 전체 계좌 가치
        - cash: 현금 잔액
        - buying_power: 매수 가능 금액
        - equity: 순자산
        - profit_loss: 총 손익
        - profit_loss_pct: 손익률
    """
    account = trading_client.get_account()
    return {
        "portfolio_value": float(account.portfolio_value),
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "equity": float(account.equity),
        "last_equity": float(account.last_equity),
        "profit_loss": float(account.equity - account.last_equity),
        "profit_loss_pct": ((float(account.equity) / float(account.last_equity)) - 1) * 100
    }
```

---

## ✅ 결론

### **포지션 조회:**
- **실시간:** `api.get_all_positions()` ✅ Alpaca API
- **이력:** DB의 `PositionTracking` 테이블 📝

### **잔고 조회:**
- **실시간:** `api.get_account()` ✅ Alpaca API
- **이력:** DB 별도 저장 없음 (필요 시 추가)

### **수정 필요:**
- `portfolio_repo.get_all_active_positions()` → Alpaca API 직접 호출로 변경 ⚠️

### **추가 권장:**
- `/api/v1/positions` 엔드포인트 추가
- `/api/v1/account` 엔드포인트 추가
