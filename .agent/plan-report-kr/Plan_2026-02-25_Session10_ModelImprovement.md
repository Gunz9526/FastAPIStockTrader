# 계획: 세션 10 — 모델 성능 개선 & 감성 스케줄 조정

**날짜**: 2026-02-25  
**단계**: J.3 (모델 훈련 평가 및 개선)  
**세션**: 10

---

## 목표

모델 진단 강화, regime별 클래스 불균형 수정, 감성 API 호출 절감, 재훈련 준비

---

## 작업 목록

| # | 작업 | 파일 | 위험도 |
|---|------|------|--------|
| 1 | 감성 스케줄 7회→2회/일 (8AM, 12PM EST) | worker.py | Low |
| 2 | 훈련 리포트: Sharpe→NEUTRAL Recall + Per-class metrics | training.py | Medium |
| 3 | Regime별 CLASS_WEIGHTS (bear NEUTRAL 1.5배) | training.py | Medium |
| 4 | Classification Report sklearn 로깅 | training.py | Low |
| 5 | Feature Importance Top-10 로깅 | training.py | Low |

---

## Regime별 CLASS_WEIGHTS

| Regime | DOWN(0) | NEUTRAL(1) | UP(2) | 근거 |
|--------|---------|-----------|-------|------|
| bull_trending | 1.3 | 1.2 | 1.0 | UP 과예측 억제 |
| bear_trending | 1.0 | 1.5 | 1.3 | NEUTRAL 11%→목표 40%+ |
| sideways_calm | 1.2 | 1.3 | 1.0 | UP 60% 초과예측 수정 |
| sideways_volatile | 1.2 | 1.3 | 1.0 | sideways_calm과 동일 |
