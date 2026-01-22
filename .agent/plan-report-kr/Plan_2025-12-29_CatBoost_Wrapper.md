# CatBoost-ScikitLearn 래퍼 구현 계획

## 목표
`scikit-learn` 버전을 다운그레이드하지 않고, `__sklearn_tags__` API를 지원하는 커스텀 CatBoost 래퍼를 구현하여 호환성 문제를 해결합니다.

## 해결 방안
`CatBoostRegressor`를 상속받는 `CompatibleCatBoostRegressor` 클래스를 정의하고, 이 클래스에서 `__sklearn_tags__` 메서드를 구현하여 `scikit-learn` 1.6+의 요구사항을 충족시킵니다.

## 변경 파일: `app/ml/models.py`
- `sklearn.utils._tags`에서 `InputTags`, `TargetTags`, `Tags` 등을 임포트 (버전에 따라 다를 수 있으므로 안전하게 `BaseEstimator` 활용)
- `CompatibleCatBoostRegressor` 클래스 정의:
    - 상속: `CatBoostRegressor`, `BaseEstimator`, `RegressorMixin`
    - `__sklearn_tags__` 구현: 회귀 모델임을 명시하는 태그 반환
- `EnsembleWrapper`에서 `CompatibleCatBoostRegressor` 사용하도록 수정

## 실행 계획
1. `requirements.txt`의 `scikit-learn` 버전 고정 해제 (원복)
2. `app/ml/models.py` 수정
3. Docker 이미지 재빌드 및 태스크 실행 검증

## 검증
- 학습 태스크 재실행 시 `AttributeError` 없이 완료되어야 함
