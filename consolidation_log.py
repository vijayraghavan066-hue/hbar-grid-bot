"""Append-only, structured audit trail of every zone-shift consolidation
and sell-fanout event. The systemd journal already logs these as text, but
that's not queryable/renderable — this gives dashboard.py a real table to
read: "consolidated from what to what," and "fanned out into what."

Written to from inside zone_shift.py and sell_fanout.py directly (not
threaded through every caller's arguments) so every code path that merges
or fans out orders is covered automatically, including future ones.
"""
import csv
import os
import time

DEFAULT_LOG_FILE = "consolidation_log.csv"
FIELDS = ["timestamp_utc", "event_type", "side", "original_prices", "original_qtys",
          "result_prices", "result_qtys"]


def log_event(event_type, side, original_prices, original_qtys, result_prices, result_qtys,
              path=DEFAULT_LOG_FILE):
    """event_type: 'pending_merge' (zone-shift merged not-yet-bought levels)
                  | 'holding_merge' (zone-shift merged already-owned sells)
                  | 'dup_merge' (housekeeping merged same-price duplicates)
                  | 'fanout' (a consolidated bucket's fill fanned into rungs)
    side: 'buy' | 'sell' — which side of the book this affected."""
    is_new = not os.path.exists(path)
    try:
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if is_new:
                w.writeheader()
            w.writerow({
                "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                "event_type": event_type,
                "side": side,
                "original_prices": ";".join(f"{p:g}" for p in original_prices),
                "original_qtys": ";".join(f"{q:g}" for q in original_qtys),
                "result_prices": ";".join(f"{p:g}" for p in result_prices),
                "result_qtys": ";".join(f"{q:g}" for q in result_qtys),
            })
    except Exception:
        pass  # logging must never break a real merge/fanout that already succeeded


def read_events(path=DEFAULT_LOG_FILE, n=200):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    return rows[-n:][::-1]
