# Celery 태스크 인식 문제 해결 계획

## 목표
`Received unregistered task` 에러를 해결하기 위해 Celery 워커 시작 시 모든 태스크 모듈을 강제로 임포트하도록 수정합니다.

## 문제 원인
`autodiscover_tasks`가 `app/tasks` 내의 개별 파일들(`training.py`, `trading.py` 등)을 제대로 찾지 못하여, 워커가 실행될 때 태스크가 등록되지 않는 문제가 발생했습니다.

## 해결 방안
`app/worker.py`의 `Celery` 생성자에 `include` 인자를 추가하여 태스크 모듈들을 명시적으로 지정합니다.

## 변경 파일: `app/worker.py`
- `Celery()` 생성자에 다음 모듈들을 `include`로 추가:
    - `"app.tasks.training"`
    - `"app.tasks.trading"`
    - `"app.tasks.market_analysis"`
    - `"app.tasks.data_tasks"`
- 중복된 `autodiscover_tasks` 호출 제거

## 검증
- 워커 재시작 후 로그 확인 또는 `celery inspect registered` 명령어로 등록된 태스크 확인
