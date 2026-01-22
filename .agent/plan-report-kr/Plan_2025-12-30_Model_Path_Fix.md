# 모델 지속성 및 경로 수정 계획 (KR)

## 목표
서버 환경(Docker)에서 `model_artifacts` 폴더와 모델 파일이 호스트에 정상적으로 유지되도록 경로와 권한을 수정합니다.

## 문제점
- **상대 경로 사용**: 현재 `model_artifacts/ensemble_model.pkl`과 같이 상대로 경로를 사용하여, 실행 위치에 따라 `/app/model_artifacts`가 아닌 엉뚱한 곳에 생성될 위험이 있습니다.
- **권한 문제**: 컨테이너가 root로 실행되어 생성된 파일에 호스트 사용자가 접근하지 못할 수 있습니다.

## 해결 방안

### 1. 절대 경로 사용
- `app/ml/predictor.py` 등 모든 모델 저장 경로를 `/app/model_artifacts/` 절대 경로로 변경하여 Docker 볼륨(`.:/app`)과 정확히 매핑되도록 합니다.

### 2. 권한 설정 (777)
- 코드 내에서 디렉토리 생성 시 `os.makedirs(path, mode=0o777, exist_ok=True)`를 사용하여 누구나 접근 가능하게 설정합니다. (사용자 요청 반영)

### 3. 앙상블 모델 저장 방식
- **질문**: 앙상블 결과만 저장? 개별 모델도 저장?
- **답변**: 현재 `VotingRegressor` 객체를 통째로 저장(`pickle/joblib`)하므로, 그 안에 개별 모델(CatBoost, LGBM, XGBoost)의 학습 상태가 모두 포함되어 있습니다. 즉, **파일 하나만 있어도 개별 모델 정보는 다 들어있습니다.**
- 다만, 디버깅 편의를 위해 개별 모델을 별도 파일로도 저장하는 로직을 추가하는 것을 고려할 수 있으나, 우선은 **파일 생성 자체**를 해결하는 데 집중합니다.

## 변경 파일
- `app/ml/predictor.py`: 경로를 `/app/model_artifacts/ensemble_model.pkl`로 수정하고 권한 설정 코드 추가.
- `app/ml/features.py`: 스케일러 저장 경로도 `/app/model_artifacts/feature_scaler.pkl`로 통일.

## 검증
- 학습 태스크 실행 후 서버의 `f:/Work/FastAPIStockTrader/model_artifacts/` 위치에 파일 생성 확인.
