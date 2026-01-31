# 작업 보고서: Bull 레짐 성능 강화

**일시:** 2026-01-23
**단계:** H.4 (시장 국면 인식 - Bull 강화)
**상태:** ✅ 완료

---

## 목표

`bull_trending` 모델의 심각한 성능 문제 해결:
- **문제:** 49% 정확도 (랜덤보다 낮음) 및 -0.22 Sharpe 비율
- **근본 원인:** 추세 시장을 위한 모멘텀 특성 부족
- **이차 문제:** bear_trending이 의심스러운 10.47 Sharpe 표시 (과적합 가능성)

---

## 구현 요약

### 생성된 파일
| 파일 | 라인 수 | 목적 |
|------|---------|------|
| `app/ml/feature_analyzer.py` | 310 | Feature importance 추출 유틸리티 |
| `.agent/adr/ADR-001-Regime-Specific-Trading-Thresholds.md` | 95 | 아키텍처 결정 기록 |
| `.agent/plan-report/Plan_2026-01-23_Bull-Regime-Enhancement.md` | 180 | 구현 계획 (영문) |
| `.agent/plan-report-kr/Plan_2026-01-23_Bull-Regime-Enhancement.md` | 160 | 구현 계획 (한글) |

### 수정된 파일
| 파일 | 변경 사항 | 영향 |
|------|-----------|------|
| `app/ml/features.py` | +6개 모멘텀 특성, base_feature_columns 업데이트 | 21→27개 특성 |
| `app/core/config.py` | +REGIME_TRADING_CONFIG 딕셔너리 | 4개 레짐 설정 |
| `app/services/trading_strategy_sync.py` | +_execute_trade_logic의 레짐 인식 임계값 | 동적 거래 |
| `app/tasks/training.py` | +_walk_forward_validation_enhanced() | OOS 과적합 감지 |
| `.agent/Backend_Roadmap.md` | +Phase H.4 섹션 | 문서화 |
| `.agent/Backend_Roadmap_KR.md` | +Phase H.4 섹션 (한글) | 문서화 |

---

## 기술적 세부사항

### 1. Feature Importance Analyzer (`app/ml/feature_analyzer.py`)
- CatBoost, LightGBM, XGBoost 지원
- 앙상블 구성에 따른 가중 평균
- JSON 내보내기 및 사람이 읽을 수 있는 보고서 생성

### 2. 모멘텀 특성 (`app/ml/features.py`)
```python
momentum_5     = (close / close.shift(5)) - 1      # 5봉 모멘텀
momentum_10    = (close / close.shift(10)) - 1     # 10봉 모멘텀  
rsi_momentum   = rsi_14 - rsi_14.shift(5)          # RSI 가속도
trend_strength = abs(sma_10 - sma_50) / sma_50     # SMA 발산
price_position = (close - low_20) / (high_20 - low_20)  # 채널 위치
breakout_flag  = 1 if close > high_20.shift(1) else 0   # 20봉 돌파
```
- 총 특성: 21 → 27개 (base_feature_columns 업데이트)
- 근거: 강세장에는 평균회귀가 아닌 추세추종 필요

### 3. 레짐별 거래 설정 (`app/core/config.py`)
| 레짐 | 매수 임계값 | 매도 임계값 | 포지션 규모 | 설명 |
|------|------------|------------|-------------|------|
| bull_trending | 0.4% | -0.1% | 30% | 보수적 (약한 모델) |
| bear_trending | 0.2% | -0.2% | 70% | 중간 |
| sideways_volatile | 0.2% | -0.2% | 50% | 비활성 |
| sideways_calm | 0.2% | -0.2% | 100% | 최적 모델 |

### 4. Walk-Forward OOS 검증 (`app/tasks/training.py`)
- TimeSeriesSplit 기반 검증 (기본 5 splits)
- 과적합 감지: `OOS/IS Sharpe 비율 < 0.3` 또는 `IS > 5이고 OOS < 1`
- 모델 신뢰도: OOS Sharpe 성능 기반 0.1-1.0

---

## 검증 결과

| 검사 항목 | 상태 | 비고 |
|----------|------|------|
| 미사용 코드 검사 | ✅ 통과 | 모든 새 import 사용, 모든 함수 호출됨 |
| 경계 검사 | ✅ 통과 | 범위 내 파일만 수정 |
| 버전 검사 | ✅ 통과 | 새 종속성 추가 없음 |
| 기능 검사 | ✅ 통과 | PM 권장사항 준수 |
| 타입 힌트 | ✅ 통과 | 모든 새 함수에 타입 어노테이션 |

---

## 실행 시간

- 계획 수립: 15분
- 구현: 45분
- 검증: 10분
- 문서화: 15분
- **총계:** ~85분

---

## 로드맵 영향

### 완료 항목
- [x] H.4 Bull 레짐 강화 (새 섹션)
  - [x] Feature Importance Analyzer
  - [x] 강세장 전용 모멘텀 특성
  - [x] 레짐별 거래 임계값
  - [x] Walk-Forward OOS 검증

### 새로운 기술 부채
- [ ] 모멘텀 특성 통합 테스트 (우선순위: 높음)
- [ ] bear_trending 모델 OOS 검증 후 재학습 (우선순위: 긴급)
- [ ] Celery Beat에 Feature importance 분석 자동화 (우선순위: 중간)

---

## 다음 단계 (권장)

1. **즉시:** 새 특성으로 전체 학습 실행
   ```bash
   docker compose exec app celery -A app.worker call app.tasks.training.train_models
   ```

2. **검증:** bear_trending OOS Sharpe 확인
   - `overfit_detected=True`면 정규화 추가 후 재학습

3. **모니터링:** 레짐별 성능 Grafana 대시보드 추가
   - 시간에 따른 레짐별 Sharpe 비율 추적
