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
import time

from zone_shift import _cancel_and_collect

log = logging.getLogger("hbar-grid-bot")

OFFSET_TOLERANCE_PCT = 0.0005  # 0.05 percentage points -- float-safe matching of a level's own gap to its ladder's offset_pct

# Below this, a ladder's reserve is treated as fully exhausted rather than
# left to the floor-capped draw's geometric decay toward (never reaching)
# zero -- found live 2026-10-13: left unchecked, a long-dry ladder's reserve
# can decay to subatomic float dust (1e-96, 1e-210 seen on DASH/HBAR) and
# keep triggering a no-op reshape every single poll cycle forever.
_NEGLIGIBLE_RESERVE_USD = 1e-6


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


def distribute_profit_ladder_aware(state, profit: float, sold_offset_pct: float, cfg, trader, filters) -> str:
    """Replaces reinvest.py's distribute_profit for the new ladder system:
    every sale's profit is split 4 ways via _split_profit_four_ways --
    bot-wide accumulation (never drawn), buy-side compounding, this
    ladder's own USD reserve (still auto-drawable), and this ladder's own
    coin reserve.

    A position whose gap EXACTLY matches one of the 5 ladders (a real rung
    of that ladder) has its ENTIRE profit routed to that one ladder, as
    always.

    A position whose gap matches NONE of them -- a legacy position held
    over from the OLD single-dense-zone design (the 7 positions at ~4.6%,
    i.e. DENSE_SELL_OFFSET/DENSE_LOW, ~$25 cost basis total) -- has its
    profit SPLIT EVENLY across all 5 ladders instead of funneling to
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
        _split_profit_four_ways(state, exact_name, profit, trader, filters, cfg)
        return exact_name

    share = profit / len(specs)
    for _, ladder_offset_pct, _, name in specs:
        _split_profit_four_ways(state, name, share, trader, filters, cfg)
    log.info(f"pct_ladder: profit {profit:.4f} from a legacy position (offset {sold_offset_pct:.4f}, "
             f"no exact ladder match) -- split evenly across all {len(specs)} ladders (${share:.4f} each, "
             f"see per-ladder log lines above)")
    return "legacy-even-split"


SELF_COMPOUND_LADDERS = ("ladder2", "ladder3", "ladder4", "ladder5")


def is_self_compound_rung(lvl, cfg) -> bool:
    """True for a real rung of Ladders 2-5 (gap EXACTLY matches that ladder's
    offset_pct). Ladder 1, bulk, coin-reserve and legacy positions are not."""
    if not cfg.pct_ladder_enabled or lvl.one_shot or lvl.from_coin_reserve or lvl.buy_price <= 0:
        return False
    return _exact_ladder_match(lvl.sell_price / lvl.buy_price - 1.0, cfg) in SELF_COMPOUND_LADDERS


def self_compound_profit(state, lvl, profit: float) -> None:
    """Ladders 2-5 only (2026-10-23, requested by Vivaan, same as DASH): 100%
    of a rung's own profit grows that SAME rung's extra_qty -- no 4-way split,
    no accum/ladder-reserve/coin-reserve growth and no USDC-protection share
    from these four ladders any more. Ladder 1 keeps the 4-way split.

    `lvl` is the level that just sold; the caller has already reset it to idle,
    so apply_order_window re-arms it next cycle at the same buy price with the
    larger target_qty(). total_profit_realized is incremented here because
    this path bypasses distribute_profit_ladder_aware."""
    if profit <= 0:
        return
    state.total_profit_realized += profit
    lvl.extra_qty += profit / lvl.buy_price
    lvl.last_reinvest_usd = profit
    lvl.last_reinvest_ts = time.time()
    log.info(f"pct_ladder: Grid {lvl.index} (${lvl.buy_price}): ladder2-5 self-compound -- "
             f"${profit:.4f} profit grew this rung's own extra_qty to {lvl.extra_qty:.6f} "
             f"(no split, no reserve/coin/USDC growth)")


def ladder_specs(cfg):
    """(step_pct, offset_pct, window_pct, name) for ladders 1-5, in order.
    step_pct/offset_pct/window_pct are fractions (0.01 = 1%), not percent
    points, matching this project's existing _pct config convention."""
    return [
        (cfg.ladder1_step_pct, cfg.ladder1_offset_pct, cfg.ladder1_window_pct, "ladder1"),
        (cfg.ladder2_step_pct, cfg.ladder2_offset_pct, cfg.ladder2_window_pct, "ladder2"),
        (cfg.ladder3_step_pct, cfg.ladder3_offset_pct, cfg.ladder3_window_pct, "ladder3"),
        (cfg.ladder4_step_pct, cfg.ladder4_offset_pct, cfg.ladder4_window_pct, "ladder4"),
        (cfg.ladder5_step_pct, cfg.ladder5_offset_pct, cfg.ladder5_window_pct, "ladder5"),
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


def _draw_from_ladder_reserve(state, cfg, name, max_amount=None, ignore_floor=False):
    """Draws from state.ladder_reserved[name] -- THIS ladder's own pool
    only, never another ladder's. Capped so the pool never drops below
    reserved_draw_floor_pct of its OWN value at the moment of THIS draw
    (geometric decay toward, never reaching, zero across repeated draws
    during a sustained decline) -- same safety property as DASH's shared-
    pool version, just scoped per ladder now -- UNLESS ignore_floor=True,
    used only for the completely-dry emergency-bootstrap case in
    reshape_ladder: a ladder that can never place a single order can also
    never earn the profit that would replenish its own reserve, so it
    stays stuck at zero rungs forever regardless of how much sits in
    reserve. Escaping that dead end takes priority over the normal
    retention floor."""
    available = state.ladder_reserved.get(name, 0.0)
    if available <= _NEGLIGIBLE_RESERVE_USD:
        if available > 0:
            state.ladder_reserved[name] = 0.0  # snap dust to exactly zero, stop the decay loop for good
        return 0.0
    drawable = available if ignore_floor else available * (1.0 - cfg.reserved_draw_floor_pct)
    draw = min(drawable, max_amount) if max_amount is not None else drawable
    draw = max(0.0, draw)
    if draw <= 0:
        return 0.0
    remainder = available - draw
    state.ladder_reserved[name] = 0.0 if remainder <= _NEGLIGIBLE_RESERVE_USD else remainder
    state.ladder_reserved_drawn[name] = state.ladder_reserved_drawn.get(name, 0.0) + draw
    return draw


def _min_capital_for_one_level(lo, hi, step_pct, reference_price, dca_exponent, min_notional):
    """Smallest total_capital that would let the single best (highest-DCA-
    weight) point in this window clear min_notional, under the same
    weighting _dca_build_levels uses. Lets reshape_ladder tell, before
    drawing anything, whether the normal floor-capped reserve draw would
    be enough to escape a completely-dry ladder, or whether this specific
    bootstrap needs to draw past the floor -- see _draw_from_ladder_reserve's
    ignore_floor."""
    points = build_ladder_points(lo, hi, step_pct)
    if not points:
        return float("inf")
    weights = [(reference_price / p) ** dca_exponent for p in points]
    best_weight = max(weights)
    total_weight = sum(weights)
    return min_notional * total_weight / best_weight


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
    """Fills genuine gaps in the current window using this ladder's own
    future-buy pool and reserve draw, then (only once the window is fully
    covered) grows existing survivors. A real resting order (buy_open), once
    live, is NEVER cancelled just because the window has since drifted past
    it (2026-10-15, explicit request: coverage should only ever grow, never
    churn -- a rung earned its spot, the window moving on doesn't evict it).
    The only levels ever collected are ones that were never real orders to
    begin with: idle levels that are structurally stuck (would cross the
    spread as a taker, or have decayed below min_notional). Held (sell_open)
    and any one_shot levels are never inspected -- a cancel failure (already
    filled for real) leaves that level completely untouched."""
    band_key = _band_key(offset_pct)
    future_pool = state.pending_reinvest_by_band.get(band_key, 0.0)

    def needs_reshaping(lvl):
        return _is_stuck_idle(lvl, current_price, filters.min_notional)

    same_ladder = [lvl for lvl in state.levels
                   if not lvl.one_shot and lvl.state in ("idle", "buy_open")
                   and abs((lvl.sell_price / lvl.buy_price - 1.0) - offset_pct) < OFFSET_TOLERANCE_PCT]
    mismatched = [lvl for lvl in same_ladder if needs_reshaping(lvl)]
    own_capital = sum(lvl.target_qty() * lvl.buy_price for lvl in same_ladder)

    reserve_draw = 0.0
    if future_pool <= 0:
        if not same_ladder:
            available = state.ladder_reserved.get(name, 0.0)
            needed_min = _min_capital_for_one_level(lo, hi, step_pct, cfg.reference_price,
                                                      cfg.dca_exponent, filters.min_notional)
            normal_preview = available * (1.0 - cfg.reserved_draw_floor_pct)
            if normal_preview < needed_min <= available:
                # The normal floor-respecting draw would leave this ladder
                # stuck at zero rungs indefinitely (it can never earn the
                # profit that would replenish its own reserve while dry),
                # but the full reserve would be enough to place one real
                # order -- bootstrap takes priority over retention here.
                reserve_draw = _draw_from_ladder_reserve(state, cfg, name, ignore_floor=True)
                if reserve_draw > 0:
                    log.warning(f"pct_ladder: {name} emergency bootstrap -- the normal floor-capped "
                                f"draw (${normal_preview:.4f}) wasn't enough to place a single order "
                                f"(needs ${needed_min:.4f}); drew the full remaining reserve "
                                f"(${reserve_draw:.4f}) instead of staying stuck at zero rungs")
            else:
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

    collected_capital = sum(l.target_qty() * l.buy_price for l in collected)
    total_capital = collected_capital + future_pool + reserve_draw
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
        now = time.time()
        for lvl in matching:
            extra_qty = per_level / lvl.buy_price
            lvl.extra_qty += extra_qty
            if per_level > 1e-6:  # skip float-dust "growths" (seen live: 4e-95) -- not a real reinvestment
                lvl.last_reinvest_usd = per_level
                lvl.last_reinvest_ts = now
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
    if collected_capital > 0:
        state.ladder_reserved[name] = state.ladder_reserved.get(name, 0.0) + collected_capital
    if future_pool > 0:
        state.pending_reinvest_by_band[band_key] = state.pending_reinvest_by_band.get(band_key, 0.0) + future_pool
    if reserve_draw > 0 or future_pool > 0 or collected_capital > 0:
        log.info(f"pct_ladder: {name} drew ${total_capital:.4f} but had nothing viable to build "
                 f"or grow -- returned to its own reserve/pending pool untouched")
    return 0


EXTEND_BATCH_SIZE = 10  # how many new points below current coverage to consider per cycle --
# bounded so DCA weighting (which favors cheaper points) can't skip the nearest gap to fund a
# far-cheaper one instead; same reasoning every other point list in this module already relies
# on being geometrically local. Explicit example figure from the 2026-10-15 request.


def extend_ladder_coverage(state, name, step_pct, offset_pct, cfg, filters):
    """As a ladder's own reserve grows, proactively extends real buy-side
    coverage DOWNWARD past whatever its lowest rung has ever reached,
    toward cfg.anchor_low -- independent of the current sliding window,
    and never touching or rebuilding any existing rung (2026-10-15,
    explicit request: every rung from a ladder's lowest point up to
    current price should eventually carry a real order, funded only by
    that ladder's own reserve as it accrues, never by moving capital out
    of rungs that already exist). Works in bounded batches of up to
    EXTEND_BATCH_SIZE points, starting right where the last batch left
    off, so later cycles keep inching the floor down as reserve regrows."""
    existing = [lvl for lvl in state.levels
                if not lvl.one_shot and lvl.state in ("idle", "buy_open", "sell_open")
                and abs((lvl.sell_price / lvl.buy_price - 1.0) - offset_pct) < OFFSET_TOLERANCE_PCT]
    if not existing:
        return 0  # nothing built yet for this ladder -- reshape_ladder's own bootstrap handles that first
    lowest = min(lvl.buy_price for lvl in existing)
    if lowest <= cfg.anchor_low:
        return 0  # already reached the floor

    new_points = []
    p = lowest / (1.0 + step_pct)
    while p >= cfg.anchor_low and len(new_points) < EXTEND_BATCH_SIZE:
        new_points.append(round(p, 10))
        p = p / (1.0 + step_pct)
    if not new_points:
        return 0

    reserve_draw = _draw_from_ladder_reserve(state, cfg, name)
    if reserve_draw <= 0:
        return 0

    thinned = _thin_to_affordable(new_points, reserve_draw, cfg.reference_price, cfg.dca_exponent, filters.min_notional)
    fresh = _dca_build_levels(thinned, reserve_draw, cfg.reference_price, cfg.dca_exponent,
                               offset_pct, filters, state.next_level_index)
    if not fresh:
        # Too thin to build anything real this cycle -- return the draw
        # untouched rather than losing it (same undo pattern reshape_ladder
        # uses for its own no-op branch).
        state.ladder_reserved[name] = state.ladder_reserved.get(name, 0.0) + reserve_draw
        state.ladder_reserved_drawn[name] = max(0.0, state.ladder_reserved_drawn.get(name, 0.0) - reserve_draw)
        return 0

    state.next_level_index += len(fresh)
    state.levels.extend(fresh)
    log.info(f"pct_ladder: {name} extended coverage downward with {len(fresh)} new rung(s) "
             f"(${reserve_draw:.4f} from reserve), lowest now ${min(l.buy_price for l in fresh):.6f}")
    return len(fresh)


def maybe_fund_sell_from_reserve(state, current_price, trader, filters, cfg):
    """Runs every poll cycle, after maybe_reshape_ladders. A ladder can only
    ever have something to sell once one of its own buy rungs has actually
    filled -- unlike the buy side, there's no reserve a ladder can draw on
    to conjure up a sell order, because a sell order requires actually
    owning the coin. replenish_coin_reserve (below) keeps
    state.coin_reserve_qty stocked with a standing, unlisted amount of coin
    per ladder precisely for this.

    Two cases, same underlying mechanic:
    - Zero live sells of its own: lists the reserve at its natural price
      (cost_basis * (1 + offset_pct)) so the ladder is never left with
      nothing to offer just because price hasn't dipped into its window yet.
    - Already has live sells: still lists the reserve as soon as there's
      enough of it to clear min_notional, priced just above whatever
      currently sits highest -- extending real sell coverage upward over
      time as the reserve regrows (mirrors extend_ladder_coverage's
      downward growth on the buy side), instead of sitting on it until the
      ladder goes completely dry. Either way, never touches or rebuilds an
      existing sell order.

    Cheap no-op in the common case (empty reserve)."""
    from grid import GridLevel

    listed = 0
    for step_pct, offset_pct, _, name in ladder_specs(cfg):
        qty = state.coin_reserve_qty.get(name, 0.0)
        if qty <= 0:
            continue

        own_sells = [lvl for lvl in state.levels
                     if lvl.state == "sell_open" and lvl.buy_price > 0
                     and abs((lvl.sell_price / lvl.buy_price - 1.0) - offset_pct) < OFFSET_TOLERANCE_PCT]

        cost_basis = state.coin_reserve_cost_basis.get(name, 0.0)
        round_qty = filters.round_qty(qty)
        if cost_basis <= 0 or round_qty <= 0:
            continue

        # Fresh price right before placing, same reasoning apply_order_window
        # uses for buys: the cycle's current_price may already be a few
        # seconds stale by the time we get here.
        fresh_price = trader.get_price()
        natural_price = cost_basis * (1.0 + offset_pct)
        if own_sells:
            highest_sell = max(lvl.sell_price for lvl in own_sells)
            natural_price = max(natural_price, highest_sell * (1.0 + step_pct))
        sell_price = filters.round_price(max(natural_price, fresh_price + cfg.taker_avoidance_buffer))
        if round_qty * sell_price < filters.min_notional:
            log.warning(f"pct_ladder: {name} coin reserve ({round_qty} @ ~${sell_price:.6f}) too thin "
                        f"to clear min_notional -- left untouched")
            continue

        try:
            order_id = trader.place_limit_sell(sell_price, round_qty)
        except Exception as exc:
            log.warning(f"pct_ladder: {name} coin-reserve sell placement failed ({exc}) -- "
                        f"reserve left untouched, will retry next cycle")
            continue

        new_lvl = GridLevel(index=state.next_level_index, buy_price=cost_basis, sell_price=sell_price,
                             base_qty=round_qty, qty=round_qty, state="sell_open",
                             sell_order_id=order_id, from_coin_reserve=True, coin_reserve_ladder=name)
        state.next_level_index += 1
        state.levels.append(new_lvl)
        state.coin_reserve_qty[name] = 0.0
        listed += 1
        if own_sells:
            log.info(f"pct_ladder: {name} extended sell coverage upward with its coin reserve "
                     f"({round_qty} @ ${sell_price:.6f}), above previous highest ${highest_sell:.6f}")
        else:
            log.info(f"pct_ladder: {name} had zero live sells of its own -- listed its coin reserve "
                     f"({round_qty} @ ${sell_price:.6f}, cost basis ${cost_basis:.6f}) as a real sell order")
    return listed


def replenish_coin_reserve(state, name, amount, trader, filters, cfg):
    """Buys more of this ladder's coin_reserve_qty[name] with `amount` USD
    -- same immediate-fill technique DASH's bulk ladder uses (a limit buy
    placed slightly ABOVE current price on purpose, to cross the spread and
    fill now rather than rest and wait).
    Two callers, two purposes:
    - liquidate_coin_reserve_position: `amount` = a just-sold reserve
      position's own principal, closing the loop back to its original
      seeded size (replenishment).
    - _grow_coin_reserve: `amount` = a slice of ordinary profit
      reinvestment (cfg.coin_reserve_ratio), permanently GROWING the
      reserve beyond its original seed size over time, gradually, across
      every ladder-native/legacy/reserve sale -- not just restoring it.
    Either way this never touches the ladder's buying power or its own
    USD reserve pool.

    Returns True if the buy succeeded, False otherwise -- the caller is
    responsible for routing `amount` somewhere safe instead (e.g.
    pending_reinvest_by_band) on a False return. This function never
    discards capital itself, it only reports whether it could put it to
    use. No synchronous fill verification here -- same lighter-weight,
    already-proven pattern DASH's bulk ladder uses for its own
    immediate-fill purchase, since this runs unattended on every
    qualifying sale rather than as a manually-reviewed script."""
    if amount <= 0:
        return False
    fresh_price = trader.get_price()
    buy_price = filters.round_price(fresh_price * 1.002)
    qty = filters.round_qty(amount / buy_price) if buy_price > 0 else 0.0
    if qty <= 0 or qty * buy_price < filters.min_notional:
        log.warning(f"pct_ladder: {name} coin-reserve buy amount (${amount:.4f}) too thin to "
                    f"clear min_notional -- skipped this cycle")
        return False
    try:
        trader.place_limit_buy(buy_price, qty)
    except Exception as exc:
        log.warning(f"pct_ladder: {name} coin-reserve buy failed ({exc}) -- reserve not grown")
        return False
    old_qty = state.coin_reserve_qty.get(name, 0.0)
    old_cost_basis = state.coin_reserve_cost_basis.get(name, 0.0)
    new_qty = old_qty + qty
    # Weighted average, not a plain overwrite -- matters once coin-reserve growth
    # can call this repeatedly while the reserve is ALREADY nonzero (gradual
    # growth from many small profit events), not just the old draw-then-
    # replenish case where it always started at exactly zero.
    state.coin_reserve_cost_basis[name] = (
        (old_qty * old_cost_basis + qty * buy_price) / new_qty if new_qty > 0 else buy_price)
    state.coin_reserve_qty[name] = new_qty
    log.info(f"pct_ladder: {name} coin reserve grew -- bought {qty} @ ${buy_price:.6f} "
             f"(${qty * buy_price:.4f} of ${amount:.4f}), now {new_qty} @ avg "
             f"${state.coin_reserve_cost_basis[name]:.6f}")
    return True


def maybe_protect_profit(state, protect_trader, protect_filters):
    """Converts state.pending_protect (accum_ratio's share of every sale's
    profit -- see _split_profit_four_ways) into cfg.protect_symbol, a
    stablecoin (e.g. USDCUSDT) that none of this project's bots trade.
    This is the only thing that actually removes money from the shared
    exchange balance every bot draws on -- state.reserved_total was always
    just a bookkeeping label, never a real withdrawal from what this bot's
    own order placement (or any other bot sharing the account) could still
    spend. Requested explicitly 2026-10-21 after confirming "protected"
    profit was never actually safe from being redeployed.

    Same immediate-fill technique as replenish_coin_reserve (a limit buy
    placed 0.2% above current price, to cross the spread and fill now
    rather than rest and wait). A single profit-split's accum share is
    almost always far too small to clear the protect symbol's own
    min_notional/lot-size on its own, so this accumulates across many
    cycles (same pattern as pending_coin_growth) until there's enough to
    actually place the buy. Called once per poll cycle, independent of
    any particular sale -- a placement failure just leaves pending_protect
    untouched for a retry next cycle, never worse off than before.

    Returns the USD amount actually converted this cycle (0 if nothing
    happened)."""
    if state.pending_protect <= 0:
        return 0.0
    price = protect_trader.get_price()
    if price <= 0:
        return 0.0
    buy_price = protect_filters.round_price(price * 1.002)
    qty = protect_filters.round_qty(state.pending_protect / buy_price) if buy_price > 0 else 0.0
    if qty <= 0 or qty * buy_price < protect_filters.min_notional:
        return 0.0
    try:
        protect_trader.place_limit_buy(buy_price, qty)
    except Exception as exc:
        log.warning(f"pct_ladder: protect-profit buy of {qty} {protect_trader.symbol} failed "
                     f"({exc}) -- ${state.pending_protect:.4f} left pending, retry next cycle")
        return 0.0
    spent = qty * buy_price
    state.pending_protect = max(0.0, state.pending_protect - spent)
    state.protected_total += spent
    log.info(f"pct_ladder: protected ${spent:.4f} -- bought {qty} {protect_trader.symbol} @ "
              f"${buy_price:.6f} (bot total protected now ${state.protected_total:.4f}, "
              f"${state.pending_protect:.4f} still pending)")
    return spent


def _grow_coin_reserve(state, name, coin_amount, trader, filters, cfg):
    """Buys coin with this ladder's already-sized coin-reserve slice of a
    sale's profit (cfg.coin_reserve_ratio of it -- sizing now happens
    once, in _split_profit_four_ways, not here).

    A single sale's slice is usually far too small to clear min_notional
    on its own (found live 2026-10-13 on XRP, same pct_ladder design: 48
    of 52 attempts since this feature went live had been silently
    skipped). Rather than discarding that opportunity each time, the slice
    ACCUMULATES in state.pending_coin_growth[name] across sales until the
    running total is big enough to actually clear min_notional, then
    resets to zero."""
    if coin_amount <= 0:
        return
    pending = state.pending_coin_growth.get(name, 0.0) + coin_amount
    if replenish_coin_reserve(state, name, pending, trader, filters, cfg):
        state.pending_coin_growth[name] = 0.0
    else:
        state.pending_coin_growth[name] = pending


def _split_profit_four_ways(state, name, profit, trader, filters, cfg):
    """Splits a ladder-attributed profit amount 4 ways (ratios configurable,
    must sum to 1.0 -- see config.py's validate()):
    - cfg.accum_ratio -> state.reserved_total, bot-wide, never auto-drawn
      by anything.
    - cfg.compound_ratio -> this ladder's pending_reinvest_by_band (today's
      existing buy-side compounding, picked up by reshape_ladder).
    - cfg.ladder_reserve_ratio -> this ladder's own state.ladder_reserved --
      still auto-drawable via _draw_from_ladder_reserve, unchanged from
      today's behavior.
    - cfg.coin_reserve_ratio -> this ladder's own coin reserve, via
      _grow_coin_reserve."""
    if profit <= 0:
        return
    accum_amount = profit * cfg.accum_ratio
    compound_amount = profit * cfg.compound_ratio
    reserve_amount = profit * cfg.ladder_reserve_ratio
    coin_amount = profit * cfg.coin_reserve_ratio

    state.reserved_total += accum_amount
    state.pending_protect += accum_amount

    band_key = _band_key(next(o for _, o, _, n in ladder_specs(cfg) if n == name))
    state.pending_reinvest_by_band[band_key] = state.pending_reinvest_by_band.get(band_key, 0.0) + compound_amount

    state.ladder_reserved[name] = state.ladder_reserved.get(name, 0.0) + reserve_amount
    state.ladder_reserved_contributed[name] = state.ladder_reserved_contributed.get(name, 0.0) + reserve_amount

    _grow_coin_reserve(state, name, coin_amount, trader, filters, cfg)

    log.info(f"pct_ladder: {name} profit {profit:.4f} split 4 ways -- "
             f"${accum_amount:.4f} accumulation (never drawn, bot total now ${state.reserved_total:.4f}), "
             f"${compound_amount:.4f} buy-side compounding, "
             f"${reserve_amount:.4f} to its own reserve (now ${state.ladder_reserved[name]:.4f}), "
             f"${coin_amount:.4f} toward coin reserve")


def liquidate_coin_reserve_position(state, lvl, profit, trader, filters, cfg) -> bool:
    """Called from bot.py's sell-fill loop when a from_coin_reserve=True
    level sells. Routes profit directly to lvl.coin_reserve_ladder -- the
    ladder recorded explicitly on the level itself, NOT re-derived from the
    sell/buy gap, since the taker-avoidance floor in
    maybe_fund_sell_from_reserve can make that gap land slightly wider than
    the ladder's exact offset_pct (which would otherwise misroute via
    distribute_profit_ladder_aware's own offset matching). The caller is
    responsible for removing the level from state.levels -- this function
    only handles capital.

    The sale's PRINCIPAL (separate from its profit) replenishes the coin
    reserve back toward its pre-sale size. Its PROFIT goes through the
    same 4-way split as any other sale (_split_profit_four_ways) -- so a
    reserve position's profit can grow the coin reserve FURTHER, on top of
    the principal already restoring it.

    Returns True if the coin reserve was successfully replenished from this
    sale's principal."""
    name = lvl.coin_reserve_ladder
    principal = lvl.buy_price * lvl.qty
    profit = max(0.0, profit)
    state.total_profit_realized += profit

    refilled = replenish_coin_reserve(state, name, principal, trader, filters, cfg)
    if not refilled:
        band_key = _band_key(next(o for _, o, _, n in ladder_specs(cfg) if n == name))
        state.pending_reinvest_by_band[band_key] = state.pending_reinvest_by_band.get(band_key, 0.0) + principal

    _split_profit_four_ways(state, name, profit, trader, filters, cfg)

    log.info(f"pct_ladder: {name} coin-reserve position liquidated -- principal ${principal:.4f} "
             + ("replenished the reserve" if refilled else "queued to pending-reinvest (replenishment failed)")
             + f", profit ${profit:.4f} split 4 ways (see line above)")
    return refilled


def maybe_reshape_ladders(state, current_price, trader, filters, cfg):
    """Runs every poll cycle, same as DASH's maybe_reshape_ladders.
    Deliberately NOT gated on price having moved -- a ladder can get
    stuck while price just sits still (capital eroded under
    min_notional), and reshape_ladder is a cheap no-op when nothing is
    actually wrong."""
    if not cfg.pct_ladder_enabled:
        return 0
    zones = compute_ladder_zones(current_price, cfg)
    # extend_ladder_coverage (2026-10-15) draws real exchange balance every
    # cycle, across all 5 ladders -- on bots sharing a tight free-balance
    # pool with another bot (e.g. XRP+UNI's USD) it can drain faster than
    # the pool refills, leaving unrelated new buy orders rejected for
    # insufficient balance. One balance check per cycle (not per ladder)
    # gates the whole batch; reshape_ladder's own gap-filling is unaffected
    # since it only ever resizes/fills within the existing window.
    quote_asset = "USDT" if cfg.symbol.endswith("USDT") else "USD"
    free_balance = trader.get_free_balance(quote_asset)
    extend_allowed = free_balance >= cfg.extend_min_free_balance_usd
    if not extend_allowed:
        log.info(f"pct_ladder: skipping extend_ladder_coverage this cycle -- "
                 f"free {quote_asset} balance ${free_balance:.2f} below "
                 f"EXTEND_MIN_FREE_BALANCE_USD ${cfg.extend_min_free_balance_usd:.2f}")
    total_reshaped = 0
    for step_pct, offset_pct, window_pct, name in ladder_specs(cfg):
        lo, hi = zones[name]
        if hi <= lo:
            continue
        total_reshaped += reshape_ladder(state, name, lo, hi, step_pct, offset_pct, trader, filters, cfg, current_price)
        if extend_allowed:
            total_reshaped += extend_ladder_coverage(state, name, step_pct, offset_pct, cfg, filters)
    return total_reshaped
