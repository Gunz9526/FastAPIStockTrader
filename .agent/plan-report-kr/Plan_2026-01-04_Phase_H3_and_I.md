# 계획: Phase H.3 및 Phase I 구현
**날짜:** 2026-01-04  
**계획명:** Phase H.3 (국면별 모델 학습) + Phase I (고급 리스크 및 포지션 방어)  
**상태:** 계획 단계  
**로드맵 연계:** Backend_Roadmap.md Phase F.3 (시장 국면) + 신규 Phase I

---

## 1. 요약

**목표:**
1. **Phase H.3**: 4개 국면별 전용 모델 학습 (bull_trending, bear_trending, sideways_volatile, sideways_calm)
2. **Phase I (우선순위)**: 조기 매도 및 빠른 재거래 방지 메커니즘 구현
3. **Phase I (부차적)**: 포트폴리오 최적화 및 고급 리스크 관리 (MPT, Kelly Criterion, VaR)

**긴급 발견 사항 (감사 결과):**
- **포지션 보유 시간 추적 없음**: 시스템이 같은 종목을 분 단위로 매수/매도 가능 (15분 주기)
- **최소 수익 임계값 없음**: 0.1% 수익에도 예측 변경 시 포지션 청산
- **재거래 대기 시간 없음**: 매도 직후 즉시 재매수 가능
- **Risk Manager 존재**하지만 보유 기간이나 수익 최소값 강제하지 않음

**예상 범위:**
- Phase H.3: ~3개 파일 수정 (training.py, predictor.py 리팩토링)
- Phase I 방어: ~5개 파일 수정 (risk_manager.py, trading_strategy_sync.py, DB 스키마)
- Phase I 고급: 연기 (외부 API, 신규 서비스 필요)

---

## 2. PHASE H.3: 국면별 모델 학습

### 2.1 현재 상태
**완료:**
- ✅ RegimeDetector가 SyncTradingStrategy에 통합됨
- ✅ PredictorService가 4개 국면별 모델 지원
- ✅ predict_next()가 regime 파라미터 수용

**갭:**
- ❌ 범용 모델 파일 1개만 존재: `ensemble_model.pkl`
- ❌ 학습 파이프라인(training.py)이 국면별 데이터 분류하지 않음
- ❌ 과거 데이터를 4개 국면 버킷으로 분할하는 로직 없음

### 2.2 구현 전략

**파일: `app/tasks/training.py`**

**필요 변경사항:**
1. `train_models()`에 국면 분류 단계 추가:
   ```python
   # 피처 생성 후 각 행을 국면별로 분류
   regime_detector = RegimeDetector()
   regimes = []
   for idx, row in features_df.iterrows():
       regime = regime_detector.detect_regime(features_df.loc[:idx])
       regimes.append(regime.value)
   features_df['regime'] = regimes
   ```

2. 데이터를 4개 국면별 데이터셋으로 분할:
   ```python
   for regime in MarketRegime:
       regime_data = features_df[features_df['regime'] == regime.value]
       if len(regime_data) < 1000:  # 최소 데이터 요구사항
           logger.warning(f"국면 {regime.value} 데이터 부족: {len(regime_data)} 행")
           continue
       # 해당 국면용 앙상블 학습
       ensemble = EnsembleWrapper(weights=best_weights)
       ensemble.fit(X_regime, y_regime)
       ensemble.save(f"ensemble_model_{regime.value}.pkl")
   ```

3. 국면 인식 Walk-Forward 검증 추가:
   - 각 검증 윈도우를 국면별로 분류
   - 국면별 Sharpe 비율 계산
   - 국면별 앙상블 가중치 최적화

**복잡도:** 중  
**예상 변경 라인:** ~150줄  
**리스크:** 중 (학습 실패 시 범용 모델로 폴백 가능)

---

## 3. PHASE I.1: 거래 방어 메커니즘 (최우선)

### 3.1 문제 분석

**이슈 1: 빠른 포지션 전환**
```
09:30 - AAPL 매수 @ $180 (예측: 0.52)
09:45 - AAPL 매도 @ $180.10 (예측: 0.48, 수익: $0.10)
```
- **근본 원인:** 최소 보유 기간 없음. 15분 주기로 즉시 청산 가능
- **영향:** 높은 거래 비용, 추세 지속 기회 상실

**이슈 2: 미미한 수익으로 청산**
```
$100 매수 → $100.05 매도 (0.05% 수익)
```
- **근본 원인:** 청산 전 최소 수익 임계값 없음
- **영향:** 거래 수수료가 수익 초과 (Alpaca: 주당 ~$0.005)

**이슈 3: 즉시 재진입**
```
09:30 - AAPL $180 매수
09:45 - AAPL $180.10 매도
10:00 - AAPL $180.05 재매수 (다시!)
```
- **근본 원인:** 청산 후 대기 시간 없음
- **영향:** 톱날 거래(whipsaw), 과매매 페널티

### 3.2 방어 메커니즘 설계

