# 세션 7 작업 보고서: 데이터 백필 & Categorical Feature Engineering

> **날짜**: 2026-02-24 | **상태**: ✅ 완료 (J.1/J.2 완료, J.3 실행 대기)

---

## 목표

1. Phase J (데이터 백필 & 모델 학습) 진행
2. 심볼 유니버스 60종목으로 확장 (11개 GICS 섹터)
3. 섹터별 vs 통합 모델 학습 분석 → **결정: Option C (Sector를 Categorical Feature로 활용)**
4. 전체 ML Classifier에 Native Categorical Encoding 적용

---

## 핵심 결정: 모델 학습 아키텍처

### 분석 요약 (Quant 서브에이전트)

| 옵션 | 점수 | 판정 |
|------|------|------|
| A — 통합 모델 (현재) | 4.15/5 | 모델당 데이터 충분하나 sector_id ordinal 처리 **버그** |
| B — 44개 섹터별 모델 | 1.95/5 | 치명적: 36–45% 모델이 min_samples=300 미달, 과적합 |
| **C — Sector를 Categorical Feature로** | **4.55/5** | ✅ 4개 regime 모델 유지, native categorical encoding 추가 |

**신뢰도**: 92%

### 발견된 치명적 버그
`sector_id`가 ordinal numeric으로 처리됨 (0 < 1 < 2... 의미없는 순서). 섹터 같은 nominal 데이터에 **부적합**. 수정: 프레임워크별 native categorical encoding.

### 예상 개선
- Accuracy: +1.5~3.0%
- F1 Score: +2~4%
- 추가 학습 시간 오버헤드: 없음

---

## 수정된 파일

| 파일 | 주요 변경 | 오류 |
|------|----------|------|
| `scripts/add_symbols.py` | 기존 ticker 섹터 업데이트, 도움말 텍스트, 섹터별 그룹 출력 | 0 ✅ |
| `scripts/backfill_ohlcv.py` | 전면 재작성: `--timeframe` CLI 인수, 일봉 기본값, 효율적 검증 쿼리 | 0 ✅ |
| `app/ml/sector_map.py` | 17→62 심볼, Unknown 99→12, NUM_SECTORS=13, GICS 분류 수정 | 0 ✅ |
| `app/ml/features.py` | sector_id fallback 5→12 | 0 ✅ |
| `app/ml/models.py` | CatBoost cat_features, LightGBM categorical_feature, XGBoost enable_categorical, Ensemble _train_with_categorical() | 0 ✅ |
| `app/tasks/training.py` | symbol_limit: 10→None | 0 ✅ |

---

## 다음 단계 (세션 8)

1. **데이터 파이프라인 실행**:
   ```bash
   python scripts/add_symbols.py
   python scripts/backfill_ohlcv.py --years 2 --timeframe 1d
   ```

2. **J.3 — 최초 모델 학습**: `train_models` Celery task 실행 → 4개 regime 모델 생성

3. **Phase K — 프로덕션 강화** (학습 후)
