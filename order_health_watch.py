"""Detects grid levels whose real exchange order left the resting book
(filled, or otherwise vanished) without the bot's own state.json ever
reflecting it — the exact "stuck, retries forever, never gets a protective
order placed" pattern found only by manual log analysis on 2026-09-25 (see
PROJECT_STATUS.md, DASH levels 682/731/733/736/737: real buy fills whose
protective sell kept failing with "insufficient balance" for 38+ hours
before anyone noticed).

Runs on its own timer, independent of the bot process. Read-only: one
get_open_orders call plus a get_order call for each anomaly found (normally
zero or a handful, never one per level). Writes order_health.json, which
dashboard.py renders as a card + banner, same pattern as listing_watch.py.

A level only alerts after staying stuck across STUCK_WARN_MINUTES /
STUCK_CRIT_MINUTES of consecutive runs — the bot's own ~2min poll loop
resolves a completely normal fill almost immediately, so a single miss here
is not itself news; only a level still missing many cycles later is.
"""
import hashlib
import json
import os
import time
from datetime import datetime, timezone

from binance_client import BinanceUSTrader
from config import load_config

OUT_FILE = os.getenv("ORDER_HEALTH_FILE", "order_health.json")
STUCK_WARN_MINUTES = 10
STUCK_CRIT_MINUTES = 60


def load_previous(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_state_levels(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("levels", [])


def check():
    cfg = load_config()
    now = time.time()
    result = {
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "checked_ts": now,
        "symbol": cfg.symbol,
        "candidates": [],
        "stuck": [],
        "orphaned": [],
        "overall": "OK",
        "error": None,
    }
    if cfg.dry_run:
        result["error"] = "dry-run — no real orders to reconcile against"
        return result

    prev = load_previous(OUT_FILE)
    prev_first_seen = {c["order_id"]: c["first_seen_ts"] for c in prev.get("candidates", [])}

    try:
        levels = load_state_levels(cfg.state_file)
        trader = BinanceUSTrader(cfg.api_key, cfg.api_secret, cfg.symbol, dry_run=False)
        open_orders = trader._client.get_open_orders(symbol=cfg.symbol)
    except Exception as exc:
        result["overall"] = "WARN"
        result["error"] = f"Could not reach exchange: {exc.__class__.__name__}: {exc}"
        return result
    real_open_ids = {int(o["orderId"]) for o in open_orders}

    candidates = []
    for lvl in levels:
        side = order_id = price = None
        if lvl.get("state") == "buy_open" and lvl.get("buy_order_id"):
            side, order_id, price = "buy", lvl["buy_order_id"], lvl.get("buy_price")
        elif lvl.get("state") == "sell_open" and lvl.get("sell_order_id"):
            side, order_id, price = "sell", lvl["sell_order_id"], lvl.get("sell_price")
        else:
            continue
        if int(order_id) in real_open_ids:
            continue  # still genuinely resting — healthy, nothing to track

        try:
            order = trader._client.get_order(symbol=cfg.symbol, orderId=int(order_id))
            status = order["status"]
        except Exception as exc:
            status = f"UNKNOWN ({exc.__class__.__name__})"

        first_seen = prev_first_seen.get(order_id, now)
        candidates.append({
            "index": lvl.get("index"), "side": side, "order_id": order_id, "price": price,
            "qty": lvl.get("qty"), "status": status, "first_seen_ts": first_seen,
            "stuck_minutes": round((now - first_seen) / 60, 1),
        })
    result["candidates"] = candidates  # carried forward next run for debouncing

    for c in candidates:
        if c["stuck_minutes"] < STUCK_WARN_MINUTES:
            continue
        entry = {k: v for k, v in c.items() if k != "first_seen_ts"}
        (result["stuck"] if c["status"] == "FILLED" else result["orphaned"]).append(entry)

    worst = max((c["stuck_minutes"] for c in candidates if c["status"] == "FILLED"), default=0)
    if result["stuck"] and worst >= STUCK_CRIT_MINUTES:
        result["overall"] = "CRITICAL"
    elif result["stuck"] or result["orphaned"]:
        result["overall"] = "WARN"

    alertable = {"stuck": result["stuck"], "orphaned": result["orphaned"]}
    result["alert_fingerprint"] = hashlib.sha256(
        json.dumps(alertable, sort_keys=True).encode()).hexdigest()[:16]
    return result


def main():
    res = check()
    tmp = OUT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2)
    os.replace(tmp, OUT_FILE)
    print(f"[{res['checked_at']}] overall={res['overall']} "
          f"stuck={len(res.get('stuck', []))} orphaned={len(res.get('orphaned', []))}")
    for c in res.get("stuck", []) + res.get("orphaned", []):
        print(f"  Grid {c['index']} ({c['side']} ${c['price']} x {c['qty']}): "
              f"{c['status']}, stuck {c['stuck_minutes']:.0f} min")


if __name__ == "__main__":
    main()
