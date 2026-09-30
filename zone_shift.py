import logging
import math

from consolidation_log import log_event

log = logging.getLogger("dash-grid-bot")


def _new_index(state):
    idx = state.next_level_index
    state.next_level_index += 1
    return idx


def _cancel_and_collect(members, trader, id_attr):
    """Try to cancel each member's order. Returns only the members whose
    cancel succeeded — anything that failed (most likely because it
    already filled for real, moments before we got to it) is left
    completely untouched so the normal fill-check picks it up next cycle,
    exactly like the same-shaped fix in reinvest.py."""
    collected = []
    for lvl in members:
        order_id = getattr(lvl, id_attr)
        if order_id is None:
            collected.append(lvl)
            continue
        try:
            trader.cancel_order(order_id)
            collected.append(lvl)
        except Exception as exc:
            log.warning(f"Zone shift: Grid {lvl.index} order {order_id} not cancellable "
                         f"({exc}) — likely already filled, leaving untouched")
    return collected


def _restore_individual_sells(members, trader):
    """Rollback path: we already cancelled these members' sell orders, but
    the consolidated replacement failed to place. Re-place each one
    individually at its OWN original price rather than leave real, owned
    inventory with no protecting order at all."""
    for lvl in members:
        try:
            lvl.sell_order_id = trader.place_limit_sell(lvl.sell_price, lvl.qty)
            lvl.state = "sell_open"
        except Exception as exc:
            log.error(f"Zone shift ROLLBACK FAILURE: Grid {lvl.index} qty={lvl.qty} "
                       f"could not be re-protected after a failed consolidation "
                       f"({exc}) — this position is currently UNPROTECTED, "
                       f"investigate immediately")
            lvl.state = "idle"
            lvl.sell_order_id = None


def _consolidate_holding_bucket(state, members, trader, filters, target_sell_price, event_type="holding_merge"):
    """Merge several already-owned (sell_open) positions into one bigger
    sell order. The consolidated price is always >= every original
    member's own target — never sells any portion for less than it was
    already individually promised. Returns the new GridLevel on success,
    or None if consolidation didn't happen (originals safely restored)."""
    collected = _cancel_and_collect(members, trader, "sell_order_id")
    if len(collected) < 2:
        # Nothing meaningful to merge (0 or 1 survived cancellation) — put
        # back whatever we did cancel, untouched, rather than leave it bare.
        for lvl in collected:
            if lvl.sell_order_id is None:
                try:
                    lvl.sell_order_id = trader.place_limit_sell(lvl.sell_price, lvl.qty)
                except Exception as exc:
                    log.error(f"Zone shift: Grid {lvl.index} left unprotected after "
                               f"aborted single-member consolidation: {exc}")
        return None

    total_qty = filters.round_qty(sum(l.qty for l in collected))
    weighted_cost = sum(l.qty * l.buy_price for l in collected)
    avg_buy_price = weighted_cost / sum(l.qty for l in collected)
    sell_price = filters.round_price(target_sell_price)

    if total_qty <= 0 or total_qty * sell_price < filters.min_notional:
        log.warning(f"Zone shift: consolidated qty {total_qty} at ${sell_price} too small, "
                     f"restoring {len(collected)} original sell order(s) instead")
        _restore_individual_sells(collected, trader)
        return None

    try:
        order_id = trader.place_limit_sell(sell_price, total_qty)
    except Exception as exc:
        log.error(f"Zone shift: consolidated sell placement failed ({exc}) — "
                   f"restoring {len(collected)} original sell order(s) instead")
        _restore_individual_sells(collected, trader)
        return None

    new_level_kwargs = dict(
        index=_new_index(state), buy_price=avg_buy_price, sell_price=sell_price,
        base_qty=total_qty, state="sell_open", sell_order_id=order_id, qty=total_qty,
        consolidated=True, one_shot=collected[0].one_shot,
    )
    for lvl in collected:
        state.levels.remove(lvl)
    from grid import GridLevel
    new_level = GridLevel(**new_level_kwargs)
    state.levels.append(new_level)
    log.info(f"Zone shift: merged {len(collected)} sell order(s) (qty {total_qty}) "
              f"into one at ${sell_price} (avg cost basis ${avg_buy_price:.4f})")
    log_event(event_type, "sell",
              original_prices=[l.sell_price for l in collected],
              original_qtys=[l.qty for l in collected],
              result_prices=[sell_price], result_qtys=[total_qty])
    return new_level


def _consolidate_pending_bucket(state, members, trader, bucket_floor, sell_offset, filters=None):
    """Merge several not-yet-bought (idle/buy_open) levels into one coarser
    future buy target. No real position exists yet, so this is zero-risk —
    just cancels any resting (unfilled) buy orders and regroups the
    intent into a single bigger level. `filters`, when given, rounds to the
    exchange's own tick/step size (Decimal-based, clean) instead of plain
    Python round() — bucket_floor arithmetic upstream (math.floor divide-
    multiply) can otherwise leave float noise like 54.99999999999999
    baked into the stored price forever."""
    collected = _cancel_and_collect(members, trader, "buy_order_id")
    if len(collected) < 2:
        return None

    total_qty = sum(l.target_qty() for l in collected)
    for lvl in collected:
        state.levels.remove(lvl)

    if filters is not None:
        clean_buy_price = filters.round_price(bucket_floor)
        clean_sell_price = filters.round_price(bucket_floor + sell_offset)
        total_qty = filters.round_qty(total_qty)
    else:
        clean_buy_price = round(bucket_floor, 10)
        clean_sell_price = round(bucket_floor + sell_offset, 10)

    from grid import GridLevel
    new_level = GridLevel(
        index=_new_index(state), buy_price=clean_buy_price,
        sell_price=clean_sell_price,
        base_qty=total_qty, consolidated=True,
    )
    state.levels.append(new_level)
    log.info(f"Zone shift: merged {len(collected)} pending level(s) into one future "
              f"buy target at ${bucket_floor:.2f} (qty {total_qty:.4f})")
    log_event("pending_merge", "buy",
              original_prices=[l.buy_price for l in collected],
              original_qtys=[l.target_qty() for l in collected],
              result_prices=[clean_buy_price], result_qtys=[total_qty])
    return new_level


