# Celery 태스크 등록 리팩토링

## 목표
모든 태스크 모듈에서 `@shared_task`를 `@celery_app.task`로 교체하여 태스크 인식 문제를 해결하고 Celery 앱 인스턴스에 명시적으로 등록합니다.

## 배경
사용자는 `@shared_task`를 사용하는 태스크가 워커에서 인식되지 않는 문제를 보고했습니다. `@celery_app.task`로 전환하면 태스크를 인스턴스화된 애플리케이션에 직접 바인딩하여 발견 모호성을 피할 수 있습니다.

**`shared_task`를 사용했던 이유:**
주로 Django와 같이 셀러리 앱 인스턴스를 임포트하기 어렵거나 순환 참조를 피하기 위해 사용되는 패턴입니다. 하지만 현재 구조에서는 명시적 등록이 더 안정적입니다.

## 변경 사항

### 1. `app/tasks/training.py`
- [수정] `app.worker`에서 `celery_app` 임포트
- [수정] `@shared_task`를 `@celery_app.task`로 변경

### 2. `app/tasks/trading.py`
- [수정] `app.worker`에서 `celery_app` 임포트
- [수정] `@shared_task`를 `@celery_app.task`로 변경

### 3. `app/tasks/market_analysis.py`
- [수정] `app.worker`에서 `celery_app` 임포트
- [수정] `@shared_task`를 `@celery_app.task`로 변경

### 4. `app/tasks/data_tasks.py`
- [수정] `app.worker`에서 `celery_app` 임포트
- [수정] `@shared_task`를 `@celery_app.task`로 변경

## 검증 계획
- Celery 워커 재시작
- `celery -A app.worker inspect registered` 명령어로 모든 태스크 등록 확인
