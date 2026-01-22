# CatBoost-ScikitLearn 호환성 수정 계획

## 목표
`AttributeError: 'CatBoostRegressor' object has no attribute '__sklearn_tags__'` 에러를 해결하기 위해 패키지 버전을 조정합니다.

## 문제 원인
현재 `scikit-learn` **1.7.1**과 `catboost` **1.2.8**이 설치되어 있습니다. `scikit-learn` 1.6 버전부터 새로운 Tags API(`__sklearn_tags__`)가 도입되었는데, CatBoost 1.2.8은 이를 지원하지 않아 `VotingRegressor` 등에서 호환성 문제가 발생합니다.

## 해결 방안
`scikit-learn` 버전을 CatBoost 1.2.x와 호환되는 **1.6 미만** 버전(예: **1.5.2**)으로 다운그레이드합니다.

## 변경 파일: `requirements.txt`
- `scikit-learn` 버전을 `1.5.2`로 고정

## 실행 및 검증
- `requirements.txt` 수정
- Docker 이미지 재빌드 (`docker-compose build`)
- 컨테이너 재시작
- 학습 태스크 재실행하여 정상 완료 확인
