# 계획: 피처 불일치 수정 + 거래 전략 최적화

**날짜:** 2026-01-31  
**단계:** Phase H - ML 모델 최적화 (긴급 수정 + 개선)  
**관련 로드맵:** Backend_Roadmap.md Phase H.1-H.4

---

## 1. 목표

**주요 목표:** 학습 시(25개 피처)와 예측 시(32개 피처) 피처 개수 불일치로 인한 LightGBM 오류를 수정합니다.

**부가 목표:**
1. 모델 성능 평가 결과 자동 리포트 시스템 구현
2. 트레일링 스톱 로직 수정 (항상 "오픈 포지션 없음" 표시 문제)
3. 거래 임계값 최적화로 조기 매도 방지
4. Phase F 피처를 예측 후처리로만 활용

**오류 메시지:**
```
LightGBMError: The number of features in data (32) is not the same as it was in training data (25).
```

**근본 원인:**
- 학습 시: `base_feature_columns` 사용 (27개 기술 지표)
- 예측 시: `extract_feature_vector()`가 자동으로 5개 Phase F 피처 추가 (sentiment + fundamentals)
- 이 불일치로 인해 런타임에 모델 실패

---

## 2. 기술적 접근

### 2.1 핵심 전략
`extract_feature_vector()`에 Phase F 피처 포함 여부를 제어하는 매개변수 추가

### 2.2 설계 결정
**Option A (선택됨):** `include_phase_f: bool = False` 매개변수 추가
- **장점:** 최소한의 코드 변경, 하위 호환성 유지, 명시적 제어
- **단점:** 호출자가 Phase F 피처 포함 여부를 알아야 함

**Option B (기각):** 모델의 `feature_names_in_` 기반 자동 감지
- **장점:** 자동 피처 매칭
- **단점:** 피처 엔지니어에 모델 전달 필요 (강한 결합)

**Option C (기각):** 항상 32개 피처로 학습 (Phase F 포함)
- **장점:** 일관된 피처 세트
- **단점:** 과거 데이터에 sentiment/fundamentals 없음 (백필 필요)

---

## 3. 파일 변경

### 3.1 `app/ml/features.py`
**수정 대상:** `extract_feature_vector()` 메서드

**변경 내용:**
```python
def extract_feature_vector(
    self,
    df: pd.DataFrame,
    fit_scaler: bool = False,
    market_avg_volume: float = None,
    sentiment_score: float | None = None,
    fundamental_data: dict[str, float] | None = None,
    include_phase_f: bool = False  # ← 새 매개변수
) -> pd.DataFrame:
```

**로직:**
- `include_phase_f=False`: `base_feature_columns`만 사용 (27개 피처)
- `include_phase_f=True`: 전체 `feature_columns` 사용 (32개 피처)

**피처 선택:**
```python
if include_phase_f:
    feature_cols = self.feature_columns  # 32개 피처 (base + Phase F)
else:
    feature_cols = self.base_feature_columns  # 27개 피처 (base만)
```

### 3.2 `app/tasks/training.py`
**수정 대상:** `_load_and_prepare_data()` 함수

**변경 내용:** `extract_feature_vector(fit_scaler=True, include_phase_f=False)` 명시적 호출

**이유:** 학습 시 기본 피처만 사용 (과거 데이터에 Phase F 피처 없음)

### 3.3 `app/services/trading_strategy_sync.py`
**수정 대상:** `process_symbol()` 및 `process_portfolio()` 메서드

**변경 내용:** `extract_feature_vector(include_phase_f=False)` 호출

**향후 작업:** Phase F 피처 완전 통합 시 `include_phase_f=True`로 변경 및 모델 재학습

---

## 4. 테스트 시나리오 (필수)

### 4.1 Happy Path: 학습 → 예측 일관성
**테스트:**
1. `base_feature_columns` (27개)로 레짐 모델 학습
2. 모델 로드 후 `include_phase_f=False`로 새 데이터 예측
3. 확인: 피처 개수 불일치 오류 없음

