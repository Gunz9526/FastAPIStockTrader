# 모델 학습 및 튜닝 가이드

## 📋 스케줄 관리

**위치**: `app/worker.py`

### Celery Beat 스케줄

```python
# 토요일 20:00 - 하이퍼파라미터 튜닝
"weekly_model_tuning": {
    "task": "app.tasks.training.tune_models",
    "schedule": crontab(minute="0", hour="20", day_of_week="6"),
}

# 일요일 22:00 - 모델 재학습
"weekly_model_training": {
    "task": "app.tasks.training.train_models",
    "schedule": crontab(minute="0", hour="22", day_of_week="0"),
}
```

### 스케줄 확인

```bash
# Celery Beat 로그 확인
docker-compose logs beat

# 등록된 스케줄 확인
docker-compose exec app celery -A app.worker inspect scheduled
```

### 스케줄 수동 변경

```python
# app/worker.py 수정 후
docker-compose restart beat
```

---

## 🎯 최적화 전략

### 1. 학습 데이터 (2년)

```python
# app/tasks/training.py
LOOKBACK_YEARS = 2  # 과적합 방지
VALIDATION_DAYS = 30  # 검증 데이터

# 실제 기간
start_date = now - 730일
validation_start = now - 30일
```

**이유**:
- 충분한 시장 사이클 포함
- 과적합 위험 최소화
- 다양한 국면 경험

### 2. 자동 복잡도 조정

```python
# app/ml/models.py
if n_samples < 1000:
    depth = 3, iterations = 100  # 단순
elif n_samples < 3000:
    depth = 6, iterations = 200  # 균형
else:
    depth = 8, iterations = 300  # 복잡
```

**장점**:
- 데이터 부족 시 과적합 방지
- 데이터 충분 시 성능 극대화

### 3. Optuna 튜닝

```python
# 탐색 공간
depth: 3-10
learning_rate: 0.01-0.3 (log scale)
iterations: 100-500
l2_leaf_reg: 1-10

# 평가
Time Series 5-Fold CV
Sharpe Ratio 최대화
100 trials (1시간 제한)
```

**결과 저장**:
```
model_artifacts/best_params.json
{
  "params": {...},
  "best_score": 1.85,
  "tuned_at": "2025-12-29T20:05:00"
}
```

---

## 🔧 수동 실행

### 튜닝 트리거

```bash
curl -X POST http://SERVER:8000/api/v1/operations/tune-models
```

### 학습 트리거

```bash
curl -X POST http://SERVER:8000/api/v1/operations/train-models
```

### 로그 확인

```bash
# 튜닝 로그
docker-compose logs app | grep "tuning"

# 학습 로그
docker-compose logs app | grep "training"
```

---

## 📊 성능 추적

### 최적 파라미터 확인

```bash
docker-compose exec app cat model_artifacts/best_params.json
```

### 모델 파일 확인

```bash
docker-compose exec app ls -lh model_artifacts/
# ensemble_model.pkl (현재 모델)
# best_params.json (최적 파라미터)
```

### 튜닝 히스토리

Optuna Study로 히스토리 추적 (선택사항):

```python
# SQLite study 저장 추가
study = optuna.create_study(
    storage='sqlite:///optuna_study.db',
    study_name='catboost_weekly'
)
```

---

## ⚡ 최적화 팁

### 1. 튜닝 시간 단축

```python
# app/tasks/training.py
study.optimize(
    objective,
    n_trials=50,  # 100 → 50 (30분)
    timeout=1800   # 1시간 → 30분
)
```

### 2. 데이터 샘플링

```python
# 대용량 데이터 시 샘플링
if len(X) > 10000:
    sample_idx = np.random.choice(len(X), 10000, replace=False)
    X = X.iloc[sample_idx]
    y = y.iloc[sample_idx]
```

### 3. Early Stopping

```python
# Optuna pruning
study = optuna.create_study(
    pruner=optuna.pruners.MedianPruner()
)
```

---

## 🚨 트러블슈팅

### 튜닝 실패

```bash
# 메모리 부족
docker-compose up -d --scale app=1 --build

# 타임아웃
# app/tasks/training.py에서 timeout 증가
```

### 스케줄 미실행

```bash
# Celery Beat 재시작
docker-compose restart beat

# 시간대 확인
docker-compose exec app date
# America/New_York (EST)
```

### 파라미터 적용 안됨

```python
# best_params.json 로드 확인
# train_models에서 로드 로직 추가
with open('model_artifacts/best_params.json') as f:
    params = json.load(f)['params']
    model = CatBoostWrapper(**params)
```

---

## 📅 주간 사이클

```
토요일 20:00 ─── Optuna 튜닝 시작
              │   - 100 trials
              │   - Sharpe Ratio 최대화
              └─→ best_params.json 저장

일요일 22:00 ─── 최적 파라미터로 재학습
              │   - 2년 데이터
              │   - 전체 종목
              └─→ ensemble_model.pkl 저장

월요일 09:30 ─── 최신 모델로 트레이딩 시작
```

---

## 다음 단계

1. **백필 실행**: `python scripts/backfill_ohlcv.py --years 2`
2. **첫 튜닝**: 수동 트리거 후 결과 확인
3. **모니터링**: 주간 성능 추적
4. **반복 개선**: 탐색 공간 조정