def _bucket_and_merge(state, levels, trader, filters, cfg, step):
    """Shared consolidation pass for a set of dense-zone levels outside the
    near-market band, regardless of which side of the band they're on.
    Buckets by a $step-wide bin (on buy_price for not-yet-bought levels,
    sell_price for already-bought ones) and merges each bucket exactly the
    same way whether price dropped below it or rose above it."""
    buckets = {}
    for lvl in levels:
        key_price = lvl.buy_price if lvl.state in ("idle", "buy_open") else lvl.sell_price
        bucket = math.floor(key_price / step) * step
        buckets.setdefault(bucket, []).append(lvl)

    merged_pending = 0
    merged_holding = 0
    for bucket, members in buckets.items():
        idle_open = [l for l in members if l.state in ("idle", "buy_open")]
        holding = [l for l in members if l.state == "sell_open"]
        if len(idle_open) >= 2:
            if _consolidate_pending_bucket(state, idle_open, trader, bucket, cfg.dense_sell_offset, filters=filters):
                merged_pending += len(idle_open)
        if len(holding) >= 2:
            # Ceiling to the next whole $step, not just the max of whatever
            # landed in this bucket — e.g. 61.1/61.2/61.3 -> $62, not $61.3.
            # Strictly safer than before (still >= every original promise)
            # and gives a clean round target instead of an arbitrary cent value.
            raw_max = max(l.sell_price for l in holding)
            target = math.ceil(raw_max / step - 1e-9) * step
            if _consolidate_holding_bucket(state, holding, trader, filters, target):
                merged_holding += len(holding)
    return merged_pending, merged_holding


def maybe_shift_zones(state, current_price, trader, filters, cfg):
    """Runs at most once every ZONE_SHIFT_INTERVAL_HOURS. The dense zone is
    meant to be the actively-traded, price-following ("floating") core —
    anything that drifts more than NEAR_MARKET_BAND_PCT away from current
    price, on EITHER side, gets coarsened into CONSOLIDATION_STEP-spaced
    levels, freeing order-count budget for levels actually near where price
    is trading. This applies symmetrically below and above the band. The
    anchor-low zone (identified by its wider anchor-step gap, distinct from
    the dense zone's dense-offset gap) is never touched, and the bulk ladder
    (one_shot=True) is still handled separately since it never has a
    pending/not-yet-bought case. Once merged, a level stays merged — this
    never un-consolidates, avoiding constant churn as price wobbles near a
    boundary.
    """
    import time
    now = time.time()
    if now - state.last_zone_shift_ts < cfg.zone_shift_interval_hours * 3600:
        return
    state.last_zone_shift_ts = now

    band_low = current_price * (1 - cfg.near_market_band_pct)
    band_high = current_price * (1 + cfg.near_market_band_pct)
    step = cfg.consolidation_step

    # Dense-zone levels, identified by their gap matching dense_sell_offset
    # exactly — the anchor-low zone's wider gap and the bulk ladder's
    # one_shot flag both naturally exclude those.
    dense = [lvl for lvl in state.levels
             if not lvl.one_shot and not lvl.consolidated and lvl.state != "closed"
             and abs((lvl.sell_price - lvl.buy_price) - cfg.dense_sell_offset) < 1e-6]

    below = [lvl for lvl in dense if lvl.buy_price < band_low]
    above_dense = [lvl for lvl in dense if lvl.sell_price > band_high]

    merged_pending = 0
    merged_holding = 0
    mp, mh = _bucket_and_merge(state, below, trader, filters, cfg, step)
    merged_pending += mp
    merged_holding += mh
    mp, mh = _bucket_and_merge(state, above_dense, trader, filters, cfg, step)
    merged_pending += mp
    merged_holding += mh

    # Bulk-ladder sell slices (one_shot=True), above market only. All
    # already bought at go-live, so only ever sell_open or closed by now —
    # no "pending" case to handle on this side.
    above_bulk = [lvl for lvl in state.levels
                  if lvl.one_shot and not lvl.consolidated and lvl.state == "sell_open"
                  and lvl.sell_price > band_high]

    above_buckets = {}
    for lvl in above_bulk:
        bucket = math.floor(lvl.sell_price / step) * step
        above_buckets.setdefault(bucket, []).append(lvl)

    for bucket, members in above_buckets.items():
        if len(members) >= 2:
            raw_max = max(l.sell_price for l in members)
            target = math.ceil(raw_max / step - 1e-9) * step
            if _consolidate_holding_bucket(state, members, trader, filters, target):
                merged_holding += len(members)

    if merged_pending or merged_holding:
        log.info(f"Zone shift complete: band=[${band_low:.2f}, ${band_high:.2f}], "
                  f"{merged_pending} pending level(s) and {merged_holding} holding "
                  f"level(s) consolidated")
