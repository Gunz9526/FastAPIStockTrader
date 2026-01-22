# 구현 계획 - Phase F.1: 백필 수정 및 RAG 인터페이스

## 목표 설명
1.  **15분봉 백필 수정**: 데이터 수집 실패(0건 저장) 원인을 파악하고 해결합니다.
2.  **RAG 인터페이스 (MSA)**: Backend가 직접 RAG를 수행하지 않고, 외부 RAG 서비스가 데이터를 조회할 수 있는 전용 API를 제공합니다.
3.  **감성 분석 (Sentiment)**: 미래의 AI 모델 고도화를 위해 `sentiment_score` 데이터를 관리할 수 있도록 스키마를 확장합니다.

## 변경 제안

### 1. 15분봉 백필 수정 (최우선 과제)
#### [MODIFY] [backfill_ohlcv.py](file:///f:/Work/FastAPIStockTrader/scripts/backfill_ohlcv.py)
- **문제**: 잘못된 `TimeFrame` 객체 사용 또는 빈 응답에 대한 예외 처리 미흡 추정.
- **수정**:
    - `TimeFrame(15, TimeFrameUnit.Minute)` 호출 로직 검증.
    - Alpaca API 응답에 대한 상세 디버그 로깅 추가.
    - **최적화**: Free Tier(IEX)가 15분봉을 지원하는지 확인 및 예외 처리.

### 2. 피처 엔지니어링: 감성 점수
#### [MODIFY] [app/domain/models/stock.py](file:///f:/Work/FastAPIStockTrader/app/domain/models/stock.py)
- `StockFundamentals` 테이블에 `sentiment_score` (Float) 컬럼 추가.
- 목적: 향후 뉴스/재무 기반의 "Smart Filtering" 구현 시 활용.

### 3. RAG 인터페이스 API (외부 서비스 지원)
#### [NEW] [app/api/v1/endpoints/rag.py](file:///f:/Work/FastAPIStockTrader/app/api/v1/endpoints/rag.py)
- **GET /rag/context/{symbol}**: 다음 정보를 JSON으로 반환
    - 최근 OHLCV 요약 (5일)
    - 주요 기술적 지표 (RSI, SMA 등)
    - 펀더멘털 및 감성 점수
- **GET /rag/portfolio**: 현재 보유 종목 및 성과 조회.

## 검증 계획
1.  **백필**: `python scripts/backfill_ohlcv.py` 실행 시 데이터 저장 성공 확인.
2.  **API**: `curl localhost:8000/api/v1/rag/context/AAPL` 호출 시 정상 JSON 응답 확인.
