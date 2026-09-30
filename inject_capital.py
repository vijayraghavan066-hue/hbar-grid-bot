"""Add fresh capital to the running strategy without disturbing anything
already in flight. Distributes the new amount (DCA-weighted, same formula
as the original grid build) across every level that's currently `idle` or
has an unfilled `buy_open` order — i.e. every level that hasn't bought yet.

Never touches `sell_open` levels (real, already-owned DASH waiting to sell)
or `closed` one-shot bulk-ladder slices.

IMPORTANT: stop the bot service before running this, and restart it after —
running this while the live bot is also polling/writing state.json risks a
race on that file.

    sudo systemctl stop dash-grid-bot
    sudo -u dashbot .venv/bin/python inject_capital.py 1000
    sudo systemctl start dash-grid-bot
"""
import sys

from binance_client import BinanceUSTrader
from config import load_config
from state import load_state, save_state

log_prefix = "[inject_capital]"


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

    weights = [(cfg.reference_price / lvl.buy_price) ** cfg.dca_exponent for lvl in eligible]
    total_weight = sum(weights)

    fresh_price = trader.get_price()

    grown = 0
    resized = 0
    total_added_usdt = 0.0
    for lvl, weight in zip(eligible, weights):
        share = amount * weight / total_weight
        lvl.extra_qty += share / lvl.buy_price
        total_added_usdt += share
        grown += 1

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
    print(f"{log_prefix} State saved to {cfg.state_file}")


if __name__ == "__main__":
    main()
