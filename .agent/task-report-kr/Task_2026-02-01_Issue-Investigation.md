# 작업 보고서: 이슈 조사 (6개 항목)
**날짜:** 2026-02-01

## 목적
사용자가 보고한 6가지 이슈 조사 및 해결: VIX 캐시, Discord 알림, 감성 캐시 정리, 피처 갯수 매칭, 감성/재무 데이터 활용 방식

---

## 1. VIX 캐시 이슈

### 문제
```
worker-training-1  | VIX 캐시 없음 (ATR 기반 레짐 감지 사용)
```
VIX 데이터가 성공적으로 캐시되었음에도 불구하고:
```
worker-data-1  | Redis VIX 캐시: 17.44 (source: yfinance)
```

### 근본 원인
**저장 측과 읽기 측 간 데이터 형식 불일치:**

- **저장 측** ([vix_data.py#L102-L106](app/tasks/vix_data.py#L102-L106)): plain string으로 저장
  ```python
  redis_client.setex('vix:latest', 86400, str(latest_vix_value))
  ```

- **읽기 측** ([training.py#L173-L177](app/tasks/training.py#L173-L177)): JSON 기대하는 `cache.get()` 사용
  ```python
  from app.core.cache import cache
  vix_cached = cache.get("vix:latest")
  ```

- **캐시 서비스** ([cache.py#L44-L47](app/core/cache.py#L44-L47)): JSON으로 파싱
  ```python
  value = self.redis_client.get(key)
  if value:
      return json.loads(value)  # "17.44" 문자열에서 실패
  ```

### 해결책
`training.py`에서 이미 존재하는 `get_latest_vix()` 함수를 사용하도록 수정 필요.

---

## 2. Discord Webhook 알림 이슈

### 문제
에러 발생 시 Discord 알림이 오지 않음.

### 분석
- 데코레이터 순서 정상: `@celery_app.task` 다음 `@notify_on_failure`
- `notify_on_failure` 데코레이터 구현 정상
- 예외 발생 후 알림 전송 후 다시 raise

### 가능한 원인
1. **DISCORD_WEBHOOK_URL 미설정**: URL이 없으면 경고 로그 출력
2. **실제 에러 미발생**: 태스크가 성공하면 알림 전송 안 함

### 해결책
1. `.env` 또는 Docker 환경에서 `DISCORD_WEBHOOK_URL` 설정 확인
2. "Discord webhook not configured" 로그 메시지 확인
3. 수동 테스트: 의도적으로 에러 발생시켜 알림 작동 확인

---

## 3. 감성 캐시 삭제 카운트 = 0

### 문제
```
worker-data-1  | 감성 캐시 항목이 없습니다
worker-data-1  | deleted_count: 0
```

### 근본 원인
`clear_stale_sentiment_cache` 태스크는 **TTL = -1** (만료 미설정) 키만 삭제:
```python
for key in keys:
    ttl = analyzer.redis_client.ttl(key)
    if ttl == -1:  # 만료 미설정인 경우만 삭제
        analyzer.redis_client.delete(key)
```

`cache_sentiment()`가 캐싱 시 TTL을 제대로 설정하므로 TTL=-1인 키가 없음.

### 결론
**예상된 동작이며 버그가 아님.** Redis가 TTL에 따라 자동으로 키를 만료시킴.

---

## 4. 피처 갯수 매칭 (학습 vs 추론)

### 분석

| 컴포넌트 | Feature Set | 갯수 |
|----------|-------------|------|
| 학습 ([training.py](app/tasks/training.py)) | 기본값 (legacy) | 25 |
| 추론 ([trading_strategy_sync.py#L210](app/services/trading_strategy_sync.py#L210)) | `feature_set="legacy"` | 25 |

**legacy set의 피처:**
- Core technical (17개): rsi, macd, macd_signal, macd_hist, bb_width, bb_position, sma_20, sma_50, ema_12, ema_26, atr_pct, adx, stoch_k, stoch_d, volume_ratio, roc, mom
- Cross-sectional (2개): sector_id, relative_volume
- VWAP & liquidity (2개): vwap_distance, trade_intensity
- Phase F (4개): sentiment_score, pe_ratio, pb_ratio, roe

### 결론
**피처 갯수 일치함.** 학습과 추론 모두 25개 피처 사용.

---

## 5. 감성 및 재무 데이터의 거래 활용 방식

### 사용자 요청
이전 요청은 감성/재무 데이터를 ML 피처에 포함하지 말라는 것이었음.

### 현재 상태
**이슈:** `legacy` feature set에 Phase F 피처가 포함됨:
```python
# features.py legacy_feature_columns
'sentiment_score',
'pe_ratio', 'pb_ratio', 'roe',
```

### 권장사항
감성/재무 데이터를 ML 학습에서 제외하려면:
1. `legacy` 대신 `feature_set="core"` (21개 피처) 사용
2. training.py와 trading_strategy_sync.py 모두 업데이트

`core` feature set은 Phase F 제외:
```python
# 21개 core technical features만
# sentiment_score, pe_ratio, pb_ratio, roe, beta 없음
```

---

## 6. Rules 디렉토리 구조

### 현재 Rules
| 파일 | 역할 | 설명 |
|------|------|------|
| [role-backend.md](.agent/rules/role-backend.md) | Backend Developer | FastAPI, DB, Docker, 보안 |
| [role-quant.md](.agent/rules/role-quant.md) | Quant Analyst | 전략, 백테스팅, ML 모델 |
| [role-trading.md](.agent/rules/role-trading.md) | Trading Logic | 학습 및 실행 로직 |
| [role-pm.md](.agent/rules/role-pm.md) | PM (영문) | 프로젝트 관리 워크플로우 |
| [role-pm-kr.md](.agent/rules/role-pm-kr.md) | PM (한글) | 한글 버전 |

### 평가
Rules가 최소한이지만 기능적임. 추가 고려사항:
- 테스트 중심의 QA Engineer 역할
- 더 상세한 제약조건 및 검증 체크리스트

---

## 필요한 수정 요약

| 이슈 | 우선순위 | 필요한 수정 |
|------|----------|-------------|
| VIX 캐시 | 높음 | training.py에서 `get_latest_vix()` 사용 |
| Discord | 중간 | DISCORD_WEBHOOK_URL 환경변수 확인 |
| 감성 캐시 | 낮음 | 수정 불필요 (예상된 동작) |
| 피처 갯수 | 해당없음 | 이미 일치 |
| Phase F 피처 | 중간 | 제외 원하면 `core` feature set으로 전환 |

---

## 실행 시간
단일 세션에서 분석 완료.
