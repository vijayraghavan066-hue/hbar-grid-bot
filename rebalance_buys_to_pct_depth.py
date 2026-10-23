"""One-time live rebalance (requested 2026-10-23): re-lay each percentage
ladder's buy rungs as a rung at EVERY grid price from just under the current
price down as far as that ladder's own existing buy capital allows at ~$1.05
per rung, so a sudden crash fills buys all the way down.

Same idea as DASH's rebalance_buys_to_depth.py, adapted to the percent
ladders. No new money: each ladder keeps exactly its own buy capital
(buy_open qty*price + idle target), spread over more, deeper rungs. The grid
is geometric from ANCHOR_LOW with that ladder's STEP_PCT (the same lattice the
XRP sell redistribution uses); a rung's sell is buy * (1 + OFFSET_PCT), so
its gap is exactly its ladder's offset.

Per ladder, rungs are laid top-down, each at the $1 exchange minimum with
room (~$1.05, rounded up to the lot) until the capital runs out, the floor
(ANCHOR_LOW) is reached, or the bot's order budget is used up; any remainder
is spread over those rungs with the bot's DCA weights (deeper = a bit larger).
The order budget is MAX_OPEN_ORDERS minus the sells already resting, a small
safety margin and any buys outside the ladders; if the ladders would exceed it
each is trimmed in proportion to its capital so everything stays live.
Existing buy_open orders of the replaced levels are cancelled; the new levels
are idle and the bot arms them (nearest to price first) on restart. Held
sells, bulk and coin-reserve levels are never touched, and new rungs are laid
below the lowest buy price of any held position in the same ladder. A cancel
failure (order probably just filled) leaves that level in place, its price is
treated as occupied, and only the capital actually freed is re-laid.
lifetime_profit is conserved (spread equally).

Usage (stop the bot first -- it rewrites state.json every cycle):
    sudo systemctl stop <bot>-grid-bot
    sudo -u <bot>bot .venv/bin/python rebalance_buys_to_pct_depth.py            # dry run
    sudo -u <bot>bot .venv/bin/python rebalance_buys_to_pct_depth.py --execute  # for real
    sudo systemctl start <bot>-grid-bot
"""
import argparse
import math
import shutil
import subprocess

SAFETY = 1.05        # fresh rungs must clear the $1 minimum with a little room
MARGIN = 5           # order slots left unused under MAX_OPEN_ORDERS
OFFSET_TOL = 0.0005  # same tolerance pct_ladder uses to classify a level's ladder
TOP_BUFFER = 0.9995  # first rung sits just under price


def level_capital(lvl):
    qty = lvl.qty if lvl.state == "buy_open" else lvl.base_qty + lvl.extra_qty
    return qty * lvl.buy_price


def is_own_buy(lvl, offset):
    return (lvl.state in ("buy_open", "idle") and not lvl.one_shot and lvl.buy_price > 0
            and abs((lvl.sell_price / lvl.buy_price - 1.0) - offset) < OFFSET_TOL)


def is_own_held(lvl, offset):
    return (lvl.state == "sell_open" and not lvl.one_shot and lvl.buy_price > 0
            and abs((lvl.sell_price / lvl.buy_price - 1.0) - offset) < OFFSET_TOL)


def min_qty(price, filters):
    step = filters.step_size
    units = math.ceil(filters.min_notional * SAFETY / price / step - 1e-9)
    return filters.round_qty(max(units, 1) * step + 1e-12)


