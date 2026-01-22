# 계획서: Phase F - Alpaca 15분봉 통합

## 1. 목표
Alpaca의 15분 단위 OHLCV 데이터를 활용하여 과거 데이터 백필 및 실시간 거래 시스템을 업그레이드합니다. 이는 데이터 입도를 높여 더욱 민첩한 대응 전략을 가능하게 하는 **Phase F (Advanced AI Capabilities)**의 시작점입니다.

## 2. 범위
*   **데이터베이스**: `StockOHLCV` 스키마에 타임프레임(15m vs 1d) 지원 추가.
*   **데이터 제공자**: `AlpacaDataProvider`를 수정하여 15분봉 데이터 수집.
*   **백필**: `backfill_ohlcv.py`를 수정하여 15분 데이터 적재.
*   **전략**: `FeatureEngineer` 및 `TradingStrategy`가 15분 주기에 맞춰 작동하도록 조정.
*   **실행**: 데이터 수집부터 DB 저장까지의 흐름 검증.

## 3. 상세 단계

### 3.1 데이터베이스 스키마 변경
*   **대상**: `app/domain/models/stock.py`
*   **작업**: `StockOHLCV`에 `timeframe` 컬럼 추가.
    *   타입: `String(10)` 또는 `Enum` (예: '1m', '15m', '1d').
    *   기본값: '1d' (기존 데이터 호환성 유지).
    *   Primary Key 업데이트: (`symbol`, `date_time`) -> (`symbol`, `date_time`, `timeframe`).
*   **제안**: 기존 일봉과 15분봉을 모두 지원하되, 실제 트레이딩은 15분봉을 기준으로 수행.

### 3.2 데이터 제공자 및 백필
*   **대상**: `app/services/data_provider.py`, `scripts/backfill_ohlcv.py`
*   **작업**:
    *   `get_historical_data` 메서드에 `timeframe` 인자 추가.
    *   15분을 Alpaca API의 `15Min`으로 매핑.
    *   `backfill_ohlcv.py` 실행 시 15분 데이터 요청 (최근 2년).
    *   *주의*: 15분봉 요청 시 API 호출 횟수 증가 예상. 페이지네이션 및 속도 제한 로직 강화.

### 3.3 피처 엔지니어링 및 학습
*   **대상**: `app/ml/feature_engineer.py`, `app/ml/train_models.py`
*   **작업**:
    *   TA-Lib 지표들이 15분 데이터에서도 정상 작동하는지 확인 (기간 설정 조정 필요 가능성).
    *   학습 스크립트가 DB에서 15분 데이터를 로드하도록 수정.

### 3.4 트레이딩 주기
*   **대상**: `app/core/scheduler.py` 또는 `main.py`
*   **작업**: 스케줄러 실행 주기를 '매일'에서 '매 15분'으로 변경.

## 4. 작업 분담 (Sub-Agents)
1.  **DB Agent**: `StockOHLCV` 모델 수정 및 마이그레이션 Script 생성.
2.  **Data Agent**: `AlpacaDataProvider` 및 `backfill_ohlcv.py` 수정.
3.  **Core Agent**: 피처 엔지니어링 및 학습 스크립트 검증.

## 5. 검증 계획
*   샘플 종목(예: AAPL)에 대해 1개월치 15분봉 백필 테스트.
*   DB에 `timeframe='15m'` 데이터가 정상 저장되었는지 확인.
*   모의 학습 주기를 실행하여 파이프라인 작동 확인.
