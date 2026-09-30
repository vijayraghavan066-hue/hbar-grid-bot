"""Quick P&L summary from the trade ledger.

    python ledger_summary.py [path/to/trades.csv]
"""
import csv
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "trades.csv"

buys = []
sells = []

with open(path, newline="") as f:
    for row in csv.DictReader(f):
        if row["event"] == "BUY_FILL":
            buys.append(row)
        elif row["event"] == "SELL_FILL":
            sells.append(row)

total_profit = sum(float(r["profit_usdt"]) for r in sells)
total_reserved = sum(float(r["reserved_usdt"]) for r in sells)
total_distributed = sum(float(r["distributed_usdt"]) for r in sells)
buy_volume = sum(float(r["usdt_value"]) for r in buys)
sell_volume = sum(float(r["usdt_value"]) for r in sells)
one_shot_sells = [r for r in sells if r["one_shot"] == "True"]

print(f"Ledger: {path}")
print(f"  Buy fills:  {len(buys):>5}  (${buy_volume:,.2f} total)")
print(f"  Sell fills: {len(sells):>5}  (${sell_volume:,.2f} total, {len(one_shot_sells)} from bulk ladder)")
print()
print(f"  Total profit realized: ${total_profit:,.4f}")
print(f"  Reserved (untouched):  ${total_reserved:,.4f}")
print(f"  Reinvested/distributed: ${total_distributed:,.4f}")
if sells:
    print(f"  Average profit per sell: ${total_profit/len(sells):,.4f}")
if buys:
    print(f"\n  First buy: {buys[0]['timestamp_utc']}")
    print(f"  Last event: {max(buys[-1]['timestamp_utc'], sells[-1]['timestamp_utc'] if sells else '')}")
