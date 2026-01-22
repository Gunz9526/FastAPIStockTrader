# 계획서: 15분봉 학습 크리티컬 버그 수정 & 검증

**작성일**: 2026-01-03  
**로드맵 연계**: Phase D (ML Core 디버깅) + Phase F.1 (15분봉 통합 안정화)  
**프로젝트**: FastAPIStockTrader

---

## 1. 문제 정의

### 1.1 크리티컬 버그
**에러**: `NameError: name 'strategy_returns' is not defined`  
**위치**: `app/tasks/training.py` 149번째 줄  
**영향도**: 백필 후 모델 학습 완전 실패

**코드 분석**:
```python
# Line 149 (오류 발생 지점)
sharpe = strategy_returns.mean() / (strategy_returns.std() + 1e-8) * (bars_per_year ** 0.5)

# 문제: 'strategy_returns' 변수가 정의되지 않음!
# tune_models에서는 'returns'로 명명 (Line 282), train_models에서는 정의 없이 사용
```

### 1.2 부차적 이슈
**LightGBM 경고**: `No further splits with positive gain`
- **크리티컬 버그 아님** (모델은 정상 학습됨)
- **가능한 원인**:
  - 15분봉 전환 후 검증 세트 크기 부족
  - 피처 간 다중공선성 (중복 피처)
  - Early Stopping 과도하게 공격적

---

## 2. 근본 원인 분석

### 2.1 변수명 불일치
| 함수명          | 라인 | 변수명           | 상태 |
|----------------|------|-----------------|------|
| `tune_models`  | 282  | `returns`       | ✅ 정상 |
| `tune_models`  | 326  | `returns`       | ✅ 정상 |
| `tune_models`  | 370  | `ens_rets`      | ✅ 정상 |
| `train_models` | 149  | `strategy_returns` | ❌ 미정의 |

**불일치 패턴**: 리팩토링 중 복사-붙여넣기 오류로 추정.

### 2.2 Optuna 통합 (정상 작동 확인)
✅ **튜닝 프로세스**:
1. `tune_models` → Optuna로 최적 파라미터 탐색 (모델당 30회 시행)
2. `model_artifacts/best_params.json`에 저장
3. `train_models` → 파라미터 자동 로드 (Line 124-133)
4. 로드된 파라미터로 최종 학습 (Line 139-141)

**Optuna 로직은 수정 불필요.**

---

## 3. 해결 방안 설계

### 3.1 수정 전략 (최소 침습적 접근)
**원칙**: 버그만 수정. 정상 작동하는 Optuna 로직은 건드리지 않음.

**변경 범위**:
- 파일: `app/tasks/training.py`
- 라인: 145-150 (6줄)
- 작업: `strategy_returns` 계산 로직 추가

**수정 전 (Line 145-150)**:
```python
                predictions = model.predict(X_val_scaled)
                
                # Sharpe Ratio (Adjusted for 15m bars: 26 bars/day * 252 days)
                bars_per_year = 252 * 26
                sharpe = strategy_returns.mean() / (strategy_returns.std() + 1e-8) * (bars_per_year ** 0.5)
                sharpe_ratios.append(max(sharpe, 0.1))
```

**수정 후**:
```python
                predictions = model.predict(X_val_scaled)
                
                # Calculate strategy returns
                pred_dir = (predictions > 0).astype(int) * 2 - 1  # -1 or +1
                strategy_returns = y_val.values * pred_dir
                
                # Sharpe Ratio (Adjusted for 15m bars: 26 bars/day * 252 days)
                bars_per_year = 252 * 26
                sharpe = strategy_returns.mean() / (strategy_returns.std() + 1e-8) * (bars_per_year ** 0.5)
                sharpe_ratios.append(max(sharpe, 0.1))
```

### 3.2 LightGBM 경고 완화 (선택사항)
**접근법**: 로깅 + 방어 체크 추가, 파라미터 변경 없음.

