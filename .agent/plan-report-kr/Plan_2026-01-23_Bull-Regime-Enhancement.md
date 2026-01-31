# 계획: Bull 레짐 모델 개선 및 Walk-Forward 검증

**날짜:** 2026-01-23  
**단계:** H.4 (신규) - 레짐 모델 최적화  
**상태:** 진행 중

---

## 1. 목표

`bull_trending` 레짐 모델의 저조한 성능(정확도 49%, Sharpe -0.22) 개선:
1. Feature Importance 분석으로 약한 예측자 식별
2. Bull 시장 전용 특성 엔지니어링 (모멘텀 지표)
3. 레짐 인식 거래 전략 조정
4. Walk-Forward Out-of-Sample 검증

---

## 2. 문제 분석

### 현재 성능 지표
| 레짐 | 정확도 | Sharpe | 평가 |
|------|--------|--------|------|
| bull_trending | 49.02% | -0.2246 | ❌ 심각 |
| bear_trending | 52.81% | 10.4766 | ✅ 양호 (과적합 의심) |
| sideways_volatile | - | - | ⚠️ 스킵 (70개 샘플) |
| sideways_calm | 53.21% | 6.5283 | ✅ 양호 |

### 근본 원인 가설
1. **Bull 시장 특성 부족**: 현재 특성이 평균회귀(RSI, BB)에 치우침
2. **추세추종 vs 평균회귀 충돌**: Bull 시장은 추세추종 로직 필요
3. **Bear/Sideways 과적합**: 최근 시장 조건에 과적합 가능성
4. **Sharpe > 10 비현실적**: Bear 모델 과적합 (OOS 검증 필요)

---

## 3. 기술적 접근

### 3.1 Feature Importance 분석 (Task 1)
- 각 레짐 모델에서 특성 중요도 추출
- 분석용 JSON + 시각화 생성
- 중요도 낮은 특성 식별 (제거 후보)

### 3.2 Bull 전용 특성 엔지니어링 (Task 2)
추세 시장에 최적화된 모멘텀 중심 특성 추가:

| 특성 | 공식 | 근거 |
|------|------|------|
| `momentum_5d` | Close / Close(-5 bars) - 1 | 단기 모멘텀 |
| `momentum_10d` | Close / Close(-10 bars) - 1 | 중기 모멘텀 |
| `rsi_momentum` | RSI - RSI(-5 bars) | RSI 추세 방향 |
| `trend_strength` | (EMA12 - EMA26) / ATR | 정규화된 추세 측정 |
| `price_position` | (Close - Low20) / (High20 - Low20) | 20봉 범위 내 위치 |
| `breakout_flag` | Close > High(20) ? 1 : 0 | 돌파 감지 |

### 3.3 레짐 인식 거래 조정 (Task 3)
레짐별 임계값 적용:

| 레짐 | 매수 임계값 | 매도 임계값 | 신뢰도 |
|------|-------------|-------------|--------|
| bull_trending | 0.4% | -0.1% | 30% (보수적) |
| bear_trending | 0.2% | -0.2% | 70% |
| sideways_calm | 0.2% | -0.2% | 70% |

### 3.4 Walk-Forward OOS 검증 (Task 4)
과적합 감지를 위한 적절한 검증:

```
훈련 윈도우: 18개월 롤링
검증 윈도우: 3개월 (Out-of-Sample)
```

---

## 4. 파일 변경

### 수정 파일
| 파일 | 변경 내용 |
|------|-----------|
| `app/ml/features.py` | 6개 모멘텀 특성 추가 |
| `app/services/trading_strategy_sync.py` | 레짐별 임계값 |
| `app/tasks/training.py` | 개선된 Walk-Forward 검증 |

### 신규 파일
| 파일 | 목적 |
|------|------|
| `app/ml/feature_analyzer.py` | Feature Importance 분석 유틸리티 |
| `docs/BULL_REGIME_ANALYSIS.md` | 분석 보고서 |

---

## 5. 성공 기준

1. ✅ Bull 레짐 정확도 > 50%
2. ✅ Bull 레짐 Sharpe > 0.0
3. ✅ OOS 검증에서 심각한 과적합 없음
4. ✅ Bear/Sideways 모델 성능 유지
5. ✅ 문서화 완료

---

## 6. 예상 소요 시간

| 작업 | 시간 |
|------|------|
| Feature Importance 분석 | 30분 |
| 모멘텀 특성 구현 | 1시간 |
| 거래 전략 조정 | 30분 |
| Walk-Forward 검증 | 1시간 |
| 문서화 | 30분 |

**총:** ~3.5시간
