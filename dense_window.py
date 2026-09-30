"""Dynamic, price-following dense zone for UNI: instead of (or rather,
alongside) the fixed DENSE_LOW/DENSE_HIGH range, this maintains a +-
DENSE_WINDOW_HALF_WIDTH window around wherever price currently is,
recentered the moment price crosses into a new DENSE_WINDOW_SNAP-sized
checkpoint. Runs every poll cycle, alongside (not instead of) the existing
timer-based zone_shift.py consolidation, which stays as a backup/catch-all.

Ported from dash-grid-bot's dense_window.py (same promote/demote mechanics,
same safety rules) with one real change: DASH's version hard-codes "round to
the nearest whole dollar" as its snap granularity, which only makes sense at
DASH's ~$60 price scale. At UNI's ~$9 scale that would be far too coarse (a
single $1 step is over 10% of price, and DASH's own $5 half-width would
swallow nearly UNI's entire configured range in one window). Both values are
scaled down here using the same ratio DASH itself uses: half-width equals
one ANCHOR_STEP, and snap is roughly a fifth of that (DASH: half-width $5 =
1x its $5 anchor_step, snap $1 = 1/5th of that). For UNI's $0.35 anchor
step, that's half-width $0.35 and snap $0.05 -- see config.py for the actual
configured values.

Two operations, one per direction the window can move:
  - DEMOTE (window slides away from a price): identical in spirit to
    zone_shift's existing far-from-market consolidation - fine dense
    levels now outside the window get merged into a coarser bucket, via
    the exact same zone_shift._bucket_and_merge helper. One-directional,
    same as always: a demoted level doesn't automatically come back.
  - PROMOTE (window slides onto a price): an anchor-zone segment (a
    chained pair of anchor points, e.g. buy $9.35 -> sell $9.70) whose own
    buy_price now falls inside the window gets split into fine dense
    sub-levels ($0.04 apart, one DENSE_SELL_OFFSET target each), using
    the segment's OWN capital reshaped - no new capital pulled in. Only
    applies to not-yet-bought (idle/buy_open) anchor segments; a held
    (sell_open) anchor position is left completely alone, same as every
    other rule in this project - promotion never touches owned inventory.
"""
import logging

from consolidation_log import log_event
from grid import GridLevel
from zone_shift import _bucket_and_merge

log = logging.getLogger("dash-grid-bot")


def window_for_price(price, half_width, snap):
    """(center, lo, hi) - center is price rounded to the nearest `snap`
    checkpoint, window is +-half_width around it. Bounds are cleaned with
    round() since center-half_width/center+half_width can otherwise land on
    e.g. 9.799999999999999 instead of 9.8 (binary float noise)."""
    center = round(round(price / snap) * snap, 10)
    return center, round(center - half_width, 10), round(center + half_width, 10)


def _build_fine_sublevels(buy_lo, sell_hi, dense_step, dense_sell_offset, capital,
                           reference_price, dca_exponent, filters, start_index):
    """Same DCA-weighted allocation formula build_hybrid_zoned_levels uses
    for the dense zone, scoped to just [buy_lo, sell_hi] and funded by
    `capital` instead of the whole bot's total capital."""
    raw = []
    p = buy_lo
    while p <= sell_hi + 1e-9:
        bp = round(p, 10)
        raw.append((bp, round(bp + dense_sell_offset, 10)))
        p += dense_step
    if not raw:
        return []

    weights = [(reference_price / bp) ** dca_exponent for bp, _ in raw]
    total_weight = sum(weights)

    levels = []
    for i, (bp, sp) in enumerate(raw):
        usdt_alloc = capital * weights[i] / total_weight
        clean_bp = filters.round_price(bp)
        clean_sp = filters.round_price(sp)
        qty = filters.round_qty(usdt_alloc / bp) if bp > 0 else 0.0
        if qty <= 0:
            continue
        levels.append(GridLevel(
            index=start_index + len(levels), buy_price=clean_bp, sell_price=clean_sp,
            base_qty=qty,
        ))
    return levels


