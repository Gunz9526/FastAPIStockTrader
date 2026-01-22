# 앙상블 개선 및 Scaler 경로 수정 계획 (KR)

## 목표
1.  **Scaler 저장 오류 수정**: `feature_scaler.pkl`이 생성되지 않는 문제를 해결합니다 (절대 경로 적용).
2.  **앙상블 전략 고도화**: 단순 평균(Voting) 방식을 **가중 평균(Weighted Averaging)** 방식으로 개선합니다.

## 문제 분석
1.  **Scaler 누락**: `app/ml/features.py`에서 저장 경로가 상대 경로(`model_artifacts/...`)로 설정되어 있어, Docker 환경에서 저장 위치가 모호하거나 권한 문제로 실패했을 가능성이 있습니다.
2.  **현재 앙상블**: `VotingRegressor`는 모든 모델(CatBoost, XGB, LGBM)의 예측값을 1/N로 단순 평균합니다. 특정 모델이 더 잘 예측하더라도 이를 반영하지 못하는 한계가 있습니다.

## 해결 방안

### 1. 절대 경로 적용
- `app/ml/features.py`의 기본 경로를 `/app/model_artifacts/feature_scaler.pkl`로 변경하고 권한 설정 로직을 추가합니다.

### 2. 가중 앙상블 (Weighted Ensemble) 구현
- **개념**: 검증 세트(Validation Set)에서의 성능(예: Sharpe Ratio 또는 역 MSE)을 기반으로 각 모델에 가중치를 부여합니다.
    - 예: CatBoost 성능 0.6, XGB 성능 0.3, LGBM 성능 0.1 -> CatBoost 결과에 60% 비중.
- **구현**:
    - `EnsembleWrapper`를 수정하여 `weights` 파라미터를 받을 수 있게 합니다.
    - `app/tasks/training.py`에서 학습 시 각 모델의 검증 성능을 측정하고 최적 가중치를 계산하여 `EnsembleWrapper`에 전달합니다.

## 변경 파일
- `app/ml/features.py`: 경로 수정.
- `app/ml/models.py`: `EnsembleWrapper`에 가중치 로직 추가.
- `app/tasks/training.py`: 모델별 개별 학습 및 평가 후 가중치 도출 로직 추가.

## 검증
- 학습 태스크 실행.
- `feature_scaler.pkl` 파일 생성 확인.
- 로그에서 `Ensemble weights: [0.5, 0.3, 0.2]` 와 같은 가중치 계산 로그 확인.
