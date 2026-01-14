"""Add partial indexes for active position queries.

Revision ID: 004_add_partial_indexes
Revises: 003_remove_sentiment_fundamentals
Create Date: 2026-01-14

## Purpose
This migration adds PARTIAL INDEXES to optimize queries on active positions.

## What is a Partial Index?
A partial index is an index built over a subset of a table, defined by a conditional
expression (WHERE clause). Only rows matching the condition are indexed.

### Benefits:
1. **Smaller Index Size**: Only active positions (exit_time IS NULL) are indexed
2. **Faster Writes**: INSERT/UPDATE on closed positions don't update this index
3. **Faster Reads**: Active position lookups scan fewer index entries

### Example:
- Table has 100,000 position records (historical)
- Only 5 positions are active (exit_time IS NULL)
- Full index: scans 100,000 entries
- Partial index: scans 5 entries

### SQL Equivalent:
```sql
CREATE INDEX CONCURRENTLY idx_position_tracking_active_partial
ON position_tracking (symbol, entry_time)
WHERE exit_time IS NULL;
```
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '003_add_partial_indexes'
down_revision: Union[str, None] = '002_vwap_tracking'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add partial indexes for active position queries.
    
    Creates indexes with WHERE clause to only index active positions,
    dramatically improving query performance for trading operations.
    """
    # Partial index on position_tracking for active positions
    # This optimizes the frequent query: WHERE exit_time IS NULL
    op.create_index(
        'idx_position_tracking_active_partial',
        'position_tracking',
        ['symbol', 'entry_time'],
        unique=False,
        postgresql_where=sa.text('exit_time IS NULL'),
    )
    
    # Partial index on positions table for open positions
    # Optimizes: SELECT * FROM positions WHERE status = 'OPEN'
    op.create_index(
        'idx_positions_open_partial',
        'positions',
        ['symbol', 'entry_time'],
        unique=False,
        postgresql_where=sa.text("status = 'OPEN'"),
    )


def downgrade() -> None:
    """Remove partial indexes."""
    op.drop_index('idx_positions_open_partial', table_name='positions')
    op.drop_index('idx_position_tracking_active_partial', table_name='position_tracking')
