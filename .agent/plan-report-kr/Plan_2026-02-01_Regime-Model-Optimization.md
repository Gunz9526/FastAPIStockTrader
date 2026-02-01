# 계획서: 레짐 모델 최적화 및 섹터 기반 학습

**날짜:** 2026-02-01  
**단계:** H.5 - 고급 레짐 모델 최적화  
**상태:** 초안

---

## 1. 목표

### 1.1 Bull 모델 활성화 전략
- **문제:** Bull 모델 정확도 48.78%, Sharpe -0.42 (랜덤 이하)
- **근본 원인:** 피처-레짐 미스매치 (평균회귀 피처 vs 추세추종 필요)
- **해결책:** Bull 전용 추세추종 피처 추가 (ADX 기반, 돌파, 모멘텀)

### 1.2 레짐별 피처 충분성 분석
- **질문:** 현재 피처가 각 레짐에 충분한가?
- **분석 필요:** 레짐별 피처 중요도 검토

### 1.3 구현 작업
1. **Bull 전용 피처** - ADX 방향, 연속 고점, MA 정렬
2. **레짐별 TimeSeriesSplit** - 각 fold에 레짐 대표성 보장
3. **Bear 모델 OOS 검증** - 10.04 Sharpe가 과적합인지 검증
4. **섹터 기반 모델 분리** - 섹터별 모델 학습

---

## 2. 기술적 접근

### 2.1 Bull 전용 피처 (신규)

| 피처 | 공식 | 근거 |
|------|------|------|
| `adx_direction` | ADX 5봉 변화 | Bull 시장은 ADX 증가 |
| `plus_di_minus_di` | +DI - -DI | 양수 스프레드 = 상승 추세 |
| `consec_higher_highs` | 연속 고점 갱신 수 | 강한 상승 추세 확인 |
| `ma_alignment` | EMA12 > EMA26 > SMA50 | Bull 시장 정렬 |
| `above_sma200` | close > SMA200 | 장기 추세 필터 |

### 2.2 레짐 피처 분석

| 레짐 | 현재 피처 효과 | 개선 |
|------|--------------|------|
| bull_trending | ❌ 불량 (피처가 추세와 싸움) | 추세추종 추가 |
| bear_trending | ⚠️ 과적합 위험 (10.04 Sharpe) | OOS 검증 |
| sideways_calm | ✅ 양호 (평균회귀 작동) | 불필요 |
| sideways_volatile | ⚠️ 비활성 (70 샘플) | 데이터 축적 |

### 2.3 섹터 기반 모델 아키텍처

```
학습할 모델:
├── sector_tech_model.pkl (기술 섹터: AAPL, MSFT, NVDA, AMD)
├── sector_consumer_model.pkl (소비자: AMZN, CRM)
├── sector_financial_model.pkl (금융: 데이터 있으면)
├── sector_healthcare_model.pkl (헬스케어: 데이터 있으면)
└── sector_general_model.pkl (알 수 없는 섹터용 폴백)
```

### 2.4 OOS 검증 강화

```python
# 통계적 유의성을 갖춘 강화된 검증
def validate_regime_model(regime, model, X, y):
    1. 5 fold TimeSeriesSplit
    2. IS vs OOS Sharpe 비율 계산
    3. 부트스트랩 신뢰구간
    4. Sharpe > 0에 대한 T-test
    5. 플래그: OOS/IS < 0.3 또는 (IS > 5 AND OOS < 1)
```

---

## 3. 파일 변경

| 파일 | 작업 | 설명 |
|------|------|------|
| `app/ml/features.py` | 수정 | Bull 전용 5개 피처 추가 |
| `app/tasks/training.py` | 수정 | 섹터 기반 학습, 레짐 검증 추가 |
| `app/core/config.py` | 수정 | 섹터 모델 설정 추가 |
| `app/ml/predictor.py` | 수정 | 섹터 기반 예측 지원 |
| `app/services/trading_strategy_sync.py` | 수정 | 섹터 모델 사용 |

---

## 4. 테스트 시나리오

### 4.1 Bull 피처 테스트
- **정상 경로:** 추세 데이터에서 Bull 피처가 올바르게 계산됨
- **엣지 케이스:** 200봉 SMA를 위한 데이터 부족

### 4.2 섹터 학습 테스트
- **정상 경로:** Tech 섹터 모델이 AAPL, MSFT, AMD, NVDA로 학습됨
- **엣지 케이스:** 1000 샘플 미만 섹터는 일반 모델로 폴백

### 4.3 Bear OOS 검증 테스트
- **정상 경로:** Bear 모델이 OOS 검증 통과 (비율 > 0.3)
- **엣지 케이스:** 과적합 감지 시 모델 비활성화 또는 플래그

---

## 5. 위험

| 위험 | 완화 |
|------|------|
| Bull 피처로도 성능 개선 안됨 | sideways_calm 폴백 유지 |
| 섹터 모델 데이터 부족 | 계층적 폴백 사용 |
| Bear 모델 과적합 확인됨 | 모델 복잡도 감소, 정규화 추가 |

---

## 6. 성공 기준

1. Bull 모델 정확도 > 51% (랜덤 이상)
2. Bull 모델 Sharpe > 0 (양의 기대 수익)
3. Bear 모델 OOS 검증 비율 > 0.3
4. >= 3개 섹터에 대한 섹터 모델 학습
5. 모든 변경이 미사용 코드 검사 통과

---

**승인 필요:** Y/N/수정요청
