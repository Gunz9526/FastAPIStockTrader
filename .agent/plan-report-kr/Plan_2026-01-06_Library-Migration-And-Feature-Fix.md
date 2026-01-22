# 마이그레이션 계획: google-generativeai → google-genai + 피처 파이프라인 수정

**날짜:** 2026-01-06  
**단계:** 코드 품질 및 버그 수정  
**복잡도:** 중간  
**예상 소요 시간:** 1시간

---

## 1. 목표

핸드오버에서 식별된 두 가지 중요한 문제 해결:

### 문제 1: Deprecated 라이브러리 (google-generativeai)
- `google-generativeai`는 deprecated됨 (Context7 확인)
- `google-genai` (공식 통합 SDK)로 마이그레이션
- 영향: sentiment_analyzer.py

### 문제 2: 훈련 파이프라인 피처 컬럼 불일치
- `feature_columns` 속성에 Phase F 피처(sentiment, fundamentals) 포함
- 훈련 파이프라인은 `create_features()` 사용 (기술 지표만 생성)
- 결과: 훈련 시 존재하지 않는 컬럼 접근 시 KeyError 발생

**근본 원인 분석 (문제 2):**
```
app/tasks/training.py:95
all_X.append(features_df[feature_engineer.feature_columns])

feature_engineer.feature_columns 포함:
['rsi', 'macd', ... 'relative_volume', 'sentiment_score', 'pe_ratio', 'pb_ratio', 'roe', 'beta']

하지만 features_df는 다음만 포함:
['rsi', 'macd', ... 'relative_volume', 'vwap_distance', 'sector_id']
```

---

## 2. 기술적 접근 방법

### 2.1 google-genai 마이그레이션

#### 기존 코드 (google-generativeai):
```python
import google.generativeai as genai

genai.configure(api_key=api_key)
self.gemini_model = genai.GenerativeModel('gemini-pro')

response = self.gemini_model.generate_content(prompt)
result = response.text
```

#### 신규 코드 (google-genai):
```python
from google import genai
from google.genai import types

client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model='gemini-2.0-flash-001',  # 업데이트된 모델명
    contents=prompt
)
result = response.text
```

**주요 차이점:**
1. Import: `from google import genai` (not `google.generativeai`)
2. Client 기반 API (configure + GenerativeModel 방식 제거)
3. 모델명 업데이트 (gemini-pro → gemini-2.0-flash-001 또는 gemini-2.5-flash)
4. `generate_content` 메서드 시그니처 변경

### 2.2 피처 파이프라인 수정

**해결책: 피처 컬럼 세트 분리**

#### 새 속성 추가 (base_feature_columns):
```python
@property
def base_feature_columns(self) -> list:
    """
    기본 기술 지표 (훈련용).
    Phase F 피처 제외 (sentiment, fundamentals, relative_volume).
    """
    return [
        'rsi', 'macd', 'macd_signal', 'macd_hist',
        'bb_width', 'bb_position',
        'sma_20', 'sma_50', 'ema_12', 'ema_26',
        'atr_pct', 'adx',
        'stoch_k', 'stoch_d',
        'volume_ratio', 'roc', 'mom',
        'sector_id',  # 카테고리
        'vwap_distance'  # Phase G
    ]
```

#### 기존 feature_columns 유지 (실시간 예측용):
```python
@property
def feature_columns(self) -> list:
    """
    전체 피처 세트 (sentiment/fundamentals 포함한 실시간 예측용).
    모든 기본 피처 + Phase F 개선 사항 포함.
    """
    return self.base_feature_columns + [
        'relative_volume',  # Cross-sectional
        'sentiment_score',  # Phase F.1
        'pe_ratio', 'pb_ratio', 'roe', 'beta'  # Phase F.2
    ]
```

#### 훈련 파이프라인 업데이트:
```python
# app/tasks/training.py:95
# 기존: all_X.append(features_df[feature_engineer.feature_columns])
# 변경:
all_X.append(features_df[feature_engineer.base_feature_columns])
```

