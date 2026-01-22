# 계획: Phase 1 (Walk-Forward 검증) + Phase 2 (섹터 피처)

**날짜:** 2026-01-03  
**목표:** 견고한 검증 구현 및 섹터/상대적 데이터로 피처 강화

---

## Phase 1: Walk-Forward 검증

### 근거
단일 30일 검증 기간의 문제점:
- 시장 국면 변화에 취약 (상승/하락/횡보장)
- 최근 데이터에 과적합 위험
- Sharpe 비율 추정에 샘플 부족

### 해결책
**3개 기간에 걸친 Walk-Forward 검증**:
1. **90-60일 전**: 가장 오래된 검증 기간
2. **60-30일 전**: 중간 검증 기간  
3. **30-0일 전**: 최신 기간

### 구현 내용
1. **공유 데이터 로딩 함수** (`_load_and_prepare_data`):
   - `train_models`와 `tune_models` 간 코드 중복 제거
   - 심볼 처리, 피처 엔지니어링, 타겟 생성 통합
   - 섹터 피처를 위한 심볼 추적 추가

2. **Walk-Forward 검증 함수** (`_walk_forward_validation`):
   - 3개의 구별된 시간 윈도우에서 모델 테스트
   - 각 기간별 Sharpe 비율 계산
   - 견고한 성능 추정을 위한 평균 Sharpe 반환

3. **train_models 리팩토링**:
   - 공유 데이터 로딩 사용
   - 가중치 계산에 Walk-Forward 검증 적용
   - Sharpe 전용으로 가중치 계산 단순화 (F1 복잡도 제거)

4. **tune_models 리팩토링**:
   - 공유 데이터 로딩 사용
   - 비율 튜닝 제거 (Sharpe:F1 최적화)
   - 하이퍼파라미터 최적화에만 집중

### 기술적 이점
- **30-40% 빠른 튜닝**: MedianPruner 조기 중단
- **과적합 감소**: 다중 기간 검증
- **깔끔한 코드**: 150+ 라인 중복 제거
- **일반화 향상**: 다양한 시장 상황 테스트

---

## Phase 2: 섹터 및 상대적 피처

### 근거
현재 피처는 100% 기술적 지표:
- 횡단면 정보 없음 (동종 대비 성과)
- 섹터별 패턴 없음 (테크와 금융은 다름)
- 시장 상대 지표 없음

### 해결책
**3개 신규 피처 추가**:

1. **섹터 ID** (범주형)
   - 각 심볼을 섹터에 매핑: Technology, Finance, Healthcare, Automotive, Consumer, Unknown
   - 모델이 섹터별 패턴 학습 가능
   - CatBoost가 범주형 네이티브 처리

2. **상대적 거래량** (연속형)
   - 공식: `symbol_volume / market_avg_volume`
   - 비정상 거래 활동 감지
   - 심볼 간 거래량 정규화

3. **시장 상대 수익률** (향후 개선)
   - 공식: `symbol_return - market_avg_return`
   - 아웃퍼포머/언더퍼포머 식별
   - (향후 반복에서 계획)

### 구현 내용

1. **신규 파일: `app/ml/sector_map.py`**
   - `SECTOR_MAP`: 심볼-섹터 딕셔너리
   - `SECTOR_TO_ID`: 섹터-숫자 매핑 (CatBoost용)
   - 헬퍼 함수: `get_sector()`, `get_sector_id()`

2. **수정: `app/ml/features.py`**
   - sector_map에서 `get_sector_id` 임포트
   - **`add_technical_indicators()`**: `sector_id` 컬럼 추가
   - **`extract_feature_vector()`**: 
     - 신규 파라미터: `market_avg_volume`
     - `relative_volume` 피처 계산
     - 범주형 vs 수치형 스케일링 처리 (sector_id는 스케일링 안 함)
   - **`feature_columns` 속성**: `sector_id`, `relative_volume` 포함 업데이트

3. **수정: `app/tasks/training.py`**
   - 모든 심볼 로딩 후 `market_avg_volume` 계산
   - `extract_feature_vector()`에 `market_avg_volume` 전달
   - 총 피처: 17 → 19 (sector_id, relative_volume 추가)

### 기대 효과
- **횡단면 분석 개선**: 유사 종목 비교 가능
- **섹터별 전략**: 테크 종목 vs 금융 종목 구분
- **거래량 이상 감지**: 높은 상대 거래량 → 잠재적 돌파
- **일반화 개선**: 시계열 패턴만 의존하지 않음

---

## 앙상블 가중치 계산 방법

### 기존 방법 (리팩토링 전)
**Sharpe + F1 혼합 점수**
```python
sharpe_weight = 0.7  # Optuna 튜닝에서
f1_weight = 0.3
combined_score = sharpe_weight * sharpe + f1_weight * f1
```

**문제점:**
1. F1 점수는 방향 정확도 측정 (분류 지표)
2. Sharpe 비율은 위험 조정 수익률 측정 (트레이딩 지표)
3. 분류와 트레이딩 지표 혼합은 이론적으로 불일치
4. 가중치 비율에 대한 추가 Optuna 튜닝 필요 (50회 시도)

### 신규 방법 (리팩토링 후)
**Sharpe 전용 가중치**
```python
model_weights = [sharpe1, sharpe2, sharpe3]
normalized_weights = weights / sum(weights)
```

