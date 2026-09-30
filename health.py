import json
import os
import time


def write_heartbeat(path: str, current_price: float, state, last_error: str | None, dry_run: bool,
                     zone_config: dict | None = None):
    level_summary = {}
    for s in ("idle", "buy_open", "sell_open"):
        level_summary[s] = sum(1 for lvl in state.levels if lvl.state == s)

    # Top of the resting sell book, closest to market first — "what's about
    # to fill next" alongside the recent-trades list of what already has.
    upcoming_sells = sorted(
        (lvl for lvl in state.levels if lvl.state == "sell_open"),
        key=lambda lvl: lvl.sell_price,
    )[:10]

    # Mirror of the above for the buy side: top of the resting buy book,
    # closest to market first (highest buy price, since buys rest below
    # market) — shown alongside upcoming_sells so the two can be compared.
    upcoming_buys = sorted(
        (lvl for lvl in state.levels if lvl.state == "buy_open"),
        key=lambda lvl: -lvl.buy_price,
    )[:10]

    payload = {
        "ts": time.time(),
        "iso_time": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "dry_run": dry_run,
        "current_price": current_price,
        "total_profit_realized": state.total_profit_realized,
        "reserved_total": state.reserved_total,
        "levels": level_summary,
        "last_error": last_error,
        "zone_config": zone_config or {},
        "upcoming_sells": [
            {"index": lvl.index, "sell_price": lvl.sell_price, "buy_price": lvl.buy_price, "qty": lvl.qty}
            for lvl in upcoming_sells
        ],
        "upcoming_buys": [
            {"index": lvl.index, "buy_price": lvl.buy_price, "sell_price": lvl.sell_price, "qty": lvl.qty}
            for lvl in upcoming_buys
        ],
        "circuit_breaker": {
            "tripped": state.circuit_breaker_tripped,
            "reason": state.circuit_breaker_reason,
            "tripped_ts": state.circuit_breaker_tripped_ts,
            "trip_low": state.circuit_breaker_trip_low,
        },
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp_path, path)


def read_heartbeat(path: str):
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)
