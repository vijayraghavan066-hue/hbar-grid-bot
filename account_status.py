"""Read-only account assessment. Calls only Binance.US read endpoints —
balances, open orders, recent trades. Never places, cancels, or modifies
anything. Safe to run against a live account at any time.

    python account_status.py
"""
import os

from binance.client import Client
from dotenv import load_dotenv

load_dotenv()

SYMBOL = os.getenv("SYMBOL", "DASHUSDT")


def main():
    api_key = os.getenv("BINANCE_US_API_KEY")
    api_secret = os.getenv("BINANCE_US_API_SECRET")
    if not api_key or not api_secret:
        raise SystemExit("BINANCE_US_API_KEY / BINANCE_US_API_SECRET not set in .env")

    client = Client(api_key, api_secret, tld="us")

    print(f"=== Account balances (nonzero only) ===")
    account = client.get_account()
    for bal in account["balances"]:
        free, locked = float(bal["free"]), float(bal["locked"])
        if free > 0 or locked > 0:
            print(f"  {bal['asset']:>6}: free={free:.8f}  locked={locked:.8f}")

    print(f"\n=== Current {SYMBOL} price ===")
    ticker = client.get_symbol_ticker(symbol=SYMBOL)
    print(f"  {ticker['price']}")

    print(f"\n=== Open orders on {SYMBOL} ===")
    open_orders = client.get_open_orders(symbol=SYMBOL)
    print(f"  Count: {len(open_orders)} (Binance.US caps this at 200 per symbol)")
    for o in open_orders[:10]:
        print(f"  {o['side']:<4} {o['origQty']:>10} @ {o['price']:>10}  status={o['status']}  id={o['orderId']}")
    if len(open_orders) > 10:
        print(f"  ... and {len(open_orders) - 10} more")

    print(f"\n=== All open orders across every symbol ===")
    all_open = client.get_open_orders()
    by_symbol = {}
    for o in all_open:
        by_symbol.setdefault(o["symbol"], 0)
        by_symbol[o["symbol"]] += 1
    if by_symbol:
        for sym, count in by_symbol.items():
            print(f"  {sym}: {count}")
    else:
        print("  none")

    print(f"\n=== Last 10 trades on {SYMBOL} ===")
    try:
        trades = client.get_my_trades(symbol=SYMBOL, limit=10)
        if not trades:
            print("  none")
        for t in trades:
            side = "BUY" if t["isBuyer"] else "SELL"
            print(f"  {side:<4} {t['qty']:>10} @ {t['price']:>10}  time={t['time']}")
    except Exception as exc:
        print(f"  could not fetch trade history: {exc}")


if __name__ == "__main__":
    main()
