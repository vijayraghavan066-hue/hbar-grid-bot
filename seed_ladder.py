"""General-purpose version of seed_ladder3.py: seeds ANY named ladder with
fresh external capital (not just whatever happens to already be sitting
in that ladder's own reserve). Built for ladder1/4/5, which were found
completely dry (ladder4/5: zero levels of any state; ladder1: sell_open
only) with $0 reserve each -- the existing tools (inject_capital.py,
seed_ladder3.py) can't recover a ladder in that state, since they only
grow levels that already exist or spend a reserve that's already there.

Any pre-existing reserve in the named ladder is folded into the seed
capital too (so nothing idle gets left behind), then the usual
thin-to-affordable + DCA-weighted level-building runs across that
ladder's own price window. Builds levels as "idle" and lets the
already-running bot's own apply_order_window arm them on its next normal
cycle, same as reshape_ladder's own levels always are -- this script
never calls place_limit_buy itself.

Plan-only by default. --execute backs up state and writes the new idle
levels.

    sudo systemctl stop hbar-grid-bot
    sudo -u hbarbot .venv/bin/python seed_ladder.py ladder1 20
    sudo -u hbarbot .venv/bin/python seed_ladder.py ladder1 20 --execute
    sudo systemctl start hbar-grid-bot
"""
import argparse
import shutil
import time

from binance_client import BinanceUSTrader
from config import load_config
from pct_ladder import _dca_build_levels, _thin_to_affordable, build_ladder_points, compute_ladder_zones, ladder_specs
from state import load_state, save_state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("ladder", help="Ladder name, e.g. ladder1")
    parser.add_argument("amount", type=float, help="Fresh USDT to seed this ladder with")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    if args.amount <= 0:
        raise SystemExit("amount must be positive.")

    cfg = load_config()
    trader = BinanceUSTrader(cfg.api_key, cfg.api_secret, cfg.symbol, cfg.dry_run)
    filters = trader.get_symbol_filters()
    current_price = trader.get_price()

    state = load_state(cfg.state_file)
    if state is None:
        raise SystemExit(f"No {cfg.state_file} found.")

    step_pct = offset_pct = window_pct = None
    for s, o, w, n in ladder_specs(cfg):
        if n == args.ladder:
            step_pct, offset_pct, window_pct = s, o, w
    if step_pct is None:
        raise SystemExit(f"{args.ladder} not found in ladder_specs(cfg)")

    existing_reserve = state.ladder_reserved.get(args.ladder, 0.0)
    seed_capital = args.amount + existing_reserve
    zones = compute_ladder_zones(current_price, cfg)
    lo, hi = zones[args.ladder]

    print(f"Current price: {current_price}")
    print(f"{args.ladder} window: {lo:.5f} - {hi:.5f} (step {step_pct*100:g}%, sells +{offset_pct*100:g}%)")
    print(f"{args.ladder} existing reserve: ${existing_reserve:.4f} + ${args.amount:.2f} fresh "
          f"= ${seed_capital:.4f} total seed capital")

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
          f"(${seed_capital - total_cost:.4f} left in reserve)")

    if not fresh:
        print("\nNo level cleared min_notional even with the full seed -- not proceeding.")
        return

    if not args.execute:
        print("\nPLAN ONLY -- nothing changed. Re-run with --execute to apply.")
        return

    print("\nEXECUTING...")
    backup_path = f"{cfg.state_file}.pre_seed_{args.ladder}_{int(time.time())}.bak"
    shutil.copy(cfg.state_file, backup_path)
    print(f"Backed up current state to {backup_path}")

    state.next_level_index += len(fresh)
    state.levels.extend(fresh)
    state.ladder_reserved[args.ladder] = seed_capital - total_cost
    state.ladder_reserved_drawn[args.ladder] = state.ladder_reserved_drawn.get(args.ladder, 0.0) + total_cost
    save_state(cfg.state_file, state)
    print(f"Wrote new state: {len(fresh)} new idle level(s) added, {args.ladder} reserve now "
          f"${state.ladder_reserved[args.ladder]:.4f}")
    print("Restart the bot service -- apply_order_window will arm these as real orders on its next cycle.")


if __name__ == "__main__":
    main()