**논리적 근거:**
- 훈련은 과거 OHLCV 데이터 사용 (sentiment/fundamentals 없음)
- 실시간 거래는 sentiment/fundamentals를 실시간으로 추가 가능
- 모델은 기본 피처로 훈련, 예측 시 Phase F 피처로 강화
- Phase F 피처는 **신호 수정자** 역할 (모델 입력 아님)

---

## 3. 파일 변경 사항

### 3.1 app/services/sentiment_analyzer.py

**변경 사항:**
1. Line 14: import 문 교체
2. Lines 64-79: `_init_gemini()` 메서드 교체
3. Lines 89-162: `analyze_sentiment()` 메서드 업데이트
4. 새 메서드 추가: `_parse_gemini_response()`

**영향받는 라인:**
- Import (Line 14)
- `_init_gemini()` (Lines 64-79)
- `analyze_sentiment()` (Lines 89-162)

### 3.2 app/ml/features.py

**변경 사항:**
1. 새 속성 추가: `base_feature_columns` (line 310 이후)
2. 기존 `feature_columns` 속성을 base 참조로 업데이트 (line 310-324)

**수정할 라인:**
- Lines 310-324: 두 개의 속성으로 분리

### 3.3 app/tasks/training.py

**변경 사항:**
1. Line 95: `feature_engineer.feature_columns` → `feature_engineer.base_feature_columns`로 변경
2. 로깅 업데이트하여 base features 사용 반영

**수정할 라인:**
- Line 95: 피처 컬럼 선택

### 3.4 requirements.txt

**변경 사항:**
1. 제거: `google-generativeai>=0.3.0`
2. 추가: `google-genai>=1.33.0`

---

## 4. 테스트 전략

### 4.1 라이브러리 마이그레이션 테스트
```python
# 새 google-genai client 테스트
from google import genai

client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
response = client.models.generate_content(
    model='gemini-2.0-flash-001',
    contents='Test message'
)
assert response.text is not None
```

### 4.2 피처 컬럼 테스트
```python
# base_feature_columns 존재 확인
feature_eng = FeatureEngineer()

base_cols = feature_eng.base_feature_columns
full_cols = feature_eng.feature_columns

# Base는 Phase F 피처를 포함하지 않아야 함
assert 'sentiment_score' not in base_cols
assert 'pe_ratio' not in base_cols

# Full은 Phase F 피처를 포함해야 함
assert 'sentiment_score' in full_cols
assert 'pe_ratio' in full_cols
```

### 4.3 훈련 파이프라인 테스트
```python
# Mock 테스트: 훈련이 base_feature_columns 사용하는지 확인
features_df = feature_engineer.create_features(sample_df)
available_cols = features_df.columns.tolist()

# KeyError가 발생하지 않아야 함
X = features_df[feature_engineer.base_feature_columns]
assert X.shape[1] == len(feature_engineer.base_feature_columns)
```

---

## 5. 위험 및 완화 방안

| 위험 | 확률 | 영향도 | 완화 방안 |
|------|------|--------|----------|
| google-genai API 동작 변경 | 낮음 | 높음 | 샘플 프롬프트로 먼저 테스트 |
| 컬럼 선택으로 인한 훈련 중단 | 낮음 | 높음 | 방어적 컬럼 필터링 추가 |
| 감성 분석 품질 저하 | 중간 | 중간 | 전후 출력 비교 |
| 모델 버전 (gemini-pro → 2.0-flash) 결과 변경 | 중간 | 중간 | 감성 점수 로그 비교 |

### 롤백 계획
문제 발생 시:
1. 변경 사항 되돌리기: `git revert <commit-hash>`
2. requirements.txt에 `google-generativeai` 복원
3. 기존 import 문 복원
4. 예상 롤백 시간: 10분

---

## 6. 검증 체크리스트

### 코드 품질
- [ ] 사용되지 않는 import 없음 (`grep` 검증)
- [ ] 사용되지 않는 함수 없음
- [ ] 사용되지 않는 파라미터 없음
- [ ] 새/수정된 함수에 타입 힌트 존재
- [ ] Docstring 업데이트

### 기능성
- [ ] 감성 분석 여전히 작동 (샘플 프롬프트로 테스트)
- [ ] 훈련 파이프라인이 KeyError 없이 완료
- [ ] 실시간 예측이 여전히 전체 feature_columns 사용
- [ ] Redis 캐싱 변경 없음 (동일한 cache key 사용)

