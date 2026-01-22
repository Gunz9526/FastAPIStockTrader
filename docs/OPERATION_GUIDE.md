# 📚 FastAPI Stock Trader - 운영 가이드

## 1️⃣ analyze_market 데이터 활용

### **현재 상태: 로그 출력만 (미활용 ⚠️)**

**`analyze_market()` 수집 데이터:**
```python
{
    'total_symbols': 20,
    'analyzed_symbols': 18,
    'high_momentum': ['NVDA', 'TSLA', ...],  # 30일 내 2% 이상 상승
    'low_volatility': ['JNJ', 'PG', ...],    # 연간 변동성 15% 미만
    'high_volume': ['AAPL', 'MSFT', ...],    # 평균 거래량 10만주 이상
    'avg_return_pct': 1.2,
    'avg_volatility': 0.25
}
```

**현재 용도:**
- ✅ 로그로 출력 (모니터링용)
- ❌ **실제 거래 전략에 미반영**

---

### **잠재적 활용 방안 (구현 필요)**

#### **A. 포트폴리오 선택 필터링**
```python
# portfolio_rebalancer.py
def select_symbols_for_rebalance(self, market_analysis: dict):
    """
    시장 분석 결과를 기반으로 리밸런싱 종목 선정
    """
    candidates = []
    
    # 상승 모멘텀 + 저변동성 조합 선호
    high_momentum = set(market_analysis['high_momentum'])
    low_volatility = set(market_analysis['low_volatility'])
    
    # 안정적 상승 종목 우선
    stable_growth = high_momentum & low_volatility
    if stable_growth:
        candidates.extend(stable_growth)
    
    # 고거래량 종목 추가 (유동성 확보)
    candidates.extend(market_analysis['high_volume'][:3])
    
    return list(set(candidates))[:5]  # 최대 5개
```

#### **B. 동적 포지션 크기 조정**
```python
# trading_strategy_sync.py
def adjust_position_size_by_market(self, base_size: float, market_analysis: dict):
    """
    시장 평균 변동성에 따라 포지션 크기 조정
    """
    avg_volatility = market_analysis['avg_volatility']
    
    if avg_volatility > 0.3:  # 고변동성
        return base_size * 0.5  # 반으로 축소
    elif avg_volatility < 0.15:  # 저변동성
        return base_size * 1.2  # 20% 확대
    else:
        return base_size
```

#### **C. Circuit Breaker 조건**
```python
# risk_manager.py
def should_pause_trading(self, market_analysis: dict):
    """
    시장 전체 평균 수익률이 -5% 이하면 거래 중단
    """
    if market_analysis['avg_return_pct'] < -5:
        logger.warning("시장 전체 급락 감지, 거래 일시 중단")
        return True
    return False
```

---

### **통합 방안 (권장 구조)**

```python
# worker.py - Celery Beat 스케줄
{
    "task": "app.tasks.market_analysis.analyze_market",
    "schedule": crontab(hour=9, minute=0),  # 매일 09:00 (장 시작 전)
    "args": ()
}

# trading.py - 거래 전 시장 분석 확인
@celery_app.task
def execute_trading_scan():
    # 1. 시장 분석 결과 가져오기 (Redis 캐시 or DB)
    market_analysis = get_latest_market_analysis()
    
    # 2. Circuit Breaker 체크
    if should_pause_trading(market_analysis):
        return
    
    # 3. 종목 선택
    symbols = select_symbols_for_rebalance(market_analysis)
    
    # 4. 포지션 크기 조정
    for symbol in symbols:
        base_size = calculate_kelly_size(symbol)
        adjusted_size = adjust_position_size_by_market(base_size, market_analysis)
        # ...
```

**결론:** 현재는 로그 출력만 하므로 **실제 활용 코드 추가 필요** ⚠️

---

## 2️⃣ 모델 학습 순서

### **전체 Workflow**

```
┌─────────────────────────────────────────────────┐
│ Step 1: 데이터 수집 (backfill_ohlcv.py)        │
│         - SPY 포함 모든 활성 심볼 90일치       │
└─────────────────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────┐
│ Step 2: 모델 학습 (train_models)               │
│         - Regime별 모델 학습                   │
│         - 기본 하이퍼파라미터 사용              │
│         - model_artifacts/ 폴더에 저장          │
└─────────────────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────┐
│ Step 3: (선택) 하이퍼파라미터 튜닝 (tune_models)│
│         - Optuna로 최적 파라미터 탐색          │
│         - 1-2시간 소요                          │
│         - best_params.json 저장                 │
└─────────────────────────────────────────────────┘
                      ↓
┌─────────────────────────────────────────────────┐
│ Step 4: 재학습 (train_models)                  │
│         - best_params.json 적용                 │
│         - 최종 모델 배포                        │
└─────────────────────────────────────────────────┘
```

---