**근거**:
- Optuna가 자동으로 최적 파라미터 탐색
- 경고가 학습을 막지는 않음 (모델 정상 학습됨)
- 모니터링 후 Optuna가 자동 적응하도록 함

**조치**:
- 학습 전 데이터 검증 추가:
  ```python
  if len(X_train) < 500:
      logger.warning(f"소규모 학습 세트: {len(X_train)} 샘플. 백필 기간 연장 권장.")
  ```

---

## 4. 검증 계획

### 4.1 유닛 테스트 (오프라인)
**시나리오**: 1000행 15분봉 데이터 모킹
```python
# tests/test_training_15min.py
def test_train_models_with_mock_15min_data():
    # 1000행 생성 (약 1주일치 15분 봉)
    # train_models 태스크 실행
    # 단언: NameError 없음
    # 단언: 모델 정상 저장됨
```

### 4.2 통합 테스트 (서버 실행)
**단계**:
1. 백필 실행: `celery -A app.worker call app.tasks.data_tasks.backfill_ohlcv --args='["AAPL"]'`
2. 튜닝 실행: `celery -A app.worker call app.tasks.training.tune_models`
3. 학습 실행: `celery -A app.worker call app.tasks.training.train_models`
4. **성공 기준**:
   - `strategy_returns` 에러 없음
   - `best_params.json` 생성 확인
   - `model_artifacts/`에 모델 저장 확인
   - 로그에 Sharpe + F1 스코어 출력 확인

---

## 5. 실행 체크리스트

### Phase 1: 코드 수정
- [ ] 현재 `app/tasks/training.py` 백업
- [ ] `strategy_returns` 수정 적용 (Line 147-148)
- [ ] 데이터 크기 경고 추가 (Line 110)
- [ ] 커밋: `git commit -m "fix: strategy_returns 계산 로직 추가"`

### Phase 2: 테스트
- [ ] 유닛 테스트 파일 생성
- [ ] 로컬 pytest 실행
- [ ] 서버 배포
- [ ] 통합 테스트 실행 (백필 → 튜닝 → 학습)

### Phase 3: 모니터링
- [ ] Worker 로그에서 경고 확인
- [ ] `best_params.json` 값 합리성 검증
- [ ] Sharpe 비율 비교 (15분봉 vs 일봉 기준선)

---

## 6. 리스크 평가

| 리스크 | 확률 | 영향도 | 완화 방안 |
|--------|------|--------|----------|
| 수정으로 인한 새 버그 발생 | 낮음 | 높음 | 코드 리뷰 + 유닛 테스트 |
| 15분봉에 Optuna 파라미터 부적합 | 중간 | 중간 | 1주일 모니터링 후 재튜닝 |
| 학습용 데이터 부족 | 낮음 | 중간 | 학습 전 최소 1000 샘플 요구 |

---

## 7. 롤백 계획

수정 후에도 학습 실패 시:
1. 커밋 되돌리기: `git revert HEAD`
2. 백업 복원: `cp training.py.bak app/tasks/training.py`
3. 데이터 품질 조사: `scripts/check_data_quality.py` 실행

---

## 8. 배포 후 검증

**24시간 내**:
- [ ] Worker 로그 에러 모니터링
- [ ] 백테스트에서 모델 성능 확인
- [ ] 앙상블 가중치 합리성 검증 (한 모델에 100% 쏠림 없는지)

**1주일 내**:
- [ ] 15분봉 전략 Sharpe vs 일봉 기준선 비교
- [ ] 파라미터 패턴 문서화 (예: LGBM이 15분봉에서 얕은 트리 선호 등)

---

## 부록: 코드 참조

**수정 대상 파일**:
1. `app/tasks/training.py` (Line 147-148, 110)

**신규 생성 파일**:
1. `tests/test_training_15min.py`

**모니터링 대상 파일**:
1. `model_artifacts/best_params.json`
2. `model_artifacts/*.cbm` (CatBoost)
3. `model_artifacts/*.pkl` (LGBM, XGBoost)
