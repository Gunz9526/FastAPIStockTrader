# 학습 태스크 날짜 비교 오류 수정 계획

## 목표
`app/tasks/training.py`에서 발생하는 `Invalid comparison between dtype=datetime64[ns, UTC] and datetime` 에러를 수정합니다.

## 문제 원인
`features_df.index`는 UTC 타임존 정보를 가진 pandas Timestamp인데 반해, `val_start` 등 기준 날짜 변수들은 타임존 정보가 없는 Python의 기본 `datetime` 객체입니다. 이 둘을 비교하려 할 때 호환되지 않아 에러가 발생합니다.

## 해결 방안
`val_start`와 기준 날짜들을 pandas Timestamp로 변환하고 명시적으로 UTC 타임존을 설정하여 비교가 가능하도록 수정합니다.

## 변경 파일: `app/tasks/training.py`
- `train_models` 함수:
    - `datetime.now()` 대신 `pd.Timestamp.now(tz='UTC')` 또는 `datetime.now(timezone.utc)` 사용
    - `val_start`를 생성할 때 타임존 정보 포함

## 검증
- 학습 태스크 실행 트리거
- 워커 로그에서 에러 사라짐 확인
