"""Standalone liveness check. Run manually, or on a cron/systemd timer for
alerting: exits 0 if the bot heartbeat is fresh, 1 if it's stale/missing/
reporting an error.

    python check_health.py

Wire this into external alerting (cron + mail, a monitoring agent, etc.) by
checking its exit code.
"""
import sys
import time

from config import load_config
from health import read_heartbeat

STALE_MULTIPLIER = 3  # heartbeat older than poll_interval * this = considered stuck


def main():
    cfg = load_config()
    hb = read_heartbeat(cfg.heartbeat_file)

    if hb is None:
        print(f"UNKNOWN: no heartbeat file found at {cfg.heartbeat_file} "
              f"(bot never started, or wrong working directory)")
        sys.exit(1)

    age = time.time() - hb["ts"]
    stale_after = cfg.poll_interval_seconds * STALE_MULTIPLIER

    print(f"Last update:    {hb['iso_time']} UTC ({age:.0f}s ago)")
    print(f"Mode:           {'DRY RUN' if hb['dry_run'] else 'LIVE'}")
    print(f"Current price:  {hb['current_price']}")
    print(f"Profit realized:{hb['total_profit_realized']:.4f} USDT")
    print(f"Reserved total: {hb['reserved_total']:.4f} USDT")
    print(f"Grid levels:    idle={hb['levels']['idle']} "
          f"buy_open={hb['levels']['buy_open']} sell_open={hb['levels']['sell_open']}")

    if hb["last_error"]:
        print(f"\nLast error:     {hb['last_error']}")

    if age > stale_after:
        print(f"\nSTALE: heartbeat is older than {stale_after}s — bot looks stuck or dead")
        sys.exit(1)

    print("\nOK: bot is alive and updating on schedule")
    sys.exit(0)


if __name__ == "__main__":
    main()
