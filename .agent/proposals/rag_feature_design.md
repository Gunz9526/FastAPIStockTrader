# RAG (Retrieval-Augmented Generation) 기능 설계

## 📊 개요
trade_logs와 portfolio_status 테이블을 활용한 AI 기반 투자 인사이트 시스템

---

## 🎯 1. 핵심 기능

### 1.1 거래 패턴 분석 (Trade Logs 기반)
**목적**: 과거 거래 데이터로부터 성공/실패 패턴 학습

**기능**:
- **시간대별 성공률 분석**
  - "15:00~15:30 거래가 승률 65%로 가장 높습니다"
  - "09:30~10:00 거래는 변동성이 높아 승률 45%입니다"

- **전략별 성과 분석**
  - "모멘텀 전략: 승률 58%, 평균 수익 +2.3%"
  - "평균회귀 전략: 승률 52%, 평균 수익 +1.1%"
  
- **시장 레짐별 성과**
  - "Bull Trending: 60% 승률, Sharpe 1.5"
  - "Bear Trending: 40% 승률, Sharpe 0.8"

**구현 예시**:
```python
# RAG 쿼리: "최근 1달 거래 중 가장 수익률 높은 전략은?"
SELECT strategy_name, 
       AVG(realized_pl) as avg_pnl,
       COUNT(*) as trade_count,
       SUM(CASE WHEN realized_pl > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) as win_rate
FROM trade_logs
WHERE execution_time > NOW() - INTERVAL '30 days'
GROUP BY strategy_name
ORDER BY avg_pnl DESC;
```

---

### 1.2 포트폴리오 건강도 진단 (Portfolio Status 기반)
**목적**: 현재 포트폴리오 상태 진단 및 리밸런싱 제안

**기능**:
- **집중도 분석**
  - "AAPL 비중 40% - 과도한 집중 위험 (권장: 30% 이하)"
  - "섹터 다각화: Technology 70%, Finance 30% (불균형)"

- **미실현 손익 알림**
  - "GOOGL: -5% 손실 중 → Stop Loss 고려 필요"
  - "TSLA: +12% 수익 → Trailing Stop 설정 권장"

- **리밸런싱 제안**
  - "현재 상관계수 0.85 → NVDA 일부 매도, MSFT 매수 권장"
  - "VaR 초과: 일일 위험 $2,500 (한도 $2,000) → 포지션 축소 필요"

**구현 예시**:
```python
# RAG 쿼리: "현재 포트폴리오 위험도가 높나요?"
SELECT symbol, 
       (current_price - avg_price) / avg_price * 100 as unrealized_pnl_pct,
       quantity * current_price as market_value
FROM portfolio_status
WHERE user_id = 'trader_001'
ORDER BY ABS(unrealized_pnl_pct) DESC;
```

---

### 1.3 자연어 질문-답변 (Gemini AI 통합)
**목적**: 사용자가 자연어로 거래 이력 질의

**질문 예시**:
- "지난주에 AAPL 몇 번 거래했어?"
- "이번 달 수익률이 가장 높은 종목은?"
- "손실 거래가 반복되는 시간대는?"
- "Bear 시장에서 어떤 전략이 효과적이었어?"

**RAG 프로세스**:
1. **자연어 → SQL 변환** (Gemini AI)
   ```
   질문: "지난주 AAPL 거래 횟수는?"
   → SQL: SELECT COUNT(*) FROM trade_logs 
           WHERE symbol = 'AAPL' 
           AND execution_time > NOW() - INTERVAL '7 days'
   ```

2. **DB 쿼리 실행** → 결과 획득

3. **결과 → 자연어 응답** (Gemini AI)
   ```
   DB 결과: [{"count": 3}]
   → 답변: "지난주 AAPL을 3번 거래했습니다. 
            평균 수익률은 +1.8%였습니다."
   ```

---

## 🛠️ 2. 구현 구조

### 2.1 API 엔드포인트
```python
# app/api/v1/endpoints/rag.py (확장)

@router.post("/chat")
async def rag_chat(
    question: str,
    user_id: str = "trader_001",
    db: AsyncSession = Depends(get_async_session)
):
    """
    자연어 질문에 대한 AI 기반 답변 생성
    
    Process:
    1. 질문 분석 (Gemini)
    2. SQL 생성
    3. DB 쿼리
    4. 답변 생성 (Gemini)
    """
    # 1. Gemini로 SQL 생성
    sql_query = await generate_sql_from_question(question)
    
    # 2. DB 쿼리 실행
    result = await db.execute(text(sql_query))
    data = result.fetchall()
    
    # 3. Gemini로 자연어 답변 생성
    answer = await generate_answer_from_data(question, data)
    
    return {"question": question, "answer": answer, "data": data}
```

