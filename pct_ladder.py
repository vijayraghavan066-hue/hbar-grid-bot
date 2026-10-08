"""Percentage-based, price-following, 3-ladder buy/sell structure for a
sub-$1 coin (HBAR) -- adapted from dash-grid-bot's ladder_window.py, with
two deliberate differences:

1. Steps and offsets are PERCENTAGES of price, not fixed dollar amounts.
   DASH's ladders use $1 steps on a ~$55 coin (~1.8%); HBAR trades at
   ~$0.09, where a $1 step would span the bot's entire anchor range. A
   fixed-dollar step cannot work across HBAR's price scale.

2. Each ladder gets its OWN separate reserve pool (`state.ladder_reserved`,
   keyed by ladder name), not one shared `reserved_total`. DASH's shared
   pool was found to pool CORRELATED risk, not diversified risk -- when
   price falls, every ladder on the same coin gets stressed at the same
   time, so a shared reserve just means every ladder competes for the
   same shrinking pot during exactly the moment it matters most (observed
   live on DASH: 3-4 of 5 ladders below threshold simultaneously during
   one decline). Separate pools mean each ladder's safety net is sized
   and funded independently, with no cross-ladder drain.

Each ladder's zone is a ONE-SIDED window ending exactly at current price,
same reasoning as DASH's 2026-09-28 change: a resting buy order above
current price can never legitimately fill (it would cross the spread and
execute as a taker), so a symmetric window always pre-builds roughly half
its own capital above price -- permanently idle unless price rises back
past it. A one-sided window doubles real downward reach for the same
capital and keeps every rung positioned to become a real order as price
falls toward it.
"""
import logging
import math

from zone_shift import _cancel_and_collect

log = logging.getLogger("hbar-grid-bot")

OFFSET_TOLERANCE_PCT = 0.0005  # 0.05 percentage points -- float-safe matching of a level's own gap to its ladder's offset_pct


def _band_key(offset_pct: float) -> str:
    """Stable dict key for a percentage offset, e.g. 0.02 -> '0.0200'."""
    return f"{offset_pct:.4f}"


def _exact_ladder_match(offset_pct: float, cfg):
    """Which of the 3 ladders a level's own (sell/buy - 1) gap EXACTLY
    matches (within OFFSET_TOLERANCE_PCT), or None if it matches none of
    them. A real rung built by reshape_ladder always has a gap matching its
    own ladder's offset_pct exactly, so this reliably distinguishes "this
    position IS one of the 3 ladders" from "this is a legacy position held
    over from whatever design preceded them"."""
    for _, ladder_offset_pct, _, name in ladder_specs(cfg):
        if abs(ladder_offset_pct - offset_pct) < OFFSET_TOLERANCE_PCT:
            return name
    return None


def distribute_profit_ladder_aware(state, profit: float, sold_offset_pct: float, cfg) -> str:
    """Replaces reinvest.py's distribute_profit for the new ladder system:
    reserve_ratio of profit goes into reserve, the remainder is queued as
    pending_reinvest_by_band, picked up by reshape_ladder's future_pool on
    its next cycle (reusing the existing gap-filling logic rather than
    placing orders directly here).

    A position whose gap EXACTLY matches one of the 3 ladders (a real rung
    of that ladder) has its ENTIRE profit routed to that one ladder, as
    always.

    A position whose gap matches NONE of them -- a legacy position held
    over from the OLD single-dense-zone design (the 7 positions at ~4.6%,
    i.e. DENSE_SELL_OFFSET/DENSE_LOW, ~$25 cost basis total) -- has its
    profit SPLIT EVENLY across all 3 ladders instead of funneling to
    whichever is merely closest (previously ladder2's 4%). Same fix applied
    to XRP's 5-ladder version 2026-10-08 after it was found there that
    "nearest ladder" routing would starve the other ladders; applied here
    on explicit request since the identical mechanism exists for HBAR's own
    legacy positions, even though the dollar amounts here are small.

    Returns the ladder name profit was routed to (exact-match case), or the
    literal string "legacy-even-split" (even-split case), for logging."""
    if profit <= 0:
        return ""

    specs = ladder_specs(cfg)
    state.total_profit_realized += profit
    exact_name = _exact_ladder_match(sold_offset_pct, cfg)

    if exact_name is not None:
        reserve_amount = profit * cfg.profit_reserve_ratio
        distribute_amount = profit - reserve_amount
        state.ladder_reserved[exact_name] = state.ladder_reserved.get(exact_name, 0.0) + reserve_amount
        state.ladder_reserved_contributed[exact_name] = state.ladder_reserved_contributed.get(exact_name, 0.0) + reserve_amount
        band_key = _band_key(sold_offset_pct)
        state.pending_reinvest_by_band[band_key] = state.pending_reinvest_by_band.get(band_key, 0.0) + distribute_amount
        log.info(f"pct_ladder: profit {profit:.4f} attributed to {exact_name} (exact offset match) -- "
                 f"{reserve_amount:.4f} to its own reserve (now ${state.ladder_reserved[exact_name]:.4f}), "
                 f"{distribute_amount:.4f} queued for its next reshape")
        return exact_name

    share = profit / len(specs)
    for _, ladder_offset_pct, _, name in specs:
        reserve_amount = share * cfg.profit_reserve_ratio
        distribute_amount = share - reserve_amount
        state.ladder_reserved[name] = state.ladder_reserved.get(name, 0.0) + reserve_amount
        state.ladder_reserved_contributed[name] = state.ladder_reserved_contributed.get(name, 0.0) + reserve_amount
        band_key = _band_key(ladder_offset_pct)
        state.pending_reinvest_by_band[band_key] = state.pending_reinvest_by_band.get(band_key, 0.0) + distribute_amount
    log.info(f"pct_ladder: profit {profit:.4f} from a legacy position (offset {sold_offset_pct:.4f}, "
             f"no exact ladder match) -- split evenly across all {len(specs)} ladders (${share:.4f} each before reserve split)")
    return "legacy-even-split"


