import csv
import os
import time

FIELDS = [
    "timestamp_utc", "event", "level_index", "buy_price", "sell_price",
    "qty", "usdt_value", "profit_usdt", "reserved_usdt", "distributed_usdt",
    "one_shot", "dry_run",
]


def _append(path: str, row: dict):
    new_file = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def log_buy_fill(path: str, level_index: int, buy_price: float, qty: float,
                  one_shot: bool, dry_run: bool):
    _append(path, {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "event": "BUY_FILL",
        "level_index": level_index,
        "buy_price": buy_price,
        "sell_price": "",
        "qty": qty,
        "usdt_value": round(qty * buy_price, 6),
        "profit_usdt": "",
        "reserved_usdt": "",
        "distributed_usdt": "",
        "one_shot": one_shot,
        "dry_run": dry_run,
    })


def log_sell_fill(path: str, level_index: int, buy_price: float, sell_price: float,
                   qty: float, profit: float, reserved_amount: float,
                   distributed_amount: float, one_shot: bool, dry_run: bool):
    _append(path, {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "event": "SELL_FILL",
        "level_index": level_index,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "qty": qty,
        "usdt_value": round(qty * sell_price, 6),
        "profit_usdt": round(profit, 6),
        "reserved_usdt": round(reserved_amount, 6),
        "distributed_usdt": round(distributed_amount, 6),
        "one_shot": one_shot,
        "dry_run": dry_run,
    })