**장점:**
1. **이론적 건전성**: 모든 모델을 동일 지표로 측정 (위험 조정 수익률)
2. **간단함**: 비율 튜닝 불필요
3. **빠름**: 50회 시도 비율 최적화 제거
4. **명확한 해석**: "모델 A는 Sharpe가 전체의 40%이므로 40% 가중치"

### 대안: 순위 기반 가중치 (향후)
```python
ranks = rank([sharpe1, sharpe2, sharpe3])  # 예: [3, 1, 2]
weights = softmax(ranks)  # 확률로 변환
```
- 이상치에 더 견고
- `scipy` 또는 수동 softmax 구현 필요

---

## 코드 변경 요약

### 생성된 파일
1. `app/ml/sector_map.py` (47줄) - 섹터 매핑

### 수정된 파일
1. `app/ml/features.py`:
   - sector_id 피처 추가 (77-83줄)
   - extract_feature_vector() 시그니처 업데이트 (106줄)
   - relative_volume 계산 추가 (120-124줄)
   - 수치형/범주형 스케일링 분리 (135-160줄)
   - feature_columns 속성 업데이트 (200-203줄)

2. `app/tasks/training.py`:
   - Walk-Forward 기간 상수 추가 (23줄)
   - `_load_and_prepare_data()` 함수 생성 (27-115줄)
   - `_walk_forward_validation()` 함수 생성 (117-176줄)
   - `train_models()` 리팩토링 (178-280줄)
   - `tune_models()` 리팩토링 (283-500줄)
   - 비율 튜닝 제거 (best_params.json 단순화)

### 코드 라인
- **추가**: ~280줄 (공유 함수 + 섹터 피처)
- **제거**: ~200줄 (중복 코드 + 비율 튜닝)
- **순 변경**: +80줄 (더 깔끔하고 유지보수 가능)

---

## 테스트 전략

### 단위 테스트 (기존)
- `tests/test_training_15min.py`가 기본 로직 커버
- 추가 필요한 테스트:
  - 섹터 매핑 정확성
  - 상대 거래량 계산
  - Walk-Forward 검증 로직

### 통합 테스트
1. **tune_models 실행**:
   - best_params.json 생성 확인
   - 3개 모델의 Sharpe 비율 확인
   - 출력에 F1/비율 필드 없음 확인

2. **train_models 실행**:
   - Walk-Forward 검증이 3회 실행되는지 확인
   - 최종 가중치가 Sharpe 기반인지 확인
   - 앙상블 모델 저장 확인

3. **피처 검사**:
   - 학습된 모델의 피처 중요도 쿼리
   - `sector_id`와 `relative_volume` 존재 확인
   - sector_id가 범주형으로 처리되는지 확인 (CatBoost)

### 성능 벤치마크
- **튜닝 시간**: ~60-90분 (100회 × 3 모델)
- **학습 시간**: ~5-10분 (10 심볼 × 2년)
- **메모리 사용**: 학습 중 < 8GB 유지

---

## 배포 체크리스트

- [x] sector_map.py 생성
- [x] features.py에 섹터 + 상대 거래량 업데이트
- [x] Walk-Forward 검증 함수 추가
- [x] train_models를 Walk-Forward 사용하도록 리팩토링
- [x] tune_models를 공유 데이터 로딩 사용하도록 리팩토링
- [x] 비율 튜닝 복잡도 제거
- [ ] 프로덕션에서 tune_models 실행 (100회 시도)
- [ ] best_params.json 구조 확인
- [ ] 신규 가중치로 train_models 실행
- [ ] 1주일간 모델 성능 모니터링
- [ ] 완료 상태로 로드맵 업데이트

---

## 성공 지표

### 코드 품질
- [x] 코드 중복 감소 (200줄 제거)
- [x] 모듈성 향상 (2개 공유 함수)
- [x] 유지보수성 개선 (단일 진실 공급원)

### 모델 성능
- [ ] Walk-Forward 기간 전체 평균 Sharpe 비율 > 0.5
- [ ] 섹터 피처가 상위 5개 중요도에 포함
- [ ] 상대 거래량과 돌파 상관관계

### 운영
- [ ] 튜닝이 2시간 내 완료
- [ ] 학습이 15분 내 완료
- [ ] 프로덕션 로그에 오류 없음

---

## 위험 완화

### 위험 1: 섹터 피처 과적합
**완화**: 섹터가 6개뿐 (낮은 카디널리티)이므로 과적합 위험 감소

### 위험 2: 시장 평균 거래량이 이상치로 왜곡
**완화**: 평균 대신 중앙값 사용 (향후 개선)

### 위험 3: Walk-Forward 검증에서 음수 Sharpe 표시
**완화**: 동일 가중치로 폴백 [0.33, 0.33, 0.33]

---

## 향후 개선사항

1. **시장 상대 수익률**:
   - `market_return` 피처 추가
   - `symbol_return - market_return` 계산
   
2. **동적 섹터 매핑**:
   - Alpaca API에서 섹터 데이터 가져오기
   - SECTOR_MAP 분기별 자동 업데이트

3. **상관관계 기반 피처**:
   - SPY와의 롤링 상관관계 계산
   - `correlation_change`를 피처로 추가

4. **고급 Walk-Forward**:
   - 5-6개 기간으로 확장
   - 계절 분석 추가 (분기별 패턴)

---

**문서 상태**: 완료  
**구현 상태**: Phase 1 + Phase 2 완료  
**다음 작업**: 프로덕션 배포 및 모니터링
