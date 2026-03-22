"""Extend trade_logs with fill price and ML metadata.

Revision ID: 005
Revises: 004
Create Date: 2026-03-04
"""
from alembic import op
import sqlalchemy as sa

revision = '005_extend_trade_logs'
down_revision = '004_risk_columns'
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column('trade_logs', sa.Column('fill_price', sa.Float(), nullable=True, comment='Actual Alpaca filled_avg_price'))
    op.add_column('trade_logs', sa.Column('commission', sa.Float(), nullable=True, server_default='0.0', comment='Trading commission/fees'))
    op.add_column('trade_logs', sa.Column('regime', sa.String(50), nullable=True, comment='Market regime at trade time'))
    op.add_column('trade_logs', sa.Column('confidence', sa.Float(), nullable=True, comment='ML prediction confidence'))
    op.add_column('trade_logs', sa.Column('predicted_class', sa.Integer(), nullable=True, comment='ML predicted class 0/1/2'))
    op.add_column('trade_logs', sa.Column('entry_trade_id', sa.Integer(), nullable=True, comment='FK to BUY trade for SELL orders'))

def downgrade() -> None:
    op.drop_column('trade_logs', 'entry_trade_id')
    op.drop_column('trade_logs', 'predicted_class')
    op.drop_column('trade_logs', 'confidence')
    op.drop_column('trade_logs', 'regime')
    op.drop_column('trade_logs', 'commission')
    op.drop_column('trade_logs', 'fill_price')