def ladder_specs(cfg):
    """(step_pct, offset_pct, window_pct, name) for ladders 1-3, in order.
    step_pct/offset_pct/window_pct are fractions (0.01 = 1%), not percent
    points, matching this project's existing _pct config convention."""
    return [
        (cfg.ladder1_step_pct, cfg.ladder1_offset_pct, cfg.ladder1_window_pct, "ladder1"),
        (cfg.ladder2_step_pct, cfg.ladder2_offset_pct, cfg.ladder2_window_pct, "ladder2"),
        (cfg.ladder3_step_pct, cfg.ladder3_offset_pct, cfg.ladder3_window_pct, "ladder3"),
    ]


def compute_ladder_zones(current_price, cfg):
    """Returns {name: (lo, hi)} -- one one-sided window per ladder, each
    [current_price * (1 - window_pct), current_price], clipped to
    [anchor_low, anchor_high]. Unlike DASH's whole-dollar-rounded center,
    this uses current_price directly -- HBAR's sub-$1 scale makes whole-
    unit rounding meaningless."""
    zones = {}
    for step_pct, offset_pct, window_pct, name in ladder_specs(cfg):
        hi = min(current_price, cfg.anchor_high)
        lo = max(current_price * (1.0 - window_pct), cfg.anchor_low)
        zones[name] = (lo, hi)
    return zones


def build_ladder_points(lo, hi, step_pct):
    """Geometric (percentage) spacing price points from lo to hi inclusive,
    each step_pct above the previous -- NOT arithmetic, since a fixed
    dollar step is meaningless across a range that itself spans a 10x
    price ratio (HBAR's anchor range is $0.02-$0.20)."""
    if lo <= 0 or hi <= 0 or hi < lo:
        return []
    if step_pct <= 0:
        raise ValueError("step_pct must be positive")
    points = []
    p = lo
    ratio = 1.0 + step_pct
    # ceiling-safe loop count so the last point lands at or just above hi
    n = int(math.floor(math.log(hi / lo) / math.log(ratio) + 1e-9)) + 1 if hi > lo else 0
    for i in range(n + 1):
        points.append(round(lo * (ratio ** i), 10))
    return [p for p in points if p <= hi + 1e-9]


def _in_range(price, lo, hi):
    return lo - 1e-9 <= price <= hi + 1e-9


def _is_stuck_idle(lvl, current_price, min_notional):
    """An idle level whose buy price is AT OR ABOVE current price can
    never fill without crossing the spread as a taker -- same condition
    DASH's ladder_window.py uses. Also flags a level whose target notional
    has decayed (via repeated partial fills/float drift) below the
    exchange minimum, which would otherwise sit forever rejecting."""
    if lvl.state != "idle":
        return False
    if lvl.buy_price >= current_price:
        return True
    notional = lvl.target_qty() * lvl.buy_price
    return 0 < notional < min_notional


