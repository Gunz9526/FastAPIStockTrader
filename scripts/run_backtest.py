import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.append(os.getcwd())

from app.backtest.engine import BacktestEngine

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def _fmt(value, fmt_str: str, fallback: str = "N/A") -> str:
    """Safely format a value, returning fallback if None."""
    if value is None:
        return fallback
    try:
        return fmt_str % value
    except (TypeError, ValueError):
        return fallback


def print_single_result(result: dict) -> None:
    """Print backtest result for a single symbol."""
    print("\n" + "=" * 50)
    print(f"  BACKTEST RESULT: {result['symbol']}")
    print("=" * 50)
    print(f"  Initial Cash : ${result['initial_cash']:,.2f}")
    print(f"  Final Value  : ${result['final_value']:,.2f}")
    print(f"  Return       : {_fmt(result['return_pct'], '%.2f%%')}")
    print(f"  Sharpe Ratio : {_fmt(result['sharpe'], '%.4f')}")
    print(f"  Max Drawdown : {_fmt(result['drawdown'], '%.2f%%')}")
    print(f"  Total Trades : {result.get('total_trades', 0)}")
    print(f"  Win Rate     : {_fmt(result.get('win_rate'), '%.1f%%')}")
    print(f"  Regime Aware : {result.get('regime_aware', True)}")
    print("=" * 50)


def print_portfolio_result(portfolio: dict) -> None:
    """Print aggregated portfolio backtest results."""
    summary = portfolio['summary']
    per_symbol = portfolio['per_symbol']
    errors = portfolio['errors']

    if not summary:
        print("\n  No successful backtests.")
        return

    print("\n" + "=" * 70)
    print("  PORTFOLIO BACKTEST SUMMARY")
    print("=" * 70)
    print(f"  Symbols Tested  : {summary['total_symbols']}")
    print(f"  Avg Return      : {_fmt(summary['avg_return_pct'], '%+.2f%%')}")
    print(f"  Avg Sharpe      : {_fmt(summary['avg_sharpe'], '%.4f')}")
    print(f"  Avg Win Rate    : {_fmt(summary['avg_win_rate'], '%.1f%%')}")
    print(f"  Total Trades    : {summary['total_trades']}")
    print(f"  Winners/Losers  : {summary['winners']}/{summary['losers']}")
    print(f"  Best            : {summary['best_symbol']} ({_fmt(summary['best_return'], '%+.2f%%')})")
    print(f"  Worst           : {summary['worst_symbol']} ({_fmt(summary['worst_return'], '%+.2f%%')})")
    print(f"  Regime Aware    : {summary['regime_aware']}")
    print("-" * 70)

    # Per-symbol table
    print(f"  {'Symbol':<8} {'Return':>10} {'Sharpe':>10} {'Trades':>8} {'WinRate':>10} {'Drawdown':>10}")
    print("  " + "-" * 58)
    for r in per_symbol:
        print(
            f"  {r['symbol']:<8} "
            f"{_fmt(r['return_pct'], '%+.2f%%'):>10} "
            f"{_fmt(r['sharpe'], '%.4f'):>10} "
            f"{r.get('total_trades', 0):>8} "
            f"{_fmt(r.get('win_rate'), '%.1f%%'):>10} "
            f"{_fmt(r.get('drawdown'), '%.2f%%'):>10}"
        )

    if errors:
        print(f"\n  Failed symbols ({len(errors)}): {', '.join(errors)}")

    print("=" * 70)


def main():
    parser = argparse.ArgumentParser(description="Run ML Backtest")
    parser.add_argument(
        "--symbol", type=str, default=None,
        help="Stock symbol (e.g., AAPL) or 'ALL' for portfolio mode",
    )
    parser.add_argument("--days", type=int, default=365, help="Backtest period in days (default: 365)")
    parser.add_argument("--cash", type=float, default=10000.0, help="Initial cash per symbol")
    parser.add_argument(
        "--no-regime", action="store_true",
        help="Disable regime-aware trading (for A/B comparison)",
    )

    args = parser.parse_args()
    regime_aware = not args.no_regime

    end_date = datetime.now()
    start_date = end_date - timedelta(days=args.days)

    engine = BacktestEngine(initial_cash=args.cash, regime_aware=regime_aware)

    if args.symbol and args.symbol.upper() != "ALL":
        # Single symbol mode
        logger.info("Starting Backtest for %s", args.symbol)
        logger.info("Period: %s ~ %s", start_date.date(), end_date.date())

        result = engine.run(args.symbol, start_date, end_date)
        if result:
            print_single_result(result)
        else:
            logger.error("Backtest failed or returned no results.")
    else:
        # Portfolio mode: load symbols from DB
        logger.info("Starting PORTFOLIO Backtest (all symbols)")
        logger.info("Period: %s ~ %s", start_date.date(), end_date.date())

        from app.core.database import SessionLocal
        from app.repositories.stock_repo_sync import SyncStockRepository

        session = SessionLocal()
        try:
            repo = SyncStockRepository(session)
            symbols = repo.get_active_symbols()
            if not symbols:
                logger.error("No active symbols found in DB")
                return

            logger.info("Found %d symbols for portfolio backtest", len(symbols))
            portfolio = engine.run_portfolio(symbols, start_date, end_date)
            print_portfolio_result(portfolio)
        finally:
            session.close()


if __name__ == "__main__":
    main()
