# Migration Plan: google-generativeai to google-genai + Feature Pipeline Fix

**Date:** 2026-01-06  
**Phase:** Code Quality & Bug Fix  
**Complexity:** Medium  
**Estimated Time:** 1 hour

---

## 1. OBJECTIVE

Fix two critical issues identified during handover:

### Issue 1: Deprecated Library (google-generativeai)
- `google-generativeai` is deprecated (confirmed by Context7)
- Migrate to `google-genai` (official unified SDK)
- Affects: sentiment_analyzer.py

### Issue 2: Training Pipeline Feature Column Mismatch
- `feature_columns` property includes Phase F features (sentiment, fundamentals)
- Training pipeline uses `create_features()` which only generates technical indicators
- Result: KeyError when training tries to access non-existent columns

**Root Cause Analysis (Issue 2):**
```
app/tasks/training.py:95
all_X.append(features_df[feature_engineer.feature_columns])

feature_engineer.feature_columns includes:
['rsi', 'macd', ... 'relative_volume', 'sentiment_score', 'pe_ratio', 'pb_ratio', 'roe', 'beta']

But features_df only contains:
['rsi', 'macd', ... 'relative_volume', 'vwap_distance', 'sector_id']
```

---

## 2. TECHNICAL APPROACH

### 2.1 google-genai Migration

#### Old Code (google-generativeai):
```python
import google.generativeai as genai

genai.configure(api_key=api_key)
self.gemini_model = genai.GenerativeModel('gemini-pro')

response = self.gemini_model.generate_content(prompt)
result = response.text
```

#### New Code (google-genai):
```python
from google import genai
from google.genai import types

client = genai.Client(api_key=api_key)

response = client.models.generate_content(
    model='gemini-2.0-flash-001',  # Updated model name
    contents=prompt
)
result = response.text
```

**Key Differences:**
1. Import: `from google import genai` (not `google.generativeai`)
2. Client-based API (not configure + GenerativeModel)
3. Model names updated (gemini-pro → gemini-2.0-flash-001 or gemini-2.5-flash)
4. `generate_content` method signature changed

### 2.2 Feature Pipeline Fix

**Solution: Separate feature column sets**

#### Add New Property (base_feature_columns):
```python
@property
def base_feature_columns(self) -> list:
    """
    Base technical indicators (for training).
    Excludes Phase F features (sentiment, fundamentals, relative_volume).
    """
    return [
        'rsi', 'macd', 'macd_signal', 'macd_hist',
        'bb_width', 'bb_position',
        'sma_20', 'sma_50', 'ema_12', 'ema_26',
        'atr_pct', 'adx',
        'stoch_k', 'stoch_d',
        'volume_ratio', 'roc', 'mom',
        'sector_id',  # Categorical
        'vwap_distance'  # Phase G
    ]
```

#### Keep Existing feature_columns (for live prediction):
```python
@property
def feature_columns(self) -> list:
    """
    Full feature set (for live prediction with sentiment/fundamentals).
    Includes all base features + Phase F enhancements.
    """
    return self.base_feature_columns + [
        'relative_volume',  # Cross-sectional
        'sentiment_score',  # Phase F.1
        'pe_ratio', 'pb_ratio', 'roe', 'beta'  # Phase F.2
    ]
```

#### Update Training Pipeline:
```python
# app/tasks/training.py:95
# OLD: all_X.append(features_df[feature_engineer.feature_columns])
# NEW:
all_X.append(features_df[feature_engineer.base_feature_columns])
```

**Rationale:**
- Training uses historical OHLCV data (no sentiment/fundamentals available)
- Live trading can add sentiment/fundamentals in real-time
- Models trained on base features, enhanced with Phase F features during prediction
- Phase F features become **signal modifiers** (not model inputs)

---

## 3. FILE CHANGES

### 3.1 app/services/sentiment_analyzer.py

**Changes:**
1. Line 14: Replace import statement
2. Lines 64-79: Replace `_init_gemini()` method
3. Lines 89-162: Update `analyze_sentiment()` method
4. Add new method: `_parse_gemini_response()`

**Affected Lines:**
- Import (Line 14)
- `_init_gemini()` (Lines 64-79)
- `analyze_sentiment()` (Lines 89-162)

### 3.2 app/ml/features.py

**Changes:**
1. Add new property: `base_feature_columns` (after line 310)
2. Update existing `feature_columns` property to reference base (line 310-324)

**Lines to Modify:**
- Lines 310-324: Split into two properties

### 3.3 app/tasks/training.py

**Changes:**
1. Line 95: Change `feature_engineer.feature_columns` → `feature_engineer.base_feature_columns`
2. Update logging to reflect base features usage

**Lines to Modify:**
- Line 95: Feature column selection

### 3.4 requirements.txt

**Changes:**
1. Remove: `google-generativeai>=0.3.0`
2. Add: `google-genai>=1.33.0`

---

## 4. TESTING STRATEGY

### 4.1 Library Migration Test
```python
# Test new google-genai client
from google import genai

client = genai.Client(api_key=os.getenv('GEMINI_API_KEY'))
response = client.models.generate_content(
    model='gemini-2.0-flash-001',
    contents='Test message'
)
assert response.text is not None
```