**해결책 1: 최소 보유 기간**
- **규칙:** 보유 기간 < 4바 (15분 거래 기준 60분)이면 청산 금지
- **구현:** DB 또는 메모리에 `position_entry_time` 추적
- **예외:** 손절매 발동 시 무시 (안전 우선)

**해결책 2: 최소 수익 임계값**
- **규칙:** 수익 > 1.5% 또는 시간 > 8바가 아니면 예측 반전에도 청산 금지
- **구현:** 미실현 손익 계산, 임계값과 비교
- **공식:** `(current_price - entry_price) / entry_price > 0.015`
- **근거:** 거래 비용(0.3%) 대비 5배 안전 마진, 15분봉 평균 ATR 고려

**해결책 3: 종목 쿨다운 기간**
- **규칙:** 매도 후 N바(예: 4바 = 60분) 동안 해당 종목 블랙리스트
- **구현:** RiskManager에서 종목별 `last_exit_time` 추적
- **공식:** `current_time - last_exit_time > timedelta(minutes=60)`

### 3.3 구현 계획

**파일 1: `app/core/database.py` (스키마 업데이트)**
- 테이블 추가: `position_tracking`
  - 컬럼: symbol, entry_time, entry_price, quantity, exit_time (nullable), exit_price (nullable)
  - 목적: 컨테이너 재시작 시에도 지속적 추적 (PostgreSQL persistence)
  - Redis 역할: 쿨다운 임시 캐시 (60분 TTL, 휘발성 데이터)

**파일 2: `app/services/risk_manager.py`**
```python
class RiskManager:
    def __init__(self):
        # 기존 코드...
        self.position_entry_times: Dict[str, datetime] = {}  # In-memory cache
        self.symbol_cooldowns: Dict[str, datetime] = {}     # Redis-backed
        self.min_hold_bars = 4                               # 60분 (15분 x 4바)
        self.min_profit_pct = 0.015                          # 1.5% (거래비용 5배)
        self.cooldown_bars = 4                               # 60분 쿨다운
        # TODO: 향후 ATR 기반 동적 조정 옵션 추가
    
    def can_exit_position(
        self, 
        symbol: str, 
        entry_price: float, 
        current_price: float,
        entry_time: datetime,
        bars_per_cycle: int = 15
    ) -> Tuple[bool, str]:
        """방어 규칙에 따라 포지션 청산 가능 여부 확인"""
        
        # 규칙 1: 최소 보유 기간
        hold_duration = datetime.now() - entry_time
        min_hold_time = timedelta(minutes=self.min_hold_bars * bars_per_cycle)
        if hold_duration < min_hold_time:
            return False, f"최소보유: {hold_duration.seconds//60}분 < {min_hold_time.seconds//60}분"
        
        # 규칙 2: 최소 수익 임계값 (1.5%)
        profit_pct = (current_price - entry_price) / entry_price
        if profit_pct < self.min_profit_pct:
            # 8바(120분) 경과 시 손실이어도 청산 허용
            max_hold_time = timedelta(minutes=8 * bars_per_cycle)
            if hold_duration < max_hold_time:
                return False, f"최소수익: {profit_pct:.2%} < 1.5%"
        
        return True, "OK"
    
    def can_enter_position(self, symbol: str) -> Tuple[bool, str]:
        """종목 쿨다운 기간 확인"""
        if symbol in self.symbol_cooldowns:
            cooldown_end = self.symbol_cooldowns[symbol]
            if datetime.now() < cooldown_end:
                remaining = (cooldown_end - datetime.now()).seconds // 60
                return False, f"쿨다운: {remaining}분 남음"
        return True, "OK"
    
    def record_position_exit(self, symbol: str):
        """청산 기록 및 쿨다운 시작"""
        cooldown_duration = timedelta(minutes=self.cooldown_bars * 15)
        self.symbol_cooldowns[symbol] = datetime.now() + cooldown_duration
        logger.info(f"{symbol} 쿨다운 종료: {self.symbol_cooldowns[symbol]}")
```

**파일 3: `app/services/trading_strategy_sync.py`**
- `_execute_trade_logic()` 수정:
  - 매수 전: `risk_manager.can_enter_position(symbol)` 확인
  - 매도 전: `risk_manager.can_exit_position(symbol, ...)` 확인
- 포지션 오픈 시 `position_entry_time` 추적
- 청산 체크 시 entry_time 전달

**복잡도:** 중상  
**예상 변경 라인:** ~200줄  
**리스크:** 낮음 (방어적 추가, 호환성 파괴 없음)

---

## 4. PHASE I.2: 포트폴리오 최적화 (연기)

**연기 사유:**
- 현대 포트폴리오 이론(MPT)은 다중 동시 포지션 필요
- 현 시스템은 1종목씩 순차 거래
- Kelly Criterion은 승률 추정 필요 (>50회 백테스트)
- VaR 계산은 과거 포트폴리오 스냅샷 필요 (아직 미추적)

**권고사항:**
- Phase H.3 및 I.1 완료 후 재검토
- 실거래 데이터 1개월 수집 후 재평가
- 배치 포지션 관리로 아키텍처 전환 필요