### **처음부터 다시 시작 (완전 초기화)**

#### **1. 모델 삭제**
```bash
# Docker 컨테이너 내부
docker exec -it fastapistocktrader-app-1 bash

# 모델 아티팩트 전체 삭제
rm -rf /app/model_artifacts/*

# 확인
ls -la /app/model_artifacts/
# (빈 디렉토리여야 함)
```

#### **2. 데이터 백필 (SPY 포함 필수)**
```bash
# SPY가 없으면 먼저 추가
python /app/scripts/add_symbols.py

# 90일치 15분봉 데이터 수집
python /app/scripts/backfill_ohlcv.py --days 90
```

**예상 시간:** 10-15분

**확인:**
```sql
-- PostgreSQL에서 확인
docker exec -it fastapistocktrader-postgres-1 psql -U stockuser -d stockdb

SELECT symbol, COUNT(*) as bar_count
FROM stock_ohlcv
WHERE symbol IN ('SPY', 'AAPL', 'MSFT')
GROUP BY symbol;

-- 결과 예시:
--  symbol | bar_count
-- --------+-----------
--  SPY    |      8736  (90일 × 26바/일 × 3.75시간)
--  AAPL   |      8520
--  MSFT   |      8640
```

#### **3. 첫 모델 학습**
```bash
# 방법 1: API 엔드포인트
curl -X POST http://localhost:8000/api/v1/operations/train-models

# 방법 2: Docker exec
docker exec fastapistocktrader-app-1 python -c "
from app.tasks.training import train_ml_ensemble
train_ml_ensemble.apply()
"
```

**예상 시간:** 5-10분

**확인:**
```bash
ls -la /app/model_artifacts/

# 생성된 파일:
# - ensemble_model.pkl
# - feature_scaler.pkl
# - ensemble_model_metadata.json
```

#### **4. (선택) 하이퍼파라미터 튜닝**
```bash
curl -X POST http://localhost:8000/api/v1/operations/tune-models
```

**예상 시간:** 1-2시간 ⏰

**결과:**
- `best_params.json` 생성
- 다음 학습부터 자동으로 적용

#### **5. 최종 재학습**
```bash
curl -X POST http://localhost:8000/api/v1/operations/train-models
```

**이때 `best_params.json` 적용됨** ✅

---

### **정답: Tune 전에 먼저 학습 필요 ❌**

**이유:**
- `tune_models()`는 **독립적으로** 데이터를 로드하여 최적 파라미터만 탐색
- 기존 모델 파일을 읽지 않음
- `train_models()`가 `best_params.json`을 읽어서 적용

**권장 순서:**
1. **즉시 학습:** 기본 파라미터로 모델 생성 → 바로 거래 가능
2. **병렬 튜닝:** 백그라운드에서 `tune_models()` 실행
3. **재학습:** 튜닝 완료 후 `train_models()` 다시 실행 → 최적 모델 배포

---

## 3️⃣ 레짐 경고 원인 (Reasoning)

### **문제:**
```
[2026-01-08 09:30:13] WARNING: 레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정
```

### **근본 원인: 임계값 불일치**

**training.py (Line 187):**
```python
if len(spy_window) >= 20:  # ❌ 20개 바 체크
    regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
```

**regime.py (Line 44):**
```python
if df.empty or len(df) < 50:  # ❌ 50개 바 체크
    logger.warning("레짐 감지용 데이터 부족, SIDEWAYS_CALM으로 기본 설정")
```

**발생 구간:**
- SPY 타임스탬프 20~49번: `len(spy_window) = 20~49`
- `detect_regime()` 호출 → `len(df) < 50` → **경고 30회 발생** ⚠️

---

### **해결: 임계값 통일**

**수정 완료:**
```python
# training.py Line 187
if len(spy_window) >= 50:  # ✅ 50개 바 확보 시에만 레짐 감지
    regime = regime_detector.detect_regime(spy_window, vix_value=vix_value)
```

**효과:**
- 처음 49개 타임스탬프: `SIDEWAYS_CALM` 직접 할당 (호출 안 함)
- 50번째부터: 정상 레짐 감지
- **경고 0회** ✅

---

## 📝 요약

| 질문 | 답변 | 현재 상태 |
|------|------|----------|
| 1. analyze_market 활용 | 로그 출력만, 거래 전략 미반영 | ⚠️ 추가 구현 필요 |
| 2. 학습 순서 | Tune 전 학습 불필요, 독립 실행 | ✅ 즉시 학습 가능 |
| 3. 레짐 경고 원인 | 임계값 불일치 (20 vs 50) | ✅ 수정 완료 |

**다음 단계:**
1. Docker 재시작: `docker-compose restart`
2. 로그 확인: 경고 0회 예상
3. 첫 모델 학습: `curl -X POST http://localhost:8000/api/v1/operations/train-models`
