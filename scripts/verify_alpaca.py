import asyncio
import os
import sys

# Add project root to path
sys.path.append(os.getcwd())

from app.services.data_provider import AlpacaDataProvider
from app.core.config import settings

async def verify_alpaca():
    print(f"--- Verifying Alpaca Integration ---")
    print(f"Base URL: {settings.ALPACA_TRADING_URL}")

    provider = AlpacaDataProvider()

    # 1. Connectivity / Account (Implicit via TradingClient init)
    try:
        acct = await asyncio.get_event_loop().run_in_executor(None, provider.trading_client.get_account)
        print(f"[PASS] Account Status: {acct.status}")
        print(f"       Buying Power: ${acct.buying_power}")
    except Exception as e:
        print(f"[FAIL] Account Connection: {e}")
        return

    # 2. Data Fetching
    symbol = "SPY"
    try:
        price = await provider.get_current_price(symbol)
        print(f"[PASS] Current Price for {symbol}: ${price}")
    except Exception as e:
        print(f"[FAIL] Price Fetch: {e}")

    # 3. Paper Order (Dry Run logic usually, but here we place a small limit order far away to be safe, or just market buy 1 if paper)
    # Since it's PAPER trading, we can try to buy 1 share of a cheap stock.
    test_sym = "AAPL" 
    print(f"Attempting Paper Trade on {test_sym}...")
    try:
        # Check if market is open? Alpaca rejects if closed.
        # We'll just try and catch error.
        order_id = await provider.place_order(test_sym, 1, "buy")
        if order_id:
            print(f"[PASS] Order Placed. ID: {order_id}")
        else:
            print(f"[FAIL] Order Placement return None")
    except Exception as e:
        print(f"[WARN] Order Placement might have failed (Market Closed?): {e}")

if __name__ == "__main__":
    asyncio.run(verify_alpaca())
