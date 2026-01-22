# 구현 계획 - Phase F 모델 튜닝 및 버그 수정

## 목표 설명
모델 학습 시 발생하는 `NameError: name 'strategy_returns' is not defined` 크리티컬 버그를 수정하고, LightGBM/CatBoost/XGBoost 3개 모델 모두에 대해 안정적인 하이퍼파라미터 튜닝 범위를 적용합니다.

## 변경 제안

### 1. 크리티컬 버그 수정 (app/tasks/training.py)
#### [MODIFY] [app/tasks/training.py](file:///f:/Work/FastAPIStockTrader/app/tasks/training.py)
- **버그 수정**: `train_models` 루프 내에서 `strategy_returns` 변수가 정의되지 않고 사용되는 문제 해결.
    - Sharpe Ratio 계산 직전에 `predictions`와 `y_val`을 이용하여 수익률 벡터를 계산하는 로직 추가.
    - `pred_dir = (predictions > 0) ...` -> `strategy_returns = ...`

### 2. 하이퍼파라미터 튜닝 최적화 (모든 모델)
#### [MODIFY] [app/tasks/training.py](file:///f:/Work/FastAPIStockTrader/app/tasks/training.py)
- **LightGBM**:
    - `learning_rate` (Max 0.1), `num_leaves` (Max 50) 등 제약 강화.
    - `min_child_samples` 추가로 노이즈 과적합 방지.
- **CatBoost**:
    - `learning_rate` 범위 축소 (0.01~0.1).
    - `depth` 제한 (4~8)으로 메모리/속도 최적화.
- **XGBoost**:
    - `max_depth` (3~6) 제한.
    - `min_child_weight` 추가.

### 3. 모델 래퍼 개선
#### [MODIFY] [app/ml/models.py](file:///f:/Work/FastAPIStockTrader/app/ml/models.py)
- **공통**: 각 모델별로 추가된 파라미터(`min_child_...`)를 `__init__`에서 잘 받아 전달하도록 확인.

## 검증 계획

### 자동화 테스트
1.  **튜닝 스크립트 실행**:
    - `python scripts/run_tuning.py` 실행.
    - **성공 기준**:
        - `NameError` 발생 없음.
        - LightGBM Warning (`No further splits`) 발생 없음.
        - `model_artifacts/best_params.json`에 3개 모델 파라미터 모두 정상 저장됨.

### 수동 검증
- 로그에서 각 모델별 Sharpe Ratio가 정상적으로 출력되는지 확인 (`0.0`이나 `infinity`가 아닌 유효한 숫자).
