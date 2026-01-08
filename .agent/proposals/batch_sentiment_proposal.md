# Batch Sentiment Analysis Proposal

## Problem
- Current: 1 Gemini API call per symbol (20 symbols = 20 calls)
- Result: API quota exceeded, slow processing

## Solution: Batch Processing

### Implementation

#### 1. Batch News Collection
```python
def _fetch_news_batch(symbols: List[str]) -> Dict[str, str]:
    """Fetch news for all symbols at once"""
    finnhub_client = finnhub.Client(api_key=api_key)
    news_batch = {}
    
    for symbol in symbols:
        articles = finnhub_client.company_news(symbol, _from=yesterday, to=today)
        news_batch[symbol] = _aggregate_news(articles)
    
    return news_batch
```

#### 2. Batch Sentiment Analysis (Single Gemini Call)
```python
def analyze_news_batch(self, symbol_news: Dict[str, str]) -> Dict[str, float]:
    """Analyze multiple symbols in ONE API call"""
    
    # Construct batch prompt
    batch_prompt = """
    You are a financial sentiment analyst. Analyze news for multiple stocks.
    
    For each stock, provide sentiment score from -1.0 to +1.0.
    
    News data:
    """
    
    for symbol, news in symbol_news.items():
        batch_prompt += f"\n\n[{symbol}]\n{news[:500]}"  # Limit per symbol
    
    batch_prompt += """
    
    Response format (JSON only):
    {
        "AAPL": {"score": 0.75, "reasoning": "..."},
        "MSFT": {"score": 0.45, "reasoning": "..."},
        ...
    }
    """
    
    response = self.gemini_client.models.generate_content(
        model='gemini-2.0-flash-exp',  # Larger context window
        contents=batch_prompt
    )
    
    return json.loads(response.text)
```

#### 3. Update Task Logic
```python
@celery_app.task(name="app.tasks.sentiment.update_sentiment_scores")
def update_sentiment_scores(self):
    # 1. Fetch news for all symbols (Finnhub API)
    news_batch = _fetch_news_batch(symbols)
    
    # 2. Analyze ALL symbols in ONE Gemini call
    scores = analyzer.analyze_news_batch(news_batch)
    
    # 3. Cache results
    for symbol, data in scores.items():
        analyzer.cache_sentiment(symbol, data['score'])
    
    logger.info(f"Batch sentiment update: {len(scores)} symbols, 1 API call")
```

## Performance Comparison

| Metric | Current (Individual) | Proposed (Batch) |
|--------|---------------------|------------------|
| Gemini API calls | 20 | 1 |
| Processing time | ~40s (2s × 20) | ~5s (single call) |
| API quota usage | 20 requests/hour | 1 request/hour |
| Rate limit risk | High | Low |

## Limitations

1. **Context Window**: Gemini 2.0 Flash Exp has 1M token limit
   - 20 symbols × 500 chars news = ~10K chars (~3K tokens)
   - Safe for up to 100 symbols

2. **Error Handling**: If batch fails, all symbols fail
   - Solution: Implement fallback to individual calls for failed batch

3. **Cache Miss Handling**: If only 1 symbol needs update, still calls batch
   - Solution: Collect symbols needing update first, batch only if > 5 symbols

## Recommendation

**Implement batch processing** with hybrid strategy:
- If ≤ 5 symbols need update → Individual calls
- If > 5 symbols need update → Batch call

Expected quota reduction: **80-95%**
