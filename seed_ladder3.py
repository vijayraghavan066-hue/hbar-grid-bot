"""One-time seed for HBAR's ladder3: the per-cycle top-up mechanism can
only ever draw up to reserved_draw_floor_pct (50%) of ladder3's reserve
in a single cycle, which has never been enough to clear min_notional
across its own wide window (13 price points spanning its 30% window) --
it's been drawing and immediately returning the same ~$7.53 every cycle
since the design went live, never actually building a single real
order (see PROJECT_STATUS.md, 2026-10-08). This script bypasses that
per-cycle cap exactly once, using ladder3's own existing reserve (no new
capital), to place its first real order(s). Builds levels as "idle" and
lets the already-running bot's own apply_order_window arm them on its
next normal cycle, same as reshape_ladder's own levels always are --
this script never calls place_limit_buy itself.

Plan-only by default. --execute backs up state, zeroes ladder3's
reserve, and writes the new idle levels.
"""
import argparse
import shutil
import time

from binance_client import BinanceUSTrader
from config import load_config
from pct_ladder import _dca_build_levels, _thin_to_affordable, build_ladder_points, compute_ladder_zones, ladder_specs
from state import load_state, save_state

LADDER_NAME = "ladder3"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    trader = BinanceUSTrader(cfg.api_key, cfg.api_secret, cfg.symbol, cfg.dry_run)
    filters = trader.get_symbol_filters()
    current_price = trader.get_price()

    state = load_state(cfg.state_file)
    if state is None:
        raise SystemExit(f"No {cfg.state_file} found.")

    step_pct = offset_pct = window_pct = None
    for s, o, w, n in ladder_specs(cfg):
        if n == LADDER_NAME:
            step_pct, offset_pct, window_pct = s, o, w
    if step_pct is None:
        raise SystemExit(f"{LADDER_NAME} not found in ladder_specs(cfg)")

    seed_capital = state.ladder_reserved.get(LADDER_NAME, 0.0)
    zones = compute_ladder_zones(current_price, cfg)
    lo, hi = zones[LADDER_NAME]

    print(f"Current price: {current_price}")
    print(f"{LADDER_NAME} window: {lo:.5f} - {hi:.5f} (step {step_pct*100:g}%, sells +{offset_pct*100:g}%)")
    print(f"{LADDER_NAME} current reserve (full one-time seed amount, no new capital): ${seed_capital:.4f}")

    if seed_capital <= 0:
        print("Nothing to seed -- reserve is empty.")
        return

    points = build_ladder_points(lo, hi, step_pct)
    thinned = _thin_to_affordable(points, seed_capital, cfg.reference_price, cfg.dca_exponent, filters.min_notional)
    print(f"Window price points: {len(points)} total, {len(thinned)} survive min_notional thinning")
    fresh = _dca_build_levels(thinned, seed_capital, cfg.reference_price, cfg.dca_exponent,
                               offset_pct, filters, state.next_level_index)
    print(f"\nWould build {len(fresh)} real level(s):")
    total_cost = 0.0
    for lvl in fresh:
        cost = lvl.base_qty * lvl.buy_price
        total_cost += cost
        print(f"  buy=${lvl.buy_price:.6f} sell=${lvl.sell_price:.6f} qty={lvl.base_qty} (${cost:.4f})")
    print(f"Total committed: ${total_cost:.4f} of ${seed_capital:.4f} seed "
          f"(${seed_capital - total_cost:.4f} returned to reserve if any rounding remainder)")

    if not fresh:
        print("\nNo level cleared min_notional even with the full reserve -- "
              "seeding would do nothing. Not proceeding.")
        return

    if not args.execute:
        print("\nPLAN ONLY -- nothing changed. Re-run with --execute to apply.")
        return

    print("\nEXECUTING...")
    backup_path = f"{cfg.state_file}.pre_ladder3_seed_{int(time.time())}.bak"
    shutil.copy(cfg.state_file, backup_path)
    print(f"Backed up current state to {backup_path}")

    state.next_level_index += len(fresh)
    state.levels.extend(fresh)
    state.ladder_reserved[LADDER_NAME] = seed_capital - total_cost  # any rounding remainder stays reserved
    state.ladder_reserved_drawn[LADDER_NAME] = state.ladder_reserved_drawn.get(LADDER_NAME, 0.0) + total_cost
    save_state(cfg.state_file, state)
    print(f"Wrote new state: {len(fresh)} new idle level(s) added, {LADDER_NAME} reserve now "
          f"${state.ladder_reserved[LADDER_NAME]:.4f}")
    print("Restart the bot service -- apply_order_window will arm these as real orders on its next cycle.")


if __name__ == "__main__":
    main()
