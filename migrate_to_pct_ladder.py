"""One-time cutover: HBAR's old single-dense-zone structure -> the new
percentage-based, 3-ladder, separate-reserve system (pct_ladder.py).

Plan-only by default (prints what it would do, changes nothing). Pass
--execute to actually cancel orders and write new state. Same shape as
xrp-grid-bot's historical rebuild_even_ladder.py cutover script.

SAFETY RULE, non-negotiable: every currently sell_open (already-bought,
real) position is preserved byte-for-byte -- never cancelled, never
resized, never touched. Only idle levels and the one resting (never-
filled) buy_open order are freed for the new structure. Run this AFTER
updating .env with the new ANCHOR_LOW/ANCHOR_HIGH/PCT_LADDER_ENABLED/
LADDERn_* values but BEFORE restarting the bot service -- it computes
and bakes in the signature bot.py's run() will compute from that same
.env on next start, so the bot takes the "resume prior state" path
instead of its own "config changed -> cancel everything" path (which
would otherwise wipe out every preserved position the moment the
service restarts with a changed ANCHOR_LOW/ANCHOR_HIGH).
"""
import argparse
import shutil
import time

from binance_client import BinanceUSTrader
from config import load_config
from pct_ladder import build_ladder_points, ladder_specs, _dca_build_levels, _thin_to_affordable
from state import BotState, load_state, save_state

# Stress-test-derived split (see conversation record): capital needed for
# each ladder to cover a 30%-below-market decline, normalized to sum to 1.
LADDER_CAPITAL_SPLIT = {"ladder1": 0.5362, "ladder2": 0.2754, "ladder3": 0.1884}


