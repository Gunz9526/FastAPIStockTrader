# 작업 보고서: 비즈니스 로직 및 ML 구현

**날짜**: 2025-12-29
**작업**: ML 엔진 및 스케줄링 구현

## 요약
트리 계열 모델(CatBoost, LightGBM, XGBoost)과 앙상블 래퍼를 사용하여 CPU에 최적화된 머신러닝 엔진을 구현했습니다. Celery를 도입하여 장중 자동 매매와 장 마감 후 모델 재학습 스케줄링을 설정했습니다. KisDataProvider를 제거하고 Alpaca 단독 체제로 전환했습니다.

## 변경 내역
### 머신러닝 (`app/ml/`)
- `models.py`: CatBoost, LGBM, XGBoost, VotingRegressor 래퍼 클래스.
- `predictor.py`: 모델 로딩 및 초저지연 추론을 위한 싱글톤 서비스.
- `trainer.py`: Celery Task(`tasks/training.py`)에 통합하여 구현.

### 스케줄링 (`app/worker.py`, `app/tasks/`)
- `worker.py`: Celery 앱 및 Beat 스케줄 설정 (시장 감시: 09-16 EST, 재학습: 18 EST).
- `tasks/trading.py`: 전략 엔진과 연동된 비동기 시장 감시 태스크.
- `tasks/training.py`: Optuna 하이퍼파라미터 튜닝을 포함한 모델 재학습 태스크.

### 리팩토링
- `app/services/data_provider.py`: Alpaca 전용 로직으로 재작성.
- `pyproject.toml` / `requirements.txt`: ML 라이브러리 추가.

## 상태
- **ML**: 완료 (트리 모델, CPU 최적화).
- **스케줄링**: 완료 (Celery Beat).
- **데이터**: Alpaca 단독 사용.