---

## 5. PHASE I.3: 외부 데이터 통합 (향후 단계)

**현 세션 범위 밖:**
- Gemini API 통합 (API 키 설정, 신규 서비스 필요)
- 뉴스 감성분석 (News API 구독 필요)
- 소셜미디어 (Reddit/Twitter API 제한, 복잡한 파싱)
- FRED 경제 지표 (매크로 수준, 종목별 아님)

**권고사항:**
- 별도 "Phase J: 외부 인텔리전스" 계획 수립
- 감성 분석 서비스용 MSA 설계 필요
- 구현 예산: 3-5일

---

## 6. 실행 전략 (토큰 최적화)

**우선순위:**
1. **Phase I.1 (방어)** - 프로덕션 안전성에 필수
   - 거래 수수료 누출 방지
   - 과매매 리스크 감소
   - 즉각적인 비즈니스 가치
2. **Phase H.3 (국면별 학습)** - AI 성능 향상
   - 국면별 모델링 활성화
   - 상당한 계산 시간 필요 (토큰 집약적 아님)
   - 백그라운드 태스크로 실행 가능

**구현 순서:**
```
1단계: 포지션 방어 구현 (Risk Manager + Trading Strategy)
2단계: 모의 포지션으로 방어 로직 테스트
3단계: 국면별 학습 구현
4단계: 4개 국면 모두에 대해 학습 태스크 실행
5단계: 4개 모델 파일 생성 검증
6단계: 문서화 및 로드맵 업데이트
```

**예상 토큰 사용량:**
- Phase I.1: ~15K 토큰 (코드 수정 + 테스트)
- Phase H.3: ~10K 토큰 (training.py 리팩토링)
- 문서화: ~5K 토큰 (보고서 + 로드맵)
- **총합:** ~30K 토큰 (예산 충분)

---

## 7. 성공 기준

**Phase I.1 (방어):**
- ✅ 포지션 최소 60분(4바) 보유
- ✅ 1.5% 미만 수익으로 청산 금지 (120분 제한 전)
- ✅ 청산 후 60분 쿨다운 강제 (Redis TTL)
- ✅ 로그에 "MIN_HOLD", "COOLDOWN" 메시지 표시
- ✅ PostgreSQL에 포지션 기록 영구 저장

**Phase H.3 (국면별 학습):**
- ✅ 4개 모델 파일 존재: `ensemble_model_{regime}.pkl`
- ✅ 각 모델이 국면별 데이터로 학습 (>1000 샘플)
- ✅ PredictorService가 4개 모델 모두 오류 없이 로드
- ✅ 거래 로그에 국면별 모델 선택 표시

---

## 8. 리스크 완화

**리스크 1: 국면 데이터 부족**
- **완화:** 샘플 <1000개 시 범용 모델 폴백
- **감지:** 학습 중 경고 로그

**리스크 2: 방어 규칙 과도하게 엄격**
- **완화:** 임계값 설정 가능 (환경 변수)
- **테스트:** 방어 규칙 활성화한 백테스트

**리스크 3: 스키마 마이그레이션 실패**
- **완화:** Alembic 마이그레이션 사용 (되돌리기 가능)
- **롤백:** 기존 RiskManager 로직 유지

---

## 9. 승인 후 다음 단계

1. **사용자 검토:** 본 계획 제시 (채팅에서 한국어 요약)
2. **승인 게이트:** "Y" 확인 대기
3. **위임:** 규칙과 함께 하위 에이전트 생성:
   - Backend Agent: Risk Manager + Trading Strategy 수정
   - Quant Agent: 국면별 학습 로직
4. **QA 루프:** `.agent/project_context.md` 기준으로 각 변경사항 검증
5. **최종 보고서:** 태스크 보고서 생성 (EN + KR)
6. **로드맵 업데이트:** Phase H.3 완료 표시, Phase I.1 항목 추가

---

## 10. 사용자 피드백 반영 사항

**Q1: Redis vs DB, 임계값 적정성?**
- A: PostgreSQL 주 저장소 (persistence), Redis는 쿨다운 캐시만
- A: 수익 임계값 0.5% → **1.5% 상향** (거래비용 5배 마진)
- A: 60분 보유 적정, ATR 동적 조정은 향후 옵션

**Q2: 다중 포지션 시스템?**
- A: 현재는 1종목씩 순차 거래 → 목표는 동시 다종목 보유 (AAPL+MSFT+GOOGL)
- A: MPT/Kelly는 포트폴리오 레벨 리스크 계산 필요
- A: 진행 시점: Phase I.1 완료 + 실거래 2주 후 (2026-01 중순~말)

**미결 질문:**
1. 국면별 보유 시간 차별화? (VOLATILE 30분, TRENDING 90분)
2. ATR 기반 동적 임계값? (변동성 높으면 2% 요구)

**권고:** 고정 파라미터로 시작, 실거래 데이터 2주 후 최적화.

---

**계획 종료**
