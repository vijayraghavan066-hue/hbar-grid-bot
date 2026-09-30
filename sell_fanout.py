import logging
from collections import defaultdict

from consolidation_log import log_event

log = logging.getLogger("dash-grid-bot")

FAN_OUT_CAP = 6
FAN_OUT_STEP = 0.10


def _split_qty(total_qty, n, filters):
    """Split total_qty into n pieces respecting the exchange's qty step,
    with the last piece absorbing rounding remainder so the pieces sum
    back to (approximately) the original total. Returns [] if any piece
    would round to zero or below."""
    per = filters.round_qty(total_qty / n)
    if per <= 0:
        return []
    pieces = [per] * (n - 1)
    remainder = filters.round_qty(total_qty - per * (n - 1))
    if remainder <= 0:
        return []
    pieces.append(remainder)
    return pieces


def plan_fanout(total_qty, base_price, filters, cap=FAN_OUT_CAP, step=FAN_OUT_STEP):
    """Returns a list of (price, qty) rungs starting at base_price and
    stepping up by `step` — never below base_price, so nothing sells for
    less than already promised. Shrinks the rung count (down from `cap`)
    until every rung clears the exchange's min notional; falls back to a
    single rung (the original lump) if fan-out isn't viable at any size."""
    for n in range(cap, 0, -1):
        pieces = _split_qty(total_qty, n, filters)
        if len(pieces) != n:
            continue
        prices = [filters.round_price(base_price + i * step) for i in range(n)]
        if all(q * p >= filters.min_notional for q, p in zip(pieces, prices)):
            return list(zip(prices, pieces))
    return [(filters.round_price(base_price), filters.round_qty(total_qty))]


def place_fanout(trader, filters, total_qty, base_sell_price, cap=FAN_OUT_CAP, step=FAN_OUT_STEP):
    """Attempts to place `total_qty` as several smaller sells starting at
    base_sell_price and stepping up by `step`, instead of one lump — never
    below base_sell_price. Returns a list of (order_id, price, qty) on full
    success. On ANY placement failure partway through, cancels whatever
    succeeded so far and returns None so the caller can fall back to the
    original, proven-safe single lump sell instead."""
    plan = plan_fanout(total_qty, base_sell_price, filters, cap, step)
    placed = []
    for price, qty in plan:
        try:
            order_id = trader.place_limit_sell(price, qty)
            placed.append((order_id, price, qty))
        except Exception as exc:
            log.warning(f"Fan-out: rung ${price} qty={qty} rejected ({exc}) — "
                         f"rolling back {len(placed)} already-placed rung(s), "
                         f"falling back to a single lump sell")
            for order_id, _, _ in placed:
                try:
                    trader.cancel_order(order_id)
                except Exception as cancel_exc:
                    log.error(f"Fan-out ROLLBACK FAILURE: could not cancel rung "
                               f"order {order_id} ({cancel_exc}) — investigate, there "
                               f"may be an extra unintended sell order live")
            return None
    log_event("fanout", "sell",
              original_prices=[base_sell_price], original_qtys=[total_qty],
              result_prices=[p for _, p, _ in placed], result_qtys=[q for _, _, q in placed])
    return placed


def merge_same_price_sells(state, trader, filters):
    """Housekeeping: if two or more sell_open levels ever end up resting at
    the same real exchange price — e.g. two independently-fanned buckets
    landing on the same rung, or a fan-out colliding with a pre-existing
    fine-resolution level — merge them into one order for the summed qty.
    Trivially safe: the merge target IS the price every member already
    shares, so nothing ever sells for less than any member's own promise.
    Groups by price rounded to the exchange's own tick size (not exact
    float equality) — a level's stored `sell_price` can carry float noise
    (e.g. 54.99999999999999) from upstream bucket-floor arithmetic even
    though the REAL order Binance holds is at a clean price, so exact
    matching would miss real duplicates. Run every cycle so duplicates can
    never accumulate, regardless of source. Returns the number of groups
    merged."""
    from zone_shift import _consolidate_holding_bucket  # local import avoids a circular import at module load

    by_price = defaultdict(list)
    for lvl in state.levels:
        if lvl.state == "sell_open":
            by_price[filters.round_price(lvl.sell_price)].append(lvl)

    merged = 0
    for price, group in by_price.items():
        if len(group) >= 2:
            if _consolidate_holding_bucket(state, group, trader, filters, price, event_type="dup_merge"):
                merged += 1
                log.info(f"Housekeeping: merged {len(group)} duplicate sell order(s) "
                          f"at ${price} into one")
    return merged


def merge_same_price_buys(state, trader, filters):
    """Buy-side mirror of merge_same_price_sells. These are pending (not
    yet bought) resting buy orders, so merging is zero-risk — reuses
    zone_shift._consolidate_pending_bucket, the exact same merge zone-shift
    itself already uses for consolidation. Root cause this cleans up:
    zone-shift's bucket-and-merge pass runs independently every cycle, and
    can create a NEW consolidated bucket that lands on the same price as
    one from an earlier pass (or, with the float-noise issue above, a
    price that's really the same but stored as a slightly different
    float) — nothing previously checked for that collision. Groups by
    price rounded to the exchange tick size for the same reason as the
    sell-side version. Returns the number of groups merged."""
    from zone_shift import _consolidate_pending_bucket  # local import avoids a circular import at module load

    by_price = defaultdict(list)
    for lvl in state.levels:
        if lvl.state == "buy_open":
            by_price[filters.round_price(lvl.buy_price)].append(lvl)

    merged = 0
    for price, group in by_price.items():
        if len(group) >= 2:
            sell_offset = group[0].sell_price - group[0].buy_price
            if _consolidate_pending_bucket(state, group, trader, price, sell_offset):
                merged += 1
                log.info(f"Housekeeping: merged {len(group)} duplicate buy order(s) "
                          f"at ${price} into one")
    return merged
