import logging
from datetime import datetime

import backtrader as bt
import pandas as pd

from app.backtest.ml_strategy import MLStrategy
from app.core.database import SessionLocal
from app.repositories.stock_repo_sync import SyncStockRepository

logger = logging.getLogger(__name__)

class BacktestEngine:
    def __init__(self, initial_cash=10000.0, commission=0.001, regime_aware=True):
        self.initial_cash = initial_cash
        self.commission = commission
        self.regime_aware = regime_aware

    def run(self, symbol: str, start_date: datetime, end_date: datetime):
        """Run backtest for a single symbol."""
        cerebro = bt.Cerebro()

        # 1. Strategy
        cerebro.addstrategy(MLStrategy, regime_aware=self.regime_aware, symbol=symbol)

        # 2. Data
        session = SessionLocal()
        try:
            repo = SyncStockRepository(session)
            ohlcv = repo.get_ohlcv_range(symbol, start_date, end_date)

            if not ohlcv:
                logger.error("No data found for %s", symbol)
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
            logger.error("Data loading failed: %s", e)
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
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name='trades')

        # 5. Run
        logger.info("Starting Portfolio Value: %.2f", cerebro.broker.getvalue())
        results = cerebro.run()
        final_value = cerebro.broker.getvalue()
        logger.info("Final Portfolio Value: %.2f", final_value)

        strat = results[0]

        # Extract metrics safely (Backtrader can return None)
        sharpe_raw = strat.analyzers.sharpe.get_analysis().get('sharperatio')
        sharpe = sharpe_raw if sharpe_raw is not None else 0.0
        # Cap extreme Sharpe values (annualization artifact with few trades)
        sharpe = max(min(sharpe, 10.0), -10.0)
        dd_raw = strat.analyzers.drawdown.get_analysis().get('max', {}).get('drawdown')
        drawdown = dd_raw if dd_raw is not None else 0.0
        total_return = strat.analyzers.returns.get_analysis().get('rtot', 0.0) or 0.0

        # Extract trade stats
        trade_analysis = strat.analyzers.trades.get_analysis()
        total_closed = trade_analysis.get('total', {}).get('closed', 0)
        won_total = trade_analysis.get('won', {}).get('total', 0)
        win_rate = (won_total / total_closed * 100) if total_closed > 0 else 0.0

        return {
            'symbol': symbol,
            'initial_cash': self.initial_cash,
            'final_value': final_value,
            'return_pct': (final_value - self.initial_cash) / self.initial_cash * 100,
            'sharpe': sharpe,
            'drawdown': drawdown,
            'total_trades': total_closed,
            'win_rate': win_rate,
            'regime_aware': self.regime_aware,
        }

    def run_portfolio(
        self,
        symbols: list[str],
        start_date: datetime,
        end_date: datetime,
    ) -> dict:
        """Run backtest across multiple symbols and aggregate results.

        Args:
            symbols: List of stock symbols to test.
            start_date: Backtest start date.
            end_date: Backtest end date.

        Returns:
            Dict with 'summary' and 'per_symbol' results.
        """
        results = []
        errors = []

        for symbol in symbols:
            try:
                result = self.run(symbol, start_date, end_date)
                if result:
                    results.append(result)
                else:
                    errors.append(symbol)
            except Exception as e:
                logger.error("Backtest failed for %s: %s", symbol, e)
                errors.append(symbol)

        if not results:
            logger.error("No successful backtests")
            return {"summary": None, "per_symbol": [], "errors": errors}

        # Aggregate metrics
        total_return = sum(r['return_pct'] for r in results) / len(results)
        total_trades = sum(r['total_trades'] for r in results)

        # Trade-weighted averages (more accurate than simple average)
        if total_trades > 0:
            avg_sharpe = sum(r['sharpe'] * r['total_trades'] for r in results) / total_trades
            avg_win_rate = sum(r['win_rate'] * r['total_trades'] for r in results) / total_trades
        else:
            avg_sharpe = 0.0
            avg_win_rate = 0.0

        # Sort by return
        results.sort(key=lambda r: r['return_pct'], reverse=True)

        # Winners / Losers
        winners = [r for r in results if r['return_pct'] > 0]
        losers = [r for r in results if r['return_pct'] <= 0]

        summary = {
            'total_symbols': len(results),
            'avg_return_pct': total_return,
            'avg_sharpe': avg_sharpe,
            'avg_win_rate': avg_win_rate,
            'total_trades': total_trades,
            'winners': len(winners),
            'losers': len(losers),
            'best_symbol': results[0]['symbol'] if results else None,
            'best_return': results[0]['return_pct'] if results else None,
            'worst_symbol': results[-1]['symbol'] if results else None,
            'worst_return': results[-1]['return_pct'] if results else None,
            'initial_cash': self.initial_cash,
            'regime_aware': self.regime_aware,
        }

        return {
            'summary': summary,
            'per_symbol': results,
            'errors': errors,
        }
