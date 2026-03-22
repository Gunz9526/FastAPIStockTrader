"""Add risk management columns to position_tracking table.

Revision ID: 004_risk_columns
Revises: 003_add_partial_indexes
"""
from alembic import op
import sqlalchemy as sa

revision = "004_risk_columns"
down_revision = "003_add_partial_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add stop_loss_price, take_profit_price, trailing_stop_price columns."""
    op.add_column("position_tracking", sa.Column("stop_loss_price", sa.Float(), nullable=True))
    op.add_column("position_tracking", sa.Column("take_profit_price", sa.Float(), nullable=True))
    op.add_column("position_tracking", sa.Column("trailing_stop_price", sa.Float(), nullable=True))


def downgrade() -> None:
    """Remove risk management columns."""
    op.drop_column("position_tracking", "trailing_stop_price")
    op.drop_column("position_tracking", "take_profit_price")
    op.drop_column("position_tracking", "stop_loss_price")