### 4.2 Feature Column Test
```python
# Verify base_feature_columns exists
feature_eng = FeatureEngineer()

base_cols = feature_eng.base_feature_columns
full_cols = feature_eng.feature_columns

# Base should NOT include Phase F features
assert 'sentiment_score' not in base_cols
assert 'pe_ratio' not in base_cols

# Full should include Phase F features
assert 'sentiment_score' in full_cols
assert 'pe_ratio' in full_cols
```

### 4.3 Training Pipeline Test
```python
# Mock test: Ensure training uses base_feature_columns
features_df = feature_engineer.create_features(sample_df)
available_cols = features_df.columns.tolist()

# Should NOT raise KeyError
X = features_df[feature_engineer.base_feature_columns]
assert X.shape[1] == len(feature_engineer.base_feature_columns)
```

---

## 5. RISKS & MITIGATION

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| google-genai API behavior change | Low | High | Test with sample prompts first |
| Training breaks due to column selection | Low | High | Add defensive column filtering |
| Sentiment analysis quality degradation | Medium | Medium | Compare outputs before/after |
| Model version (gemini-pro → 2.0-flash) changes results | Medium | Medium | Log sentiment scores for comparison |

### Rollback Plan
If issues arise:
1. Revert changes: `git revert <commit-hash>`
2. Restore `google-generativeai` in requirements.txt
3. Restore old import statements
4. Estimated rollback time: 10 minutes

---

## 6. VERIFICATION CHECKLIST

### Code Quality
- [ ] No unused imports (`grep` verification)
- [ ] No unused functions
- [ ] No unused parameters
- [ ] Type hints present for new/modified functions
- [ ] Docstrings updated

### Functionality
- [ ] Sentiment analysis still works (test with sample prompt)
- [ ] Training pipeline completes without KeyError
- [ ] Live prediction still uses full feature_columns
- [ ] Redis caching unchanged (uses same cache keys)

### Error Handling
- [ ] All API calls wrapped in try-except
- [ ] Logging statements present for errors
- [ ] Graceful degradation if GEMINI_API_KEY missing

---

## 7. ACCEPTANCE CRITERIA

- [ ] `google-genai` installed and working
- [ ] `google-generativeai` removed from requirements.txt
- [ ] Training pipeline uses `base_feature_columns`
- [ ] Live prediction uses full `feature_columns`
- [ ] No KeyError when training models
- [ ] Sentiment analysis produces valid scores (-1.0 to +1.0)
- [ ] All existing tests pass
- [ ] get_errors tool shows no syntax errors

---

## 8. POST-DEPLOYMENT NOTES

### Environment Variable Check
```bash
# Verify GEMINI_API_KEY is set
echo $GEMINI_API_KEY

# Test new library
python -c "from google import genai; print('OK')"
```

### Model Performance Monitoring
- Monitor sentiment scores after migration (should be similar)
- Compare training metrics (Sharpe, F1) before/after
- If degradation >10%, investigate model version change

### Documentation Updates
- Update README.md with new library name
- Add migration notes to CHANGELOG.md
- Update .env.example with GEMINI_API_KEY

---

## 9. CONTEXT7 MCP USAGE CONFIRMATION

**Question:** Did we use Context7 MCP?  
**Answer:** YES

**Evidence:**
1. Used `mcp_context7_resolve-library-id` to search for google-genai library
2. Used `mcp_context7_get-library-docs` to retrieve migration guide from `/googleapis/python-genai`
3. Retrieved 80+ code snippets for:
   - Client initialization
   - generate_content method usage
   - Authentication patterns
   - Error handling

**Value:**
- Confirmed google-generativeai is deprecated
- Found correct import patterns (`from google import genai`)
- Discovered model name updates (gemini-pro → gemini-2.0-flash-001)
- Obtained official migration examples

---

## 10. SQL LOG ANALYSIS (PENDING USER INPUT)

**Note:** User mentioned "SQL statements continuously appearing in logs" but did not provide log samples.

**Request:** Please provide log excerpt showing:
1. Full error messages
2. SQL statements being logged
3. Timestamps and frequency

**Likely Causes:**
1. **SQLAlchemy echo=True:** Debug mode enabled (shows all SQL)
2. **Excessive DB queries:** N+1 query problem
3. **Connection pool warnings:** Too many connections

**Next Steps After Log Review:**
- Analyze SQL patterns
- Identify unnecessary queries
- Optimize query batching
- Adjust logging levels

---

## 11. EXECUTION PLAN

### Phase 1: Preparation (10 min)
1. Backup current code (git commit)
2. Install google-genai: `pip install google-genai`
3. Verify installation

### Phase 2: Library Migration (20 min)
1. Update sentiment_analyzer.py
2. Test with sample prompt
3. Verify Redis caching works

### Phase 3: Feature Pipeline Fix (15 min)
1. Add base_feature_columns property
2. Update feature_columns property
3. Update training.py

### Phase 4: Testing (10 min)
1. Run unit tests
2. Test training pipeline with mock data
3. Verify get_errors shows no issues

### Phase 5: Documentation (5 min)
1. Update requirements.txt
2. Update roadmap
3. Commit changes

---

**Total Estimated Time:** 1 hour

