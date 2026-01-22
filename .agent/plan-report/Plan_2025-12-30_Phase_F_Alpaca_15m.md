# Plan: Phase F - Alpaca 15m Candle Integration

## 1. Goal
Upgrade the FastAPI Stock Trader system to utilize 15-minute interval OHLCV data from Alpaca for both historical backfilling and live trading. This marks the beginning of **Phase F (Advanced AI Capabilities)** by enhancing data granularity for more responsive strategies.

## 2. Scope
*   **Database**: Update `StockOHLCV` schema to support timeframes (15m vs 1d).
*   **Data Provider**: Modify `AlpacaDataProvider` to fetch 15-minute bars.
*   **Backfill**: Update `backfill_ohlcv.py` to handle 15-minute data ingestion.
*   **Strategies**: Ensure `FeatureEngineer` and `TradingStrategy` adapt to 15-minute cycles.
*   **Execution**: Verify the end-to-end flow from data fetch to DB storage.

## 3. Detailed Steps

### 3.1 Database Schema Update
*   **Target**: `app/domain/models/stock.py`
*   **Action**: Add `timeframe` column to `StockOHLCV`.
    *   Type: `String(10)` or `Enum` (e.g., '1m', '15m', '1d').
    *   Default: '1d' (to preserve legacy data interpretation if needed).
    *   Update Primary Key to include `timeframe` (Composite PK: `symbol`, `date_time`, `timeframe`).
*   *Note*: Since we are moving to 15m mainly, we might truncate the table and reload for clean state if user approves, or just support both. **Proposal: Support both, strictly use 15m for active trading.**

### 3.2 Data Provider & Backfill
*   **Target**: `app/services/data_provider.py`, `scripts/backfill_ohlcv.py`
*   **Action**:
    *   Update `get_historical_data` method to accept `timeframe` argument (default `TimeFrame.Day`).
    *   Map `15m` to Alpaca's `TimeFrame.Minute` with multiplier if needed (or `15Min`).
    *   Update `backfill_ohlcv.py` to request 15m data for the last 2 years.
    *   *Warning*: API limits might be hit faster with 15m data. Implement robust pagination/rate-limiting.

### 3.3 Feature Engineering & Training
*   **Target**: `app/ml/feature_engineer.py`, `app/ml/train_models.py`
*   **Action**:
    *   Validate that TA-Lib indicators work correctly with 15m data (they are agnostic, but window sizes might need tuning).
    *   Ensure training script reads 15m data from DB.

### 3.4 Trading Cycle
*   **Target**: `app/core/scheduler.py` (if applicable) or `main.py`
*   **Action**: Change Cron/Scheduler trigger from "Daily" to "Every 15 minutes".

## 4. Work Breakdown (Sub-Agents)
1.  **DB Agent**: Modify `StockOHLCV` model and generate Alembic migration (or raw SQL update).
2.  **Data Agent**: Update `AlpacaDataProvider` and `backfill_ohlcv.py`.
3.  **Core Agent**: Verify Feature Engineering and Training scripts.

## 5. Verification Plan
*   Run `backfill_ohlcv.py` for 1 symbol (e.g., 'AAPL') for 1 month.
*   Verify DB records exist with `timeframe='15m'`.
*   Run a mock training cycle.
