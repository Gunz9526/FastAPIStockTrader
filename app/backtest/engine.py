import backtrader as bt
import pandas as pd
import logging
from datetime import datetime
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository
from app.backtest.ml_strategy import MLStrategy

logger = logging.getLogger(__name__)

class BacktestEngine:
    def __init__(self, initial_cash=10000.0, commission=0.001):
        self.initial_cash = initial_cash
        self.commission = commission
        
    def run(self, symbol: str, start_date: datetime, end_date: datetime):
        """Run backtest for a single symbol."""
        cerebro = bt.Cerebro()
        
        # 1. Strategy
        cerebro.addstrategy(MLStrategy)
        
        # 2. Data
        session = SessionLocal()
        try:
            repo = SyncStockRepository(session)
            ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date)
            
            if not ohlcv:
                logger.error(f"No data found for {symbol}")
                return None
            
            # Convert to Pandas DataFrame
            df = pd.DataFrame([{
                'datetime': bar.date_time, # Must match bt.feeds.PandasData requirement
                'open': bar.open,
                'high': bar.high,
                'low': bar.low,
                'close': bar.close,
                'volume': bar.volume,
                'openinterest': 0 # Default
            } for bar in ohlcv])
            
            df.set_index('datetime', inplace=True)
            df.sort_index(inplace=True)
            
            data = bt.feeds.PandasData(dataname=df)
            cerebro.adddata(data)
            
        except Exception as e:
            logger.error(f"Data loading failed: {e}")
            return None
        finally:
            session.close()
            
        # 3. Settings
        cerebro.broker.setcash(self.initial_cash)
        cerebro.broker.setcommission(commission=self.commission)
        
        # 4. Analyzers
        cerebro.addanalyzer(bt.analyzers.SharpeRatio, _name='sharpe')
        cerebro.addanalyzer(bt.analyzers.DrawDown, _name='drawdown')
        cerebro.addanalyzer(bt.analyzers.Returns, _name='returns')
        
        # 5. Run
        logger.info(f"Starting Portfolio Value: {cerebro.broker.getvalue():.2f}")
        results = cerebro.run()
        final_value = cerebro.broker.getvalue()
        logger.info(f"Final Portfolio Value: {final_value:.2f}")
        
        strat = results[0]
        
        # Extract metrics safely
        sharpe = strat.analyzers.sharpe.get_analysis().get('sharperatio', 0.0)
        drawdown = strat.analyzers.drawdown.get_analysis().get('max', {}).get('drawdown', 0.0)
        total_return = strat.analyzers.returns.get_analysis().get('rtot', 0.0)
        
        return {
            'symbol': symbol,
            'initial_cash': self.initial_cash,
            'final_value': final_value,
            'return_pct': (final_value - self.initial_cash) / self.initial_cash * 100,
            'sharpe': sharpe,
            'drawdown': drawdown
        }
