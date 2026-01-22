# 작업 보고서: Phase F - 15분봉 통합

## 📋 실행 요약
**작업**: 고빈도 전략 준비를 위한 백엔드 전반의 15분 타임프레임 지원 추가.
**일자**: 2025-12-30
**상태**: ✅ 완료

## 🛠️ 기술적 변경 사항
### 1. 데이터베이스 스키마
- **파일**: `app/domain/models/stock.py`
- **변경**: `StockOHLCV` 테이블에 `timeframe` 컬럼 추가.
- **제약조건**: 기본키(PK)를 (`symbol`, `date_time`, `timeframe`) 복합키로 변경.
- **마이그레이션**: 테이블 재생성을 위한 `scripts/migrate_db_for_15m.py` 스크립트 작성 (서버 실행 필요).

### 2. 데이터 제공자
- **파일**: `app/services/data_provider.py`
- **변경**: `get_historical_data` 메서드가 `timeframe` 인자를 받도록 수정.
- **라이브러리**: `alpaca-py`의 `TimeFrame(15, TimeFrameUnit.Minute)` 객체 사용으로 변경.

### 3. 백필 및 저장소
- **파일**: `scripts/backfill_ohlcv.py`
- **변경**: 기본적으로 최근 2년치 15분봉 데이터를 수집하도록 설정.
- **파일**: `app/repositories/stock_repo_sync.py`
- **변경**: `get_ohlcv_range` 호출 시 `timeframe='15m'` 데이터를 필터링하도록 수정.

## 🔍 검증 절차 (수동 필요)
로컬 환경 제약으로 인해 서버에서 직접 다음 명령어를 실행해야 합니다:
1.  **서버**: `python scripts/migrate_db_for_15m.py` 실행하여 테이블 스키마 적용 (기존 데이터 삭제됨).
2.  **서버**: `python scripts/backfill_ohlcv.py` 실행하여 15분봉 데이터 적재.
3.  **확인**: DB 접속 후 `stock_ohlcv` 테이블에 `timeframe='15m'` 레코드가 생성되었는지 확인.

## 📝 다음 단계
- Phase F.1 (RAG 파이프라인) 또는 F.2 (펀더멘털 데이터 통합) 진행.