def plan_ladder(capital, price, step_pct, anchor_low, held_buy_prices, filters,
                dca_ref, dca_exp, max_rungs=None, skip_prices=()):
    """Pure. Returns dict(points=[(buy_price, qty)] ascending, spent, floor, top, clipped)."""
    ratio = 1.0 + step_pct
    hi = price * TOP_BUFFER
    if held_buy_prices:
        hi = min(hi, min(held_buy_prices) / ratio + 1e-9)
    if hi <= anchor_low:
        return dict(points=[], spent=0.0, floor=None, top=None, clipped=True)
    k = int(math.floor(math.log(hi / anchor_low) / math.log(ratio) + 1e-9))
    skip = {filters.round_price(p) for p in skip_prices}

    pts, spent, clipped, seen = [], 0.0, False, set()
    while k >= 0:
        p = filters.round_price(anchor_low * ratio ** k)
        k -= 1
        if p in seen or p in skip:
            continue
        if p < anchor_low - 1e-9:
            clipped = True
            break
        q = min_qty(p, filters)
        cost = q * p
        if max_rungs is not None and len(pts) >= max_rungs:
            break
        if spent + cost > capital + 1e-9:
            break
        seen.add(p)
        pts.append([p, q])
        spent += cost
    else:
        clipped = True
    if not pts:
        return dict(points=[], spent=0.0, floor=None, top=round(hi, 10), clipped=clipped)

    extra = capital - spent
    weights = [(dca_ref / p) ** dca_exp for p, _ in pts]
    wsum = sum(weights)
    for i, (p, q) in enumerate(pts):
        add = filters.round_qty(extra * weights[i] / wsum / p)
        pts[i][1] = filters.round_qty(q + add)
    points = sorted((p, q) for p, q in pts)
    spent = sum(p * q for p, q in points)
    return dict(points=points, spent=spent, floor=points[0][0], top=points[-1][0], clipped=clipped)


def build_levels(points, offset, lifetime_profit, first_index):
    from grid import GridLevel
    per = lifetime_profit / len(points) if points else 0.0
    return [GridLevel(index=first_index + k, buy_price=p, sell_price=round(p * (1.0 + offset), 10),
                      base_qty=q, state="idle", lifetime_profit=per)
            for k, (p, q) in enumerate(points)]


def allocate_budget(wanted, capitals, budget):
    """wanted[i] = rungs ladder i could lay; trim proportionally to capital if over budget.
    Returns max_rungs per ladder (None = unconstrained)."""
    if sum(wanted) <= budget:
        return [None] * len(wanted)
    total_cap = sum(capitals) or 1.0
    return [max(1, int(budget * c / total_cap)) if w else 0 for w, c in zip(wanted, capitals)]


def order_budget(levels, max_open_orders, own_buy_ids):
    """Slots available to the ladders' buys: cap - resting sells - margin - other resting buys."""
    sells = sum(1 for l in levels if l.state == "sell_open" and not getattr(l, "parked", False))
    other_buys = sum(1 for l in levels if l.state == "buy_open" and id(l) not in own_buy_ids)
    return max(0, max_open_orders - sells - MARGIN - other_buys)


def plan_all(levels, price, specs, cfg, filters):
    own_by_ladder = {}
    for n, (step, offset) in enumerate(specs, start=1):
        own_by_ladder[n] = [l for l in levels if is_own_buy(l, offset)]
    own_ids = {id(l) for ls in own_by_ladder.values() for l in ls}
    budget = order_budget(levels, cfg.max_open_orders, own_ids) if levels else 0
    inputs = {}
    for n, (step, offset) in enumerate(specs, start=1):
        held = [l.buy_price for l in levels if is_own_held(l, offset)]
        inputs[n] = (sum(level_capital(l) for l in own_by_ladder[n]), held)

    def plan_with(maxes):
        out = {}
        for n, (step, offset) in enumerate(specs, start=1):
            cap, held = inputs[n]
            p = plan_ladder(cap, price, step, cfg.anchor_low, held, filters, cfg.reference_price,
                            cfg.dca_exponent, max_rungs=maxes[n - 1])
            out[n] = dict(own=own_by_ladder[n], capital=cap, step=step, offset=offset, **p)
        return out

    first = plan_with([None] * len(specs))
    maxes = allocate_budget([len(first[n]["points"]) for n in first], [inputs[n][0] for n in first], budget)
    plans = plan_with(maxes) if any(m is not None for m in maxes) else first
    return plans, budget


def apply_plan(state, price, specs, cfg, filters, trader):
    """Cancels live buys of the replaced levels, then re-plans on the capital
    actually freed. Returns (new_levels, failures)."""
    failures, new_all = [], []
    plans, _ = plan_all(state.levels, price, specs, cfg, filters)  # budget/max_rungs decided pre-cancel
    for n, (step, offset) in enumerate(specs, start=1):
        own = [l for l in state.levels if is_own_buy(l, offset)]
        held = [l.buy_price for l in state.levels if is_own_held(l, offset)]
        removed, kept = [], []
        for l in own:
            if l.state == "buy_open" and l.buy_order_id is not None:
                try:
                    trader.cancel_order(l.buy_order_id)
                except Exception as exc:
                    failures.append((l.index, str(exc)))
                    kept.append(l)
                    continue
            removed.append(l)
        capital = sum(level_capital(l) for l in removed)
        max_rungs = len(plans[n]["points"])
        p = plan_ladder(capital, price, step, cfg.anchor_low, held, filters, cfg.reference_price,
                        cfg.dca_exponent, max_rungs=max_rungs, skip_prices=[l.buy_price for l in kept])
        new = build_levels(p["points"], offset, sum(l.lifetime_profit for l in removed), state.next_level_index)
        state.next_level_index += len(new)
        gone = {id(l) for l in removed}
        state.levels = [l for l in state.levels if id(l) not in gone] + new
        new_all.extend(new)
    return new_all, failures


