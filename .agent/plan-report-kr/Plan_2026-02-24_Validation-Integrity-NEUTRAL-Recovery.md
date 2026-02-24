# 계획: 세션 9 — 검증 무결성 & NEUTRAL 복구

**날짜**: 2026-02-24  
**단계**: J.3.1 (학습 파이프라인 품질 개선)  
**트리거**: 세션 8 학습 결과 분석

---

## 목표

첫 번째 학습 결과의 정량 분석에서 발견된 3가지 critical 문제 수정:

1. **Data Leakage**: 검증 metric이 학습에 사용된 데이터로 계산됨 (in-sample)
2. **NEUTRAL 클래스 붕괴**: 예측 NEUTRAL < 0.2%, 실제 NEUTRAL ~18.6%
3. **θ 과소**: CLASSIFICATION_THRESHOLD = 0.003 → 0.005로 조정

---

## 수정 내용

### Fix 1: 검증 데이터 분리 (Data Leakage 제거)
- 학습 전에 holdout set (20%) 분리
- Holdout으로 true out-of-sample metric 보고
- Production 모델은 전체 데이터로 재학습

### Fix 2: CLASS_WEIGHTS 정상화
- `{0: 1.5, 1: 0.5, 2: 1.5}` → `{0: 1.3, 1: 1.0, 2: 1.3}`
- NEUTRAL이 "거래하지 않음" signal로 작동

### Fix 3: θ 조정
- `0.003 (±0.3%)` → `0.005 (±0.5%)`
- Daily noise 필터 개선

### Fix 4: min_samples 상향
- `300` → `500`
- sideways_volatile fallback to sideways_calm

---

## 파일 변경

| 파일 | 변경 |
|------|------|
| `app/tasks/training.py` | 검증 분리, min_samples, θ |
| `app/ml/models.py` | DEFAULT_CLASS_WEIGHTS |

---

**영문 계획 참조**: `.agent/plan-report/Plan_2026-02-24_Validation-Integrity-NEUTRAL-Recovery.md`
