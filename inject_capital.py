"""Add fresh capital to the running strategy without disturbing anything
already in flight. Distributes the new amount across every level that's
currently `idle` or has an unfilled `buy_open` order -- i.e. every level
that hasn't bought yet.

Never touches `sell_open` levels (real, already-owned HBAR waiting to sell)
or `closed` one-shot bulk-ladder slices.

LADDER-SKEWED ALLOCATION (2026-10-14): new capital is no longer split
evenly across all 5 ladders. Per the project's own 6-month backtest (see
PROJECT_STATUS.md "Grid parameter research") wider offsets earn
consistently more per dollar deployed -- Ladder 1's 1% offset sits below
the tested sweet spot, Ladders 2-5 (2-5%) land right in the range that
tested well. Requested explicitly: keep Ladder 1 meaningfully alive
(frequent small fills are valued for their own sake, as a visible signal
the bots are actively working) rather than starving it, while directing
new capital's actual earning power toward the ladders proven to use it
better. Chosen skew -- a flat floor, not proportional to offset -- Ladder
1 gets HALF of an equal (20%) share (10%), the remaining 90% splits evenly
across Ladders 2-5 (22.5% each). Within each ladder, the existing
DCA-weighted formula still applies (cheaper points get a bigger share of
that ladder's own pool). Any level that doesn't match one of the 5
ladders exactly (legacy/bulk) falls back to an unweighted 20% share, and
missing ladders (no eligible levels right now) don't lose their money --
the remaining buckets' weights renormalize to still sum to 100%.

IMPORTANT: stop the bot service before running this, and restart it after —
running this while the live bot is also polling/writing state.json risks a
race on that file.

    sudo systemctl stop hbar-grid-bot
    sudo -u hbarbot .venv/bin/python inject_capital.py 1000
    sudo systemctl start hbar-grid-bot
"""
import sys

from binance_client import BinanceUSTrader
from config import load_config
from pct_ladder import _exact_ladder_match
from state import load_state, save_state

log_prefix = "[inject_capital]"

LADDER_WEIGHT = {"ladder1": 0.10, "ladder2": 0.225, "ladder3": 0.225, "ladder4": 0.225, "ladder5": 0.225}
DEFAULT_WEIGHT = 0.20  # any level that doesn't match one of the 5 ladders exactly (legacy/bulk) -- same
# share an unweighted ladder would get, so it's never starved just for lacking a ladder identity.


def _ladder_key_for(lvl, cfg):
    gap = (lvl.sell_price / lvl.buy_price - 1.0) if lvl.buy_price else 0.0
    return _exact_ladder_match(gap, cfg) or "other"


def _compute_shares(eligible, amount, cfg):
    """Returns [(lvl, share_usd), ...] -- the ladder-skewed, then
    DCA-weighted-within-ladder, apportionment of `amount` across `eligible`
    levels. Buckets with no eligible levels right now simply don't appear,
    and the remaining buckets' weights renormalize so no money is silently
    dropped."""
    buckets = {}
    for lvl in eligible:
        key = _ladder_key_for(lvl, cfg)
        buckets.setdefault(key, []).append(lvl)

    bucket_weight = {key: LADDER_WEIGHT.get(key, DEFAULT_WEIGHT) for key in buckets}
    total_bucket_weight = sum(bucket_weight.values())

    shares = []
    for key, levels_in_bucket in buckets.items():
        bucket_amount = amount * bucket_weight[key] / total_bucket_weight
        weights = [(cfg.reference_price / lvl.buy_price) ** cfg.dca_exponent for lvl in levels_in_bucket]
        total_weight = sum(weights)
        for lvl, w in zip(levels_in_bucket, weights):
            shares.append((lvl, bucket_amount * w / total_weight))
    return shares


def main():
    if len(sys.argv) != 2:
        print("Usage: python inject_capital.py <amount_usdt>")
        sys.exit(1)
    amount = float(sys.argv[1])
    if amount <= 0:
        print("Amount must be positive.")
        sys.exit(1)

    cfg = load_config()
    state = load_state(cfg.state_file)
    if state is None:
        print(f"No state file found at {cfg.state_file} — is the bot set up here?")
        sys.exit(1)

    trader = BinanceUSTrader(cfg.api_key, cfg.api_secret, cfg.symbol, cfg.dry_run)
    filters = trader.get_symbol_filters()

    eligible = [lvl for lvl in state.levels if lvl.state in ("idle", "buy_open")]
    if not eligible:
        print("No idle/buy_open levels to grow (everything is sell_open or closed?). Nothing to do.")
        sys.exit(0)

    fresh_price = trader.get_price()

    grown = 0
    resized = 0
    total_added_usdt = 0.0
    by_ladder_usdt = {}
    for lvl, share in _compute_shares(eligible, amount, cfg):
        lvl.extra_qty += share / lvl.buy_price
        total_added_usdt += share
        grown += 1
        key = _ladder_key_for(lvl, cfg)
        by_ladder_usdt[key] = by_ladder_usdt.get(key, 0.0) + share

        if lvl.state == "buy_open" and lvl.buy_order_id is not None:
            try:
                trader.cancel_order(lvl.buy_order_id)
            except Exception as exc:
                print(f"{log_prefix} Grid {lvl.index}: could not cancel {lvl.buy_order_id} "
                      f"({exc}) — likely already filled, leaving untouched")
                continue

            new_qty = filters.round_qty(lvl.target_qty())
            lvl.state = "idle"
            lvl.buy_order_id = None
            if new_qty <= 0:
                continue

            place_price = lvl.buy_price
            if place_price >= fresh_price:
                adjusted = filters.round_price(fresh_price - cfg.taker_avoidance_buffer)
                if adjusted > 0 and new_qty * adjusted >= filters.min_notional:
                    place_price = adjusted

            try:
                lvl.buy_order_id = trader.place_limit_buy(place_price, new_qty)
                lvl.buy_price = place_price
                lvl.state = "buy_open"
                lvl.qty = new_qty
                resized += 1
            except Exception as exc:
                print(f"{log_prefix} Grid {lvl.index} (${lvl.buy_price}): resized buy "
                      f"rejected ({exc}) — left idle, order window will retry")

    save_state(cfg.state_file, state)

    print(f"{log_prefix} Injected ${amount:.2f} across {grown} level(s) "
          f"(${total_added_usdt:.2f} allocated, {resized} open buy order(s) resized now)")
    for key in sorted(by_ladder_usdt):
        print(f"{log_prefix}   {key}: ${by_ladder_usdt[key]:.4f}")
    print(f"{log_prefix} State saved to {cfg.state_file}")


if __name__ == "__main__":
    main()