def verify(plans, cfg, filters):
    problems = []
    for n, p in plans.items():
        for price, qty in p["points"]:
            if qty * price < filters.min_notional - 1e-9:
                problems.append(f"L{n} {price:g} below the minimum notional")
            if price < cfg.anchor_low - 1e-9:
                problems.append(f"L{n} {price:g} below ANCHOR_LOW")
            sell = round(price * (1.0 + p["offset"]), 10)
            if abs((sell / price - 1.0) - p["offset"]) >= OFFSET_TOL:
                problems.append(f"L{n} {price:g} gap misses its offset")
        if p["spent"] > p["capital"] + 1e-6:
            problems.append(f"L{n} spends more than its capital")
    return problems


def print_plan(plans, price, budget, levels, cfg):
    print(f"Price {price}; ANCHOR_LOW {cfg.anchor_low:g}")
    total = 0
    for n, p in plans.items():
        total += len(p["points"])
        if not p["points"]:
            print(f"  Ladder {n}: nothing to lay (capital ${p['capital']:.2f})")
            continue
        sizes = [q * pr for pr, q in p["points"]]
        print(f"  Ladder {n} (+{p['offset'] * 100:g}%, {p['step'] * 100:g}% grid): {len(p['own'])} buys now -> "
              f"{len(p['points'])} rungs {p['floor']:g}-{p['top']:g} "
              f"({(1 - p['floor'] / price) * 100:.0f}% below price), "
              f"${p['spent']:.2f} of ${p['capital']:.2f}, ${min(sizes):.2f}-${max(sizes):.2f} per rung")
    sells = sum(1 for l in levels if l.state == "sell_open")
    print(f"Total buy rungs: {total} (budget {budget}); resting sells {sells}; "
          f"order cap {cfg.max_open_orders} -> {total + sells}/{cfg.max_open_orders}")


def main():
    from binance_client import BinanceUSTrader
    from config import load_config
    from pct_ladder import ladder_specs
    from state import load_state, save_state

    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    state = load_state(cfg.state_file)
    if state is None:
        raise SystemExit(f"No state file at {cfg.state_file}")
    trader = BinanceUSTrader(cfg.api_key, cfg.api_secret, cfg.symbol, cfg.dry_run)
    filters = trader.get_symbol_filters()
    price = trader.get_price()
    specs = [(s, o) for s, o, _, _ in ladder_specs(cfg)]
    plans, budget = plan_all(state.levels, price, specs, cfg, filters)
    print_plan(plans, price, budget, state.levels, cfg)
    problems = verify(plans, cfg, filters)
    print("Checks:", "all passed (>= minimum notional, above ANCHOR_LOW, gap exact, within each ladder's own capital)"
          if not problems else problems)
    if problems:
        raise SystemExit("refusing to continue")
    if not args.execute:
        print("\nDry run only -- nothing changed. Re-run with --execute (bot must be stopped).")
        return
    service = f"{cfg.symbol.lower().replace('usdt', '').replace('usd', '')}-grid-bot"
    active = subprocess.run(["systemctl", "is-active", service], capture_output=True, text=True).stdout.strip()
    if active == "active":
        raise SystemExit(f"{service} is still running -- stop it first (it rewrites state.json).")
    shutil.copy(cfg.state_file, cfg.state_file + ".pre_pct_buy_depth.bak")
    new, failures = apply_plan(state, price, specs, cfg, filters, trader)
    save_state(cfg.state_file, state)
    print(f"\nDone. {len(new)} buy rungs laid; {len(failures)} cancel failure(s) left in place: {failures}")
    print(f"Backup at {cfg.state_file}.pre_pct_buy_depth.bak")
    print("Restart the bot -- it should log 'Resumed prior state (config unchanged)'.")


if __name__ == "__main__":
    main()