def _promote_anchor_segment(state, level, trader, filters, cfg):
    """Cancel a not-yet-bought anchor segment's resting buy order (if any)
    and reshape its capital into fine dense sub-levels spanning its own
    [buy_price, sell_price] range. Returns the new sub-levels on success,
    or None if this segment couldn't be promoted (already filled for
    real - left completely untouched, same cancel-failure safety pattern
    as everywhere else in this project)."""
    if level.buy_order_id is not None:
        try:
            trader.cancel_order(level.buy_order_id)
        except Exception as exc:
            log.warning(f"Dense window: Grid {level.index} order {level.buy_order_id} "
                         f"not cancellable ({exc}) — likely already filled, "
                         f"leaving this anchor position untouched, not promoting")
            return None

    capital = level.target_qty() * level.buy_price
    sub_levels = _build_fine_sublevels(
        level.buy_price, level.sell_price, cfg.dense_step, cfg.dense_sell_offset,
        capital, cfg.reference_price, cfg.dca_exponent, filters, state.next_level_index,
    )
    if not sub_levels:
        return None

    state.next_level_index += len(sub_levels)
    state.levels.remove(level)
    state.levels.extend(sub_levels)
    log.info(f"Dense window: promoted anchor segment ${level.buy_price}-${level.sell_price} "
              f"into {len(sub_levels)} fine dense level(s) (${capital:.2f} reshaped)")
    log_event("promote", "buy",
              original_prices=[level.buy_price], original_qtys=[level.target_qty()],
              result_prices=[l.buy_price for l in sub_levels],
              result_qtys=[l.base_qty for l in sub_levels])
    return sub_levels


def _demote_out_of_window(state, trader, filters, cfg, lo, hi):
    """Fine (not already consolidated) dense-zone levels whose own price
    has fallen outside [lo, hi] get merged via the same bucket-and-merge
    helper zone_shift.py's timer-based pass already uses."""
    dense = [lvl for lvl in state.levels
             if not lvl.one_shot and not lvl.consolidated and lvl.state != "closed"
             and abs((lvl.sell_price - lvl.buy_price) - cfg.dense_sell_offset) < 1e-6]

    def key_price(lvl):
        return lvl.buy_price if lvl.state in ("idle", "buy_open") else lvl.sell_price

    outside = [lvl for lvl in dense if key_price(lvl) < lo or key_price(lvl) > hi]
    if not outside:
        return 0, 0
    return _bucket_and_merge(state, outside, trader, filters, cfg, cfg.consolidation_step)


def maybe_shift_dense_window(state, current_price, trader, filters, cfg):
    """Runs every poll cycle (not timer-gated) - cheap no-op unless price
    has actually crossed into a new DENSE_WINDOW_SNAP-sized checkpoint since
    the last check. First-ever call (state.dense_window_center is None, e.g.
    right after this feature is deployed) always shifts once, to bring
    whatever the bot already has into line with the window."""
    if not cfg.dense_window_enabled:
        return

    center, lo, hi = window_for_price(current_price, cfg.dense_window_half_width, cfg.dense_window_snap)
    if state.dense_window_center is not None and state.dense_window_center == center:
        return

    prev_center = state.dense_window_center
    state.dense_window_center = center

    demoted_pending, demoted_holding = _demote_out_of_window(state, trader, filters, cfg, lo, hi)

    candidates = [lvl for lvl in state.levels
                  if lvl.state in ("idle", "buy_open") and not lvl.one_shot
                  and abs((lvl.sell_price - lvl.buy_price) - cfg.anchor_step) < 1e-6
                  and lo <= lvl.buy_price <= hi]
    promoted = 0
    for lvl in candidates:
        if _promote_anchor_segment(state, lvl, trader, filters, cfg):
            promoted += 1

    if demoted_pending or demoted_holding or promoted:
        log.info(f"Dense window shift: center ${prev_center} -> ${center} (window ${lo}-${hi}) — "
                  f"demoted {demoted_pending} pending + {demoted_holding} holding dense level(s), "
                  f"promoted {promoted} anchor segment(s)")