def build_current_signature(cfg):
    """Must match bot.py's run() EXACTLY, field for field -- this is what
    makes the bot resume instead of rebuild on next start."""
    return {
        "symbol": cfg.symbol,
        "dry_run": cfg.dry_run,
        "grid_mode": cfg.grid_mode,
        "pilot_points": cfg.pilot_points,
        "anchor_low": cfg.anchor_low,
        "anchor_high": cfg.anchor_high,
        "anchor_step": cfg.anchor_step,
        "dense_low": cfg.dense_low,
        "dense_high": cfg.dense_high,
        "dense_step": cfg.dense_step,
        "dense_sell_offset": cfg.dense_sell_offset,
        "total_capital_usdt": cfg.total_capital_usdt,
        "reference_price": cfg.reference_price,
        "dca_exponent": cfg.dca_exponent,
        "bulk_amount_usdt": cfg.bulk_amount_usdt,
        "bulk_sell_high": cfg.bulk_sell_high,
        "bulk_sell_step": cfg.bulk_sell_step,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true", help="Actually cancel orders and write new state. Omit for a plan-only dry run.")
    args = parser.parse_args()

    cfg = load_config()
    trader = BinanceUSTrader(cfg.api_key, cfg.api_secret, cfg.symbol, cfg.dry_run)
    filters = trader.get_symbol_filters()
    current_price = trader.get_price()

    prior = load_state(cfg.state_file)
    if prior is None:
        raise SystemExit(f"No existing {cfg.state_file} found -- nothing to migrate.")

    preserve = [lvl for lvl in prior.levels if lvl.state == "sell_open"]
    free_buy_open = [lvl for lvl in prior.levels if lvl.state == "buy_open"]
    free_idle = [lvl for lvl in prior.levels if lvl.state == "idle"]
    other = [lvl for lvl in prior.levels if lvl.state not in ("sell_open", "buy_open", "idle")]

    freed_capital = sum(l.target_qty() * l.buy_price for l in free_buy_open + free_idle)

    print(f"Current price: {current_price}")
    print(f"Preserved (sell_open, untouched): {len(preserve)} levels, "
          f"{sum(l.target_qty() for l in preserve):.4f} {cfg.symbol[:-4]} held, "
          f"${sum(l.target_qty() * l.buy_price for l in preserve):.4f} cost basis")
    print(f"Freed (buy_open, real order to cancel): {len(free_buy_open)} levels, ${sum(l.target_qty() * l.buy_price for l in free_buy_open):.4f}")
    print(f"Freed (idle, no real order): {len(free_idle)} levels, ${sum(l.target_qty() * l.buy_price for l in free_idle):.4f}")
    print(f"Total freed capital for the new ladders: ${freed_capital:.4f}")
    if other:
        print(f"WARNING: {len(other)} level(s) in an unexpected state {[l.state for l in other]} -- preserved untouched, not counted above")
    print()

    new_signature = build_current_signature(cfg)

    new_levels = []
    new_ladder_reserved = {}
    next_index = prior.next_level_index
    for step_pct, offset_pct, window_pct, name in ladder_specs(cfg):
        ladder_capital = freed_capital * LADDER_CAPITAL_SPLIT[name]
        lo = max(current_price * (1.0 - window_pct), cfg.anchor_low)
        hi = min(current_price, cfg.anchor_high)
        points = [p for p in build_ladder_points(lo, hi, step_pct) if p < current_price]
        points = _thin_to_affordable(points, ladder_capital, cfg.reference_price, cfg.dca_exponent, filters.min_notional)
        levels = _dca_build_levels(points, ladder_capital, cfg.reference_price, cfg.dca_exponent, offset_pct, filters, next_index)
        next_index += len(levels)
        new_levels.extend(levels)

        built_capital = sum(l.target_qty() * l.buy_price for l in levels)
        leftover = ladder_capital - built_capital
        if leftover > 1e-9:
            # Too little capital per point to clear min_notional anywhere
            # in the window (or a partial remainder) -- goes into this
            # ladder's OWN reserve rather than vanishing. It's not wasted:
            # reshape_ladder draws on this same pool to reseed the ladder
            # once there's enough to place a real order.
            new_ladder_reserved[name] = new_ladder_reserved.get(name, 0.0) + leftover

        status = f"{len(levels)} levels" if levels else "0 levels -- capital too thin for min_notional, going to its own reserve instead"
        print(f"{name}: window ${lo:.4f}-${hi:.4f}, capital ${ladder_capital:.4f} -> {status}"
              + (f" (${leftover:.4f} to reserve)" if leftover > 1e-9 and levels else ""))

    print()
    print(f"Total new levels: {len(new_levels)}")
    print(f"Final structure: {len(preserve)} preserved + {len(new_levels)} new = {len(preserve) + len(new_levels)} levels")
    if new_ladder_reserved:
        print(f"Seeded into per-ladder reserves (not lost, available for auto top-up once big enough): {new_ladder_reserved}")

    if not args.execute:
        print()
        print("PLAN ONLY -- nothing changed. Re-run with --execute to apply.")
        return

    print()
    print("EXECUTING...")

    backup_path = f"{cfg.state_file}.pre_pct_ladder_{int(time.time())}.bak"
    shutil.copy(cfg.state_file, backup_path)
    print(f"Backed up current state to {backup_path}")

    cancelled = 0
    still_buy_open = []
    for lvl in free_buy_open:
        try:
            if lvl.buy_order_id is not None:
                trader.cancel_order(lvl.buy_order_id)
            cancelled += 1
        except Exception as exc:
            print(f"WARNING: could not cancel level {lvl.index} (order {lvl.buy_order_id}): {exc} "
                  f"-- likely already filled for real, PRESERVING it untouched instead of discarding")
            lvl.state = "sell_open"  # if it just filled, treat it as a real position, not idle/discarded
            still_buy_open.append(lvl)

    final_levels = preserve + still_buy_open + new_levels + other
    final_state = BotState(
        levels=final_levels,
        reserved_total=prior.reserved_total,
        total_profit_realized=prior.total_profit_realized,
        config_signature=new_signature,
        next_level_index=next_index,
        ladder_reserved=dict(new_ladder_reserved),
        ladder_reserved_contributed=dict(new_ladder_reserved),
        ladder_reserved_drawn={},
        pending_reinvest_by_band={},
    )
    save_state(cfg.state_file, final_state)
    print(f"Cancelled {cancelled}/{len(free_buy_open)} buy_open order(s)")
    print(f"Wrote new state: {len(final_state.levels)} total levels")
    print("Restart the bot service now to pick this up (it will see the matching "
          "signature and RESUME, not rebuild).")


if __name__ == "__main__":
    main()
