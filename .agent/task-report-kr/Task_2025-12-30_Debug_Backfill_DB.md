# Task Report: Debugging Backfill DB Insertion Error

## 🚨 Issue Diagnosis
**Symptom**: `Expected error: ROLLBACK` during `backfill_ohlcv.py` execution.
**Log Analysis**: The logs showed `INSERT INTO stock_ohlcv (id, ...)` with `id` as `$1` (None). But the `id` column was failing to auto-generate a value because it was no longer the Primary Key (due to 15m composite PK change), and PostgreSQL `SERIAL` behavior is tied to PKs by default in some ORM configurations.

## 🛠️ Fix Implementation
### 1. Model Definition Update
- **File**: `app/domain/models/stock.py`
- **Change**: Explicitly attached a `Sequence('stock_ohlcv_id_seq')` to the `id` column.
- **Effect**: Forces SQLAlchemy to use the sequence `nextval('stock_ohlcv_id_seq')` even if `id` is not the Primary Key.

### 2. Migration Script Update
- **File**: `scripts/migrate_db_for_15m.py`
- **Change**: Added raw SQL commands to:
    - `DROP SEQUENCE IF EXISTS stock_ohlcv_id_seq`
    - `CREATE SEQUENCE stock_ohlcv_id_seq`
- **Effect**: Ensures the database sequence object exists before the table attempts to use it.

## 🔍 Verification (Server Side)
Please run the migration script again to apply the Sequence fix:
1.  `python scripts/migrate_db_for_15m.py`
    - Check log for: `CREATE SEQUENCE stock_ohlcv_id_seq`
2.  `python scripts/backfill_ohlcv.py`
    - Confirm `Total inserted > 0` and no more `ROLLBACK`.