### 에러 처리
- [ ] 모든 API 호출이 try-except로 감싸짐
- [ ] 에러에 대한 로깅 문 존재
- [ ] GEMINI_API_KEY 누락 시 graceful degradation

---

## 7. 완료 기준

- [ ] `google-genai` 설치 및 작동
- [ ] requirements.txt에서 `google-generativeai` 제거
- [ ] 훈련 파이프라인이 `base_feature_columns` 사용
- [ ] 실시간 예측이 전체 `feature_columns` 사용
- [ ] 모델 훈련 시 KeyError 없음
- [ ] 감성 분석이 유효한 점수 생성 (-1.0 ~ +1.0)
- [ ] 기존 테스트 모두 통과
- [ ] get_errors 도구에서 문법 에러 없음

---

## 8. 배포 후 노트

### 환경 변수 확인
```bash
# GEMINI_API_KEY 설정 확인
echo $GEMINI_API_KEY

# 새 라이브러리 테스트
python -c "from google import genai; print('OK')"
```

### 모델 성능 모니터링
- 마이그레이션 후 감성 점수 모니터링 (유사해야 함)
- 전후 훈련 메트릭 (Sharpe, F1) 비교
- 저하 >10%인 경우 모델 버전 변경 조사

### 문서 업데이트
- README.md에 새 라이브러리명 업데이트
- CHANGELOG.md에 마이그레이션 노트 추가
- .env.example에 GEMINI_API_KEY 업데이트

---

## 9. CONTEXT7 MCP 사용 확인

**질문:** Context7 MCP를 사용했는가?  
**답변:** 예 (YES)

**증거:**
1. `mcp_context7_resolve-library-id`를 사용하여 google-genai 라이브러리 검색
2. `mcp_context7_get-library-docs`를 사용하여 `/googleapis/python-genai`에서 마이그레이션 가이드 조회
3. 다음을 위한 80개 이상의 코드 스니펫 조회:
   - Client 초기화
   - generate_content 메서드 사용
   - 인증 패턴
   - 에러 처리

**가치:**
- google-generativeai가 deprecated됨을 확인
- 올바른 import 패턴 발견 (`from google import genai`)
- 모델명 업데이트 발견 (gemini-pro → gemini-2.0-flash-001)
- 공식 마이그레이션 예제 확보

---

## 10. SQL 로그 분석 (사용자 입력 대기)

**참고:** 사용자가 "로그에 SQL 문이 계속 표시됨"을 언급했지만 로그 샘플을 제공하지 않음.

**요청:** 다음을 보여주는 로그 발췌 제공:
1. 전체 에러 메시지
2. 로그되는 SQL 문
3. 타임스탬프 및 빈도

**가능한 원인:**
1. **SQLAlchemy echo=True:** 디버그 모드 활성화 (모든 SQL 표시)
2. **과도한 DB 쿼리:** N+1 쿼리 문제
3. **연결 풀 경고:** 너무 많은 연결

**로그 검토 후 다음 단계:**
- SQL 패턴 분석
- 불필요한 쿼리 식별
- 쿼리 배치 최적화
- 로깅 레벨 조정

---

## 11. 실행 계획

### Phase 1: 준비 (10분)
1. 현재 코드 백업 (git commit)
2. google-genai 설치: `pip install google-genai`
3. 설치 확인

### Phase 2: 라이브러리 마이그레이션 (20분)
1. sentiment_analyzer.py 업데이트
2. 샘플 프롬프트로 테스트
3. Redis 캐싱 작동 확인

### Phase 3: 피처 파이프라인 수정 (15분)
1. base_feature_columns 속성 추가
2. feature_columns 속성 업데이트
3. training.py 업데이트

### Phase 4: 테스트 (10분)
1. 단위 테스트 실행
2. Mock 데이터로 훈련 파이프라인 테스트
3. get_errors에서 문제 없음 확인

### Phase 5: 문서화 (5분)
1. requirements.txt 업데이트
2. 로드맵 업데이트
3. 변경 사항 커밋

---

**총 예상 시간:** 1시간