### 2.2 Gemini Prompt 템플릿
```python
# app/services/rag_service.py

SYSTEM_PROMPT = """
당신은 주식 거래 전문가 AI입니다.
아래 스키마를 기반으로 SQL 쿼리를 생성하세요.

테이블:
- trade_logs: 거래 이력 (symbol, action, price, quantity, realized_pl, execution_time, strategy_name)
- portfolio_status: 포트폴리오 현황 (symbol, avg_price, quantity, current_price)
- position_tracking: 포지션 추적 (entry_price, exit_price, regime)

제약사항:
- PostgreSQL 문법 사용
- 날짜 필터는 NOW() - INTERVAL 사용
- 결과는 최대 50개로 제한
"""

USER_PROMPT = f"""
질문: {question}

SQL 쿼리만 반환하세요 (코드 블록 없이 순수 SQL만)
"""
```

---

## 📈 3. 활용 시나리오

### 시나리오 1: 거래 후 회고
```
사용자: "오늘 거래 결과 어땠어?"

RAG:
- trade_logs에서 오늘 거래 조회
- 총 거래: 5건, 승률: 60%, 총 손익: +$120
- 최고 수익: AAPL (+$80), 최대 손실: TSLA (-$30)
- 제안: "TSLA는 3일 연속 손실 - Stop Loss 재검토 필요"
```

### 시나리오 2: 전략 최적화
```
사용자: "모멘텀 전략 성과가 안 좋은데 원인이 뭐야?"

RAG:
- trade_logs에서 strategy_name='MomentumStrategy' 필터
- 승률: 45% (평균 52% 대비 낮음)
- 시간 분석: 09:30~10:00 거래가 80% 실패
- 제안: "시장 개장 직후 변동성 높은 시간대 회피 권장"
```

### 시나리오 3: 리스크 알림
```
사용자: "현재 포트폴리오 위험도는?"

RAG:
- portfolio_status에서 미실현 손익 계산
- GOOGL: -8% (Stop Loss 임계값 -5% 초과)
- 섹터 집중도: Technology 85% (위험)
- VaR: $3,200 (한도 $2,000 초과)
- 제안: "GOOGL 익절 후 Finance 섹터 다각화 권장"
```

---

## 🔧 4. 기술 스택

| 구성 요소 | 기술 | 역할 |
|----------|------|------|
| **자연어 처리** | Gemini 1.5 Pro | 질문 이해, SQL 생성, 답변 생성 |
| **데이터 저장** | PostgreSQL | trade_logs, portfolio_status |
| **벡터 검색** | pgvector (선택) | 유사 거래 패턴 검색 |
| **캐싱** | Redis | 자주 묻는 질문 결과 캐싱 |
| **API** | FastAPI | RAG 엔드포인트 제공 |

---

## 🚀 5. 구현 우선순위

### Phase 1: 기본 쿼리 응답 (1주)
- ✅ trade_logs 기반 거래 통계 (승률, 평균 손익)
- ✅ portfolio_status 기반 포트폴리오 현황
- ✅ 정해진 SQL 템플릿 사용 (Gemini 없이)

### Phase 2: Gemini 통합 (2주)
- 🔄 자연어 → SQL 변환
- 🔄 데이터 → 자연어 답변 생성
- 🔄 프롬프트 엔지니어링 최적화

### Phase 3: 고급 분석 (3주)
- 🔜 거래 패턴 클러스터링 (K-Means)
- 🔜 시간대별 성과 히트맵
- 🔜 리밸런싱 자동 제안

### Phase 4: 대화형 인터페이스 (4주)
- 🔜 Multi-turn 대화 (컨텍스트 유지)
- 🔜 시각화 자동 생성 (차트)
- 🔜 Discord/Slack 봇 통합

---

## 📊 6. 예상 효과

| 항목 | Before (수동) | After (RAG) | 개선율 |
|------|---------------|-------------|--------|
| 거래 회고 시간 | 30분 | 5분 | **83% 단축** |
| 패턴 발견 정확도 | 50% | 80% | **60% 향상** |
| 리밸런싱 주기 | 월 1회 | 주 1회 | **4배 증가** |
| 전략 최적화 반복 | 월 1회 | 주 2회 | **8배 증가** |

---

## ⚠️ 7. 주의사항

1. **SQL Injection 방지**
   - Gemini가 생성한 SQL을 직접 실행하지 말 것
   - Whitelist 검증 (허용된 테이블/컬럼만)
   - Prepared Statement 사용

2. **데이터 프라이버시**
   - trade_logs에 민감한 계좌 번호 저장 금지
   - user_id로 격리

3. **비용 관리**
   - Gemini API 호출 최소화 (캐싱 활용)
   - 무료 Tier: 60 calls/min (충분)

---

## 📝 8. 구현 체크리스트

- [ ] trade_logs 자동 기록 (거래 발생 시 트리거)
- [ ] portfolio_status 실시간 업데이트 (Alpaca API 동기화)
- [ ] RAG 엔드포인트 구현 (`POST /v1/rag/chat`)
- [ ] Gemini Prompt 템플릿 작성
- [ ] SQL Whitelist 검증 로직
- [ ] Redis 캐싱 (자주 묻는 질문)
- [ ] 프론트엔드 챗봇 UI (선택)
- [ ] 단위 테스트 (SQL 생성 검증)

---

**작성일**: 2026-01-15  
**버전**: v1.0 (설계안)
