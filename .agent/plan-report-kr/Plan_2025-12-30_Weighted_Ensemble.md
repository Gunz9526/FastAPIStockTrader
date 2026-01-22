# 가중 앙상블 및 모델 평가 API 구현 계획 (KR)

## ✅ HANDOVER.md 인지 확인
**확인 완료**. 다음 핵심 사항을 숙지했습니다:
- Python 3.14, Docker 기반 서버 환경
- Phase D (ML Training) 완료 상태
- Alpaca IEX 피드 사용 (무료 플랜)
- TimescaleDB + Redis + Celery 스택
- 다음 목표: Phase E (Production Hardening)

## 목표
1.  `feature_scaler.pkl` 저장 오류 수정
2.  **가중 앙상블** 구현 (검증 성능 기반 가중치)
3.  **모델 성능 평가 API** 생성

## 구현 계획

### 1단계: Feature Scaler 경로 수정
**파일**: `app/ml/features.py`
- 상대 경로 → 절대 경로 `/app/model_artifacts/feature_scaler.pkl`
- 권한 설정 `0o777` 적용

### 2단계: 가중 앙상블 구현
**파일**: `app/ml/models.py`
- `EnsembleWrapper`에 `weights` 파라미터 추가
- `VotingRegressor(estimators, weights=weights)` 사용

**파일**: `app/tasks/training.py`
- 각 모델(CatBoost, XGBoost, LGBM)을 검증 세트에서 개별 평가
- Sharpe Ratio 또는 역 MSE 계산
- 정규화하여 가중치 도출 (합이 1.0)
- 예: `[0.5, 0.3, 0.2]` → CatBoost 50%, XGBoost 30%, LGBM 20%

### 3단계: 모델 평가 API 생성
**신규 파일**: `app/api/v1/endpoints/model.py`

**엔드포인트 1**: `GET /api/v1/model/metrics`
- 응답:
```json
{
  "ensemble_weights": [0.5, 0.3, 0.2],
  "last_training_date": "2025-12-30T02:00:00",
  "validation_metrics": {
    "sharpe_ratio": 1.8,
    "win_rate": 0.61,
    "individual_models": {
      "catboost": {"sharpe": 2.0, "mse": 0.0012},
      "xgboost": {"sharpe": 1.7, "mse": 0.0015},
      "lgbm": {"sharpe": 1.5, "mse": 0.0018}
    }
  }
}
```

**엔드포인트 2**: `GET /api/v1/model/predict`
- 요청: `{"symbol": "AAPL"}` 또는 `{"features": {...}}`
- 응답: `{"prediction": 0.0023, "model_version": "2025-12-30"}`

### 4단계: 메타데이터 저장
**파일**: `model_artifacts/metadata.json` (새로 생성)
- 학습 시 자동 생성
- 내용: 가중치, 학습일자, 각 모델 metrics

## 검증 계획

### 수동 검증 (사용자가 서버에서 직접 실행)
1.  **Scaler 생성 확인**:
    ```bash
    # 학습 후 서버에서
    ls -la model_artifacts/feature_scaler.pkl
    ```

2.  **가중치 로그 확인**:
    ```bash
    docker-compose logs worker | grep "Ensemble weights"
    ```

3.  **API 테스트 - Metrics**:
    ```bash
    curl http://localhost:8000/api/v1/model/metrics
    ```

4.  **API 테스트 - Prediction**:
    ```bash
    curl -X GET "http://localhost:8000/api/v1/model/predict?symbol=AAPL"
    ```

## 다음 진행 단계 (승인 후)
1.  Feature scaler 경로 수정 ✅
2.  `EnsembleWrapper` 가중치 로직 추가 ✅
3.  `train_models`에서 성능 평가 및 가중치 계산 ✅
4.  `/api/v1/model/metrics` 엔드포인트 생성 ✅
5.  `/api/v1/model/predict` 엔드포인트 생성 ✅
6.  메타데이터 저장 로직 추가 ✅
7.  수동 검증 및 로그 확인

**예상 소요 시간**: 각 단계 5-10분, 총 40-60분
