"""Enable TimescaleDB and create all tables

Revision ID: 001_timescaledb_setup
Revises: 
Create Date: 2025-12-29

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '001_timescaledb_setup'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    # Enable TimescaleDB extension
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
    
    # Create stock_tickers table
    op.create_table(
        'stock_tickers',
        sa.Column('symbol', sa.String(20), primary_key=True),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('market', sa.String(20), nullable=False),
        sa.Column('sector', sa.String(100), nullable=True),
        sa.Column('is_active', sa.Boolean(), default=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now())
    )
    op.create_index('ix_stock_tickers_symbol', 'stock_tickers', ['symbol'])
    
    # Create stock_ohlcv table
    # Note: Primary key includes date_time (partitioning column) for TimescaleDB compatibility
    op.create_table(
        'stock_ohlcv',
        sa.Column('id', sa.Integer(), autoincrement=True),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('stock_tickers.symbol'), nullable=False),
        sa.Column('date_time', sa.DateTime(timezone=True), nullable=False),
        sa.Column('open', sa.Float(), nullable=False),
        sa.Column('high', sa.Float(), nullable=False),
        sa.Column('low', sa.Float(), nullable=False),
        sa.Column('close', sa.Float(), nullable=False),
        sa.Column('volume', sa.Float(), nullable=False),
        sa.Column('adj_close', sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint('id', 'date_time')
    )
    op.create_index('ix_stock_ohlcv_symbol', 'stock_ohlcv', ['symbol'])
    op.create_index('ix_stock_ohlcv_date_time', 'stock_ohlcv', ['date_time'])
    
    # Create corporate_actions table
    op.create_table(
        'corporate_actions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('stock_tickers.symbol'), nullable=False),
        sa.Column('action_type', sa.String(50), nullable=False),
        sa.Column('execution_date', sa.Date(), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('applied_date', sa.DateTime(timezone=True), server_default=sa.func.now())
    )
    op.create_index('ix_corporate_actions_symbol', 'corporate_actions', ['symbol'])
    
    # Create stock_fundamentals table
    op.create_table(
        'stock_fundamentals',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('stock_tickers.symbol'), nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('per', sa.Float(), nullable=True),
        sa.Column('pbr', sa.Float(), nullable=True),
        sa.Column('roe', sa.Float(), nullable=True),
        sa.Column('market_cap', sa.Float(), nullable=True),
        sa.Column('sector', sa.String(100), nullable=True)
    )
    op.create_index('ix_stock_fundamentals_symbol', 'stock_fundamentals', ['symbol'])
    
    # Create portfolio_status table
    op.create_table(
        'portfolio_status',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.String(50), nullable=False),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('stock_tickers.symbol'), nullable=False),
        sa.Column('avg_price', sa.Float(), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('current_price', sa.Float(), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now())
    )
    op.create_index('ix_portfolio_status_user_id', 'portfolio_status', ['user_id'])
    
    # Create positions table
    op.create_table(
        'positions',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('stock_tickers.symbol'), nullable=False),
        sa.Column('entry_price', sa.Float(), nullable=False),
        sa.Column('entry_time', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('initial_qty', sa.Integer(), nullable=False),
        sa.Column('current_qty', sa.Integer(), nullable=False),
        sa.Column('current_price', sa.Float(), nullable=True),
        sa.Column('stop_loss_price', sa.Float(), nullable=True),
        sa.Column('take_profit_price', sa.Float(), nullable=True),
        sa.Column('trailing_stop_price', sa.Float(), nullable=True),
        sa.Column('realized_pl', sa.Float(), default=0.0),
        sa.Column('unrealized_pl', sa.Float(), nullable=True),
        sa.Column('status', sa.String(20), default='OPEN'),
        sa.Column('exit_price', sa.Float(), nullable=True),
        sa.Column('exit_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now())
    )
    op.create_index('ix_positions_symbol', 'positions', ['symbol'])
    op.create_index('idx_positions_symbol_status', 'positions', ['symbol', 'status'])
    
    # Create trade_logs table
    op.create_table(
        'trade_logs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('symbol', sa.String(20), sa.ForeignKey('stock_tickers.symbol'), nullable=False),
        sa.Column('action', sa.String(10), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('price', sa.Float(), nullable=False),
        sa.Column('order_id', sa.String(100), nullable=True),
        sa.Column('execution_time', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('strategy_name', sa.String(50), nullable=True),
        sa.Column('signal_strength', sa.Float(), nullable=True),
        sa.Column('realized_pl', sa.Float(), nullable=True)
    )
    op.create_index('ix_trade_logs_symbol', 'trade_logs', ['symbol'])
    op.create_index('ix_trade_logs_execution_time', 'trade_logs', ['execution_time'])
    op.create_index('idx_trades_symbol_time', 'trade_logs', ['symbol', 'execution_time'])
    
    # Convert stock_ohlcv to hypertable (table now exists)
    op.execute("""
        SELECT create_hypertable(
            'stock_ohlcv',
            'date_time',
            chunk_time_interval => INTERVAL '7 days',
            if_not_exists => TRUE
        );
    """)
    
    # Create optimized index for query performance
    op.execute("""
        CREATE INDEX IF NOT EXISTS idx_ohlcv_symbol_time 
        ON stock_ohlcv (symbol, date_time DESC);
    """)
    
    # Create continuous aggregate for daily bars (for performance optimization)
    op.execute("""
        CREATE MATERIALIZED VIEW IF NOT EXISTS daily_ohlcv
        WITH (timescaledb.continuous) AS
        SELECT
            time_bucket('1 day', date_time) AS bucket,
            symbol,
            first(open, date_time) AS open,
            max(high) AS high,
            min(low) AS low,
            last(close, date_time) AS close,
            sum(volume) AS volume
        FROM stock_ohlcv
        GROUP BY bucket, symbol
        WITH NO DATA;
    """)
    
    # Add refresh policy for continuous aggregate
    op.execute("""
        SELECT add_continuous_aggregate_policy('daily_ohlcv',
            start_offset => INTERVAL '3 days',
            end_offset => INTERVAL '1 hour',
            schedule_interval => INTERVAL '1 hour'
        );
    """)


def downgrade():
    # Remove continuous aggregate policy
    op.execute("SELECT remove_continuous_aggregate_policy('daily_ohlcv', if_exists => true);")
    
    # Drop continuous aggregate
    op.execute("DROP MATERIALIZED VIEW IF EXISTS daily_ohlcv;")
    
    # Drop indexes
    op.execute("DROP INDEX IF EXISTS idx_ohlcv_symbol_time;")
    
    # Drop all tables (in reverse order of creation)
    op.drop_table('trade_logs')
    op.drop_table('positions')
    op.drop_table('portfolio_status')
    op.drop_table('stock_fundamentals')
    op.drop_table('corporate_actions')
    op.drop_table('stock_ohlcv')
    op.drop_table('stock_tickers')
