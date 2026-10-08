"""Small top-up: adds capital to HBAR's DORMANT ladder reserves (ladder2,
ladder3 only -- ladder1 is already active and already funded, so it's
excluded here), split proportional to each dormant ladder's own
30%-below-market stress shortfall. Source of the capital is external to
this script (e.g. DASH's ladder1 step-change freed orders -- see
dash-grid-bot's migrate_ladder1_step_and_reserves.py) -- this just books
it into HBAR's state. Plan-only by default.
"""
import argparse
import shutil
import time

from binance_client import BinanceUSTrader
from config import load_config
from pct_ladder import ladder_specs
from state import load_state, save_state


def compute_shortfalls(state, cfg, current_price, drop_pct=0.30):
    target_price = current_price * (1.0 - drop_pct)
    results = {}
    for step_pct, offset_pct, window_pct, name in ladder_specs(cfg):
        same_ladder = [lvl for lvl in state.levels
                       if not lvl.one_shot and lvl.state in ("idle", "buy_open")
                       and abs((lvl.sell_price / lvl.buy_price - 1.0) - offset_pct) < 0.0005]
        own_capital = sum(lvl.target_qty() * lvl.buy_price for lvl in same_ladder)
        n_levels = len(same_ladder)
        avg_per_level = own_capital / n_levels if n_levels else 1.0
        levels_needed = max(1, int((current_price - target_price) / (current_price * step_pct)) + 1)
        capital_needed = levels_needed * avg_per_level
        reserve_now = state.ladder_reserved.get(name, 0.0)
        shortfall = max(0.0, capital_needed - own_capital - reserve_now)
        results[name] = {"own_capital": own_capital, "reserve_now": reserve_now, "shortfall": shortfall}
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--amount", type=float, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    cfg = load_config()
    trader = BinanceUSTrader(cfg.api_key, cfg.api_secret, cfg.symbol, cfg.dry_run)
    current_price = trader.get_price()

    state = load_state(cfg.state_file)
    if state is None:
        raise SystemExit(f"No {cfg.state_file} found.")

    shortfalls = compute_shortfalls(state, cfg, current_price)
    dormant = {"ladder2", "ladder3"}
    shortfalls = {name: info for name, info in shortfalls.items() if name in dormant}
    total_shortfall = sum(s["shortfall"] for s in shortfalls.values())
    print(f"Current price: {current_price}")
    print(f"Topping up ${args.amount:.4f} across dormant ladders only (ladder1 excluded, already funded):")

    new_ladder_reserved = dict(state.ladder_reserved)
    for name, info in shortfalls.items():
        share = (args.amount * info["shortfall"] / total_shortfall) if total_shortfall > 0 else args.amount / 2
        new_ladder_reserved[name] = new_ladder_reserved.get(name, 0.0) + share
        print(f"{name}: own_capital={info['own_capital']:.2f} reserve_now={info['reserve_now']:.2f} "
              f"shortfall={info['shortfall']:.2f} -> +{share:.4f} = new reserve {new_ladder_reserved[name]:.4f}")

    if not args.execute:
        print()
        print("PLAN ONLY -- nothing changed. Re-run with --execute to apply.")
        return

    print()
    backup_path = f"{cfg.state_file}.pre_topup_{int(time.time())}.bak"
    shutil.copy(cfg.state_file, backup_path)
    print(f"Backed up current state to {backup_path}")

    state.ladder_reserved = new_ladder_reserved
    save_state(cfg.state_file, state)
    print("Wrote new state. No orders or levels touched -- reserve pools only.")
    print("Restart the bot service to let the next reshape cycle pick up the new reserve "
          "(may activate ladder2/ladder3 if they now clear min_notional).")


if __name__ == "__main__":
    main()
