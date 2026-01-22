# Implementation Plan - Phase F.1: Backfill Fix & RAG Interface

## Goal Description
1.  **Fix 15m Backfill**: Solve the data ingestion issue causing 0 bars inserted.
2.  **RAG Interface (MSA)**: Expose backend data to an external RAG service via API, avoiding internal RAG logic.
3.  **Sentiment Analysis**: Add `sentiment_score` to the data model to support future AI regimes.

## Proposed Changes

### 1. Fix 15m Backfill (Immediate Priority)
#### [MODIFY] [backfill_ohlcv.py](file:///f:/Work/FastAPIStockTrader/scripts/backfill_ohlcv.py)
- **Problem**: Likely incorrect `TimeFrame` object usage or empty response handling.
- **Fix**: 
    - Verify `TimeFrame(15, TimeFrameUnit.Minute)` implementation.
    - Add explicit error logging for Alpaca API response.
    - **Optimization**: Check if `IEX` feed supports 15m for free tier (Alpaca constraint).

### 2. Feature Engineering: Sentiment
#### [MODIFY] [app/domain/models/stock.py](file:///f:/Work/FastAPIStockTrader/app/domain/models/stock.py)
- Add `sentiment_score` (Float) column to `StockFundamentals`.
- Rationale: Storing sentiment alongside fundamentals allows "Smart Filtering" (Roadmap F.2).

### 3. RAG Interface API (External Service Support)
#### [NEW] [app/api/v1/endpoints/rag.py](file:///f:/Work/FastAPIStockTrader/app/api/v1/endpoints/rag.py)
- **GET /rag/context/{symbol}**: Returns JSON with:
    - Recent OHLCV summary (last 5 days)
    - Key Technical Indicators (RSI, SMA)
    - Fundamentals & Sentiment Score
- **GET /rag/portfolio**: Returns current holdings and performance.

## Verification Plan
1.  **Backfill**: Run `python scripts/backfill_ohlcv.py`. Success = >0 bars inserted.
2.  **API**: `curl localhost:8000/api/v1/rag/context/AAPL` returns valid JSON.
