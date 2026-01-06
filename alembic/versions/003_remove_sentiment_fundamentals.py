"""Remove sentiment and fundamentals from stock_fundamentals table

Revision ID: 003_remove_sentiment_fundamentals
Revises: 002_vwap_and_position_tracking
Create Date: 2026-01-06

Rationale:
- Sentiment scores are volatile (1-hour TTL), stored in Redis only
- Fundamentals (PE, PB, ROE, Beta) fetched on-demand via yfinance with LRU cache
- No need to persist in DB (no historical analysis, no backfill)
- Reduces DB storage, indexing overhead, and query complexity
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '003_remove_sentiment_fundamentals'
down_revision = '002_vwap_tracking'
branch_labels = None
depends_on = None


def upgrade():
    """Remove sentiment and fundamental columns from stock_fundamentals table."""
    # Drop columns that are now fetched on-demand
    op.drop_column('stock_fundamentals', 'sentiment_score')
    op.drop_column('stock_fundamentals', 'per')
    op.drop_column('stock_fundamentals', 'pbr')
    op.drop_column('stock_fundamentals', 'roe')
    op.drop_column('stock_fundamentals', 'market_cap')
    op.drop_column('stock_fundamentals', 'sector')
    
    # Note: stock_fundamentals table might be empty now
    # Consider dropping the entire table if no other columns used


def downgrade():
    """Restore columns if rollback needed."""
    op.add_column('stock_fundamentals', sa.Column('sentiment_score', sa.Float(), nullable=True, comment="News/Social Sentiment (-1.0 to 1.0)"))
    op.add_column('stock_fundamentals', sa.Column('per', sa.Float(), nullable=True))
    op.add_column('stock_fundamentals', sa.Column('pbr', sa.Float(), nullable=True))
    op.add_column('stock_fundamentals', sa.Column('roe', sa.Float(), nullable=True))
    op.add_column('stock_fundamentals', sa.Column('market_cap', sa.Float(), nullable=True))
    op.add_column('stock_fundamentals', sa.Column('sector', sa.String(100), nullable=True))