def _draw_from_ladder_reserve(state, cfg, name, max_amount=None):
    """Draws from state.ladder_reserved[name] -- THIS ladder's own pool
    only, never another ladder's. Capped so the pool never drops below
    reserved_draw_floor_pct of its OWN value at the moment of THIS draw
    (geometric decay toward, never reaching, zero across repeated draws
    during a sustained decline) -- same safety property as DASH's shared-
    pool version, just scoped per ladder now."""
    available = state.ladder_reserved.get(name, 0.0)
    if available <= 0:
        return 0.0
    drawable = available * (1.0 - cfg.reserved_draw_floor_pct)
    draw = min(drawable, max_amount) if max_amount is not None else drawable
    draw = max(0.0, draw)
    if draw <= 0:
        return 0.0
    state.ladder_reserved[name] = available - draw
    state.ladder_reserved_drawn[name] = state.ladder_reserved_drawn.get(name, 0.0) + draw
    return draw


def _dca_build_levels(points, total_capital, reference_price, dca_exponent, offset_pct, filters, start_index):
    """Equal-ratio DCA weighting: cheaper points get a bigger USDT share,
    weight = (reference_price / buy_price) ** dca_exponent -- same formula
    this project's other bots already use. Sell price is buy_price scaled
    up by (1 + offset_pct), not a dollar addition."""
    if not points:
        return []
    weights = [(reference_price / p) ** dca_exponent for p in points]
    total_weight = sum(weights)
    from grid import GridLevel
    levels = []
    for i, buy_price in enumerate(points):
        usdt_alloc = total_capital * weights[i] / total_weight
        qty = usdt_alloc / buy_price
        qty = filters.round_qty(qty) if hasattr(filters, "round_qty") else qty
        if qty <= 0:
            continue
        sell_price = round(buy_price * (1.0 + offset_pct), 10)
        levels.append(GridLevel(
            index=start_index + i, buy_price=buy_price, sell_price=sell_price, base_qty=qty,
        ))
    return levels


def _thin_to_affordable(points, total_capital, reference_price, dca_exponent, min_notional):
    """Drops points whose DCA-weighted share would fall under min_notional
    -- same purpose as DASH's _thin_to_affordable, prevents building levels
    the exchange would reject outright."""
    if not points:
        return points
    weights = [(reference_price / p) ** dca_exponent for p in points]
    total_weight = sum(weights)
    kept = [p for p, w in zip(points, weights) if total_capital * w / total_weight >= min_notional]
    return kept


