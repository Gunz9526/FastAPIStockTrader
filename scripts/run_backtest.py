import sys
import os
import argparse
import logging
from datetime import datetime, timedelta

# Add project root to path
sys.path.append(os.getcwd())

from app.backtest.engine import BacktestEngine

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description="Run ML Backtest")
    parser.add_argument("--symbol", type=str, required=True, help="Stock symbol (e.g., AAPL)")
    parser.add_argument("--days", type=int, default=365, help="Backtest days (default: 365)")
    parser.add_argument("--cash", type=float, default=10000.0, help="Initial cash")
    
    args = parser.parse_args()
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=args.days)
    
    logger.info(f"🚀 Starting Backtest for {args.symbol}")
    logger.info(f"📅 Period: {start_date.date()} ~ {end_date.date()}")
    
    engine = BacktestEngine(initial_cash=args.cash)
    result = engine.run(args.symbol, start_date, end_date)
    
    if result:
        print("\n" + "="*40)
        print(f"📊 BACKTEST RESULT: {result['symbol']}")
        print("="*40)
        print(f"Initial Cash: ${result['initial_cash']:,.2f}")
        print(f"Final Value : ${result['final_value']:,.2f}")
        print(f"Return      : {result['return_pct']:.2f}%")
        print(f"Sharpe Ratio: {result['sharpe']:.4f}")
        print(f"Max Drawdown: {result['drawdown']:.2f}%")
        print("="*40 + "\n")
    else:
        logger.error("Backtest failed or returned no results.")

if __name__ == "__main__":
    main()
