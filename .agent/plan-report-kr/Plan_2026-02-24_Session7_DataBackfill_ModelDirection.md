# 계획: Session 7 — 데이터 백필, 심볼 확장 및 모델 학습 방향성

**날짜**: 2026-02-24  
**단계**: J.1 + J.2 + 모델 아키텍처 결정  
**로드맵 참조**: Phase J (Data Backfill & Model Training)

---

## 목표

1. **심볼 유니버스 확장**: 17 → 60+ 종목, 11개 GICS 섹터 전체 커버
2. **일봉 OHLCV 백필 스크립트**: `backfill_ohlcv.py`에 `timeframe='1d'` 지원 추가
3. **모델 학습 방향성**: Option C (Sector as Categorical Feature) 적용 — `sector_id`를 ordinal numeric → native categorical로 수정
4. **SECTOR_MAP 업데이트**: 전체 GICS 커버리지, Unknown(99) → 12 재매핑

---

## 분석 결과 요약

### 섹터별 vs 통합 모델: Option C 선택 (신뢰도 92%)

| Option | 모델 수 | 모델당 샘플 | 과적합 리스크 | 구현 난이도 |
|--------|---------|-----------|------------|-----------|
| A: 통합 (현행) | 4 | 11,250 | Low | 기존 유지 |
| B: 섹터별 | 44 | ~1,023 (최소 101) | **CRITICAL** | 대규모 |
| **C: Sector Feature** | **4** | **11,250** | **Low** | **소규모** |

**핵심 발견**: `sector_id`가 현재 ordinal numeric으로 잘못 처리 중 → categorical 지정만으로 Accuracy +1.5~3.0% 개선 기대

---

## 성공 기준

- 60+ 활성 심볼 등록
- `backfill_ohlcv.py`가 `--timeframe 1d` 지원 (기본값)
- CatBoost에 `cat_features` 지정
- LightGBM에 `categorical_feature` 지정
- 전체 신규 심볼에 대한 sector_map 커버리지
- 수정 파일 0 lint errors