def reshape_ladder(state, name, lo, hi, step_pct, offset_pct, trader, filters, cfg, current_price):
    """Cancels any idle/buy_open level belonging to THIS ladder (matched
    by its own (sell_price/buy_price - 1) gap vs offset_pct) that no
    longer belongs in the current window or is structurally stuck, pools
    that capital with this ladder's own future-buy pool and any reserve
    draw, fills genuine gaps in the window first, and only grows existing
    survivors once the window is fully covered. Held (sell_open) and any
    one_shot levels are never inspected -- a cancel failure (already
    filled for real) leaves that level completely untouched."""
    band_key = _band_key(offset_pct)
    future_pool = state.pending_reinvest_by_band.get(band_key, 0.0)

    def needs_reshaping(lvl):
        return not _in_range(lvl.buy_price, lo, hi) or _is_stuck_idle(lvl, current_price, filters.min_notional)

    same_ladder = [lvl for lvl in state.levels
                   if not lvl.one_shot and lvl.state in ("idle", "buy_open")
                   and abs((lvl.sell_price / lvl.buy_price - 1.0) - offset_pct) < OFFSET_TOLERANCE_PCT]
    mismatched = [lvl for lvl in same_ladder if needs_reshaping(lvl)]
    own_capital = sum(lvl.target_qty() * lvl.buy_price for lvl in same_ladder)

    reserve_draw = 0.0
    if future_pool <= 0:
        if not same_ladder:
            reserve_draw = _draw_from_ladder_reserve(state, cfg, name)
            if reserve_draw > 0:
                log.warning(f"pct_ladder: {name} is completely dry -- drew ${reserve_draw:.4f} "
                            f"from its own reserve (${state.ladder_reserved.get(name, 0.0):.4f} remaining)")
        elif own_capital < cfg.reserved_topup_threshold_usd:
            needed = cfg.reserved_topup_threshold_usd - own_capital
            reserve_draw = _draw_from_ladder_reserve(state, cfg, name, max_amount=needed)
            if reserve_draw > 0:
                log.warning(f"pct_ladder: {name} low (${own_capital:.4f} < ${cfg.reserved_topup_threshold_usd:.2f}) "
                            f"-- drew ${reserve_draw:.4f} from its own reserve")

    if not mismatched and future_pool <= 0 and reserve_draw <= 0:
        return 0

    collected = _cancel_and_collect(mismatched, trader, "buy_order_id") if mismatched else []
    if mismatched and not collected and future_pool <= 0 and reserve_draw <= 0:
        return 0

    total_capital = sum(l.target_qty() * l.buy_price for l in collected) + future_pool + reserve_draw
    if future_pool > 0:
        state.pending_reinvest_by_band[band_key] = 0.0
    for lvl in collected:
        state.levels.remove(lvl)
    if total_capital <= 0:
        return 0

    matching = [lvl for lvl in state.levels
                if not lvl.one_shot and lvl.state in ("idle", "buy_open")
                and abs((lvl.sell_price / lvl.buy_price - 1.0) - offset_pct) < OFFSET_TOLERANCE_PCT
                and not needs_reshaping(lvl)]
    all_points = build_ladder_points(lo, hi, step_pct)
    covered = {round(lvl.buy_price, 10) for lvl in matching}
    gap_points = [p for p in all_points if round(p, 10) not in covered and p < current_price]

    if gap_points:
        gap_points = _thin_to_affordable(gap_points, total_capital, cfg.reference_price, cfg.dca_exponent, filters.min_notional)
        fresh = _dca_build_levels(gap_points, total_capital, cfg.reference_price, cfg.dca_exponent,
                                   offset_pct, filters, state.next_level_index)
        if fresh:
            state.next_level_index += len(fresh)
            state.levels.extend(fresh)
            log.info(f"pct_ladder: {name} reshaped {len(collected)} level(s) (+${future_pool:.4f} future-buy, "
                     f"+${reserve_draw:.4f} from reserve) into {len(fresh)} fresh level(s) (${total_capital:.4f}), "
                     f"{len(matching)} survivor(s) untouched")
            return len(fresh)
        # gap_points existed before thinning/min-notional filtering but
        # nothing survived to build -- fall through instead of discarding
        # total_capital (see the no-op branch below for why this matters).

    if matching:
        per_level = total_capital / len(matching)
        for lvl in matching:
            extra_qty = per_level / lvl.buy_price
            lvl.extra_qty += extra_qty
        log.info(f"pct_ladder: {name} reshaped {len(collected)} level(s) by growing {len(matching)} "
                 f"existing level(s) (${total_capital:.4f} added, window already fully covered)")
        return 0

    # Nothing buildable (too thin to clear min_notional) and no survivor to
    # grow either -- return the capital to where it came from instead of
    # letting it vanish. Found live 2026-10-08: a ladder stuck below
    # reserved_topup_threshold_usd with no levels of its own kept drawing a
    # top-up every single cycle that was too small to ever build a real
    # order, silently discarding the draw each time and draining its
    # reserve to near zero within minutes of normal operation.
    if reserve_draw > 0:
        state.ladder_reserved[name] = state.ladder_reserved.get(name, 0.0) + reserve_draw
        # Undo the ladder_reserved_drawn increment _draw_from_ladder_reserve
        # just made for this same draw -- otherwise a ladder stuck in this
        # draw-then-return loop inflates its "all-time drawn" figure by the
        # same amount every single poll cycle forever, even though no real
        # capital is actually lost (it's returned right here). Found live
        # 2026-10-08 on Ladder 3: $3,108 "drawn" against $5.92 ever
        # contributed, from an extended run of this exact loop. Never driven
        # below zero -- this exactly reverses the increment from THIS call.
        state.ladder_reserved_drawn[name] = max(
            0.0, state.ladder_reserved_drawn.get(name, 0.0) - reserve_draw)
    if future_pool > 0:
        state.pending_reinvest_by_band[band_key] = state.pending_reinvest_by_band.get(band_key, 0.0) + future_pool
    if reserve_draw > 0 or future_pool > 0:
        log.info(f"pct_ladder: {name} drew ${total_capital:.4f} but had nothing viable to build "
                 f"or grow -- returned to its own reserve/pending pool untouched")
    return 0


def maybe_reshape_ladders(state, current_price, trader, filters, cfg):
    """Runs every poll cycle, same as DASH's maybe_reshape_ladders.
    Deliberately NOT gated on price having moved -- a ladder can get
    stuck while price just sits still (capital eroded under
    min_notional), and reshape_ladder is a cheap no-op when nothing is
    actually wrong."""
    if not cfg.pct_ladder_enabled:
        return 0
    zones = compute_ladder_zones(current_price, cfg)
    total_reshaped = 0
    for step_pct, offset_pct, window_pct, name in ladder_specs(cfg):
        lo, hi = zones[name]
        if hi <= lo:
            continue
        total_reshaped += reshape_ladder(state, name, lo, hi, step_pct, offset_pct, trader, filters, cfg, current_price)
    return total_reshaped
