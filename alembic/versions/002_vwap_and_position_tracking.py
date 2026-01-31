"""Add VWAP, trade_count, and position_tracking table

Revision ID: 002_vwap_tracking
Revises: 001_timescaledb_setup
Create Date: 2026-01-05

"""
import sqlalchemy as sa

from alembic import op

revision = '002_vwap_tracking'
down_revision = '001_timescaledb_setup'
branch_labels = None
depends_on = None


def upgrade():
    # 1. Add VWAP and trade_count to stock_ohlcv
    op.add_column('stock_ohlcv', sa.Column('vwap', sa.Float(), nullable=True))
    op.add_column('stock_ohlcv', sa.Column('trade_count', sa.Integer(), nullable=True))

    # 2. Create position_tracking table
    op.create_table(
        'position_tracking',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('stock_tickers.symbol'), nullable=False),
        sa.Column('entry_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('exit_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('regime', sa.String(50), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now())
    )

    # Indexes for position_tracking
    op.create_index('ix_position_tracking_symbol', 'position_tracking', ['symbol'])
    op.create_index('ix_position_tracking_active', 'position_tracking', ['symbol', 'exit_time'])
    op.create_index('ix_position_tracking_entry_time', 'position_tracking', ['entry_time'])
    op.create_index('ix_position_tracking_regime', 'position_tracking', ['regime'])


def downgrade():
    # Drop position_tracking
    op.drop_index('ix_position_tracking_regime')
    op.drop_index('ix_position_tracking_entry_time')
    op.drop_index('ix_position_tracking_active')
    op.drop_index('ix_position_tracking_symbol')
    op.drop_table('position_tracking')

    # Drop VWAP columns
    op.drop_column('stock_ohlcv', 'trade_count')
    op.drop_column('stock_ohlcv', 'vwap')