**예상 결과:** 예측 성공, LightGBM 오류 없음

### 4.2 Edge Case: Phase F 통합 (미래)
**테스트:**
1. 과거 데이터에 수동으로 sentiment/fundamental 피처 추가
2. 32개 피처로 모델 학습 (`include_phase_f=True`)
3. 32개 피처로 예측 (`include_phase_f=True`)

**예상 결과:** 전체 피처 세트로 학습 및 예측 성공

### 4.3 회귀 테스트: Scaler 동작
**테스트:**
1. Scaler가 숫자 피처만 정규화하는지 확인 (`sector_id` 제외)
2. Scaler가 27개 피처로 올바르게 저장/로드되는지 확인

**예상 결과:** Scaler shape이 피처 개수와 일치

---

## 5. 리스크 및 완화 조치

### 리스크 1: Scaler 불일치
**문제:** 기존 저장된 scaler (`feature_scaler.pkl`)가 32개 피처일 수 있음  
**완화:** 기존 scaler 삭제, `include_phase_f=False`로 재학습하여 재생성

### 리스크 2: 모델 재학습 필요
**문제:** 모든 기존 레짐 모델이 잘못된 피처 개수로 학습됨  
**완화:** 수정 배포 후 모든 레짐 모델 강제 재학습

### 리스크 3: Phase F 출시 지연
**문제:** Sentiment/fundamentals가 아직 학습 파이프라인에 통합되지 않음  
**완화:** Phase F.3로 로드맵에 문서화 (다음 스프린트로 연기)

---

## 6. 구현 순서

### Task 1: 피처 일관성 (긴급 - Backend Agent)
1. **`features.py` 수정:** `include_phase_f` 매개변수 추가
2. **`training.py` 업데이트:** 학습 시 `include_phase_f=False` 보장
3. **`trading_strategy_sync.py` 업데이트:** 예측 시 `include_phase_f=False` 설정
4. **기존 아티팩트 삭제:** `feature_scaler.pkl` 및 모든 `*.pkl` 모델 제거
5. **모델 재학습:** 4개 레짐 모두에 대해 학습 태스크 실행
6. **예측 검증:** 트레이딩 태스크 실행 및 오류 없음 확인

### Task 2: 모델 성능 리포트 (Backend Agent)
1. **디렉토리 생성:** `.agent/model-reports/`
2. **`training.py` 수정:** `_save_performance_report()` 함수 추가
3. **출력 형식:** JSON + Markdown 요약
4. **포함 메트릭:** Sharpe, Accuracy, Overfit Ratio, Win Rate, 평균 승/패
5. **Discord 자동 업로드:** 주요 메트릭 알림 (옵션)

### Task 3: 트레일링 스톱 수정 (Trading Agent)
1. **조사:** 왜 `get_active_position()`이 빈 값을 반환하는지
2. **디버그 로깅:** 포지션 수 검증 추가
3. **쿼리 수정:** Position status 필터 로직 확인
4. **테스트:** 활성 포지션에 대한 트레일링 스톱 업데이트 확인

### Task 4: Threshold 최적화 (Quant Agent)
1. **분석:** 현재 threshold (0.2%) vs 거래 비용
2. **계산:** Sharpe/Win Rate 기반 최적 threshold
3. **구현:** 레짐별 차등 threshold + 최소 보유 시간
4. **전략:** Bull 보수적, Sideways_calm 공격적
5. **최소 보유:** 1시간 최소 포지션 유지 추가

---

## 7. 검증 체크리스트

- [ ] 사용되지 않는 import 추가 안 됨
- [ ] 모든 새 매개변수에 기본값 설정 (하위 호환성)
- [ ] 수정 확인 후 오류 로그 제거
- [ ] Phase F.3 연기 반영하여 로드맵 업데이트
- [ ] 수정 확인 Discord 알림 전송

---

**예상 시간:** 30분  
**복잡도:** 낮음 (매개변수 추가 + 플래그 전파)
