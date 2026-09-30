import logging
import time

from state import BotState

log = logging.getLogger("dash-grid-bot")


def _eligible_dense_buy_levels(state: BotState, current_price: float, band_pct: float,
                                dense_sell_offset: float):
    """Dense-zone levels only, identified by their gap matching
    dense_sell_offset exactly (same technique zone_shift.py uses) — this
    naturally excludes the low-anchor zone's wider gap and the bulk
    ladder's one_shot flag. Only "buy orders" in the plain sense (not yet
    filled): idle or resting buy_open, within band_pct below current price.
    """
    if dense_sell_offset is None:
        return []
    band_low = current_price * (1 - band_pct)
    return [lvl for lvl in state.levels
            if lvl.state in ("idle", "buy_open")
            and not lvl.one_shot
            and abs((lvl.sell_price - lvl.buy_price) - dense_sell_offset) < 1e-6
            and band_low <= lvl.buy_price < current_price]


def _apply_reinvestment(amount: float, lower_levels: list, trader, filters,
                         taker_avoidance_buffer: float) -> int:
    """Split `amount` evenly across `lower_levels`, compounding each level's
    extra_qty. If a level receiving a share has an open buy order, it's
    cancelled and replaced with one sized for the new, larger quantity.
    Returns the number of resting orders actually resized."""
    share = amount / len(lower_levels)
    updated_orders = 0

    # One fresh snapshot for this whole batch of resizes — enough to flag
    # when a resized order is about to cross the market and fill as a
    # taker instead of resting as a maker order. Visibility only, doesn't
    # change what gets placed.
    fresh_price = trader.get_price()

    for lvl in lower_levels:
        lvl.extra_qty += share / lvl.buy_price

        if lvl.state == "buy_open" and lvl.buy_order_id is not None:
            try:
                trader.cancel_order(lvl.buy_order_id)
            except Exception as exc:
                # The order is gone from the exchange's side already — most
                # likely it just filled for real, and our own fill-check
                # loop hasn't reached this level yet this cycle. DO NOT
                # proceed to place a replacement buy: that would risk a
                # second, duplicate purchase on top of one we don't know
                # about yet. Leave the level completely untouched — the
                # normal fill-check will call is_filled() on this same
                # order_id next pass and correctly detect and process the
                # real fill. The extra_qty growth above is preserved either
                # way and will be used whenever this level next places an
                # order for real.
                log.warning(f"Grid {lvl.index}: order {lvl.buy_order_id} not cancellable "
                             f"({exc}) — likely already filled, leaving untouched "
                             f"for normal fill detection")
                continue

            new_qty = filters.round_qty(lvl.target_qty())
            lvl.state = "idle"
            lvl.buy_order_id = None
            if new_qty <= 0:
                continue

            place_price = lvl.buy_price
            if place_price >= fresh_price:
                adjusted = filters.round_price(fresh_price - taker_avoidance_buffer)
                if adjusted > 0 and new_qty * adjusted >= filters.min_notional:
                    log.info(f"Grid {lvl.index}: resized buy price ${lvl.buy_price} already "
                              f"at/above fresh market ${fresh_price} — placing ${adjusted} "
                              f"instead to stay a maker order")
                    place_price = adjusted

            try:
                lvl.buy_order_id = trader.place_limit_buy(place_price, new_qty)
                lvl.buy_price = place_price
                lvl.state = "buy_open"
                lvl.qty = new_qty
                updated_orders += 1
            except Exception as exc:
                # Left as idle (cancelled above) — normal order-window logic
                # will retry placing it fresh next cycle.
                log.warning(f"Grid {lvl.index} (${lvl.buy_price}): resized buy order "
                             f"rejected: {exc}")

    return updated_orders


def distribute_profit(state: BotState, profit: float, current_price: float,
                       reserve_ratio: float, trader, filters, taker_avoidance_buffer: float = 0.0,
                       dense_sell_offset: float = None, reinvest_band_pct: float = 0.04,
                       near_market_band_pct: float = 0.10):
    """Called immediately when a sell fills. `reserve_ratio` of the profit is
    held aside untouched (state.reserved_total). The remainder is split
    evenly across dense-zone buy orders (idle or resting) priced within
    `reinvest_band_pct` below the current market price, compounding their
    `extra_qty`. Restricted to the dense zone (not the sparse low-anchor
    zone, not the one-shot bulk ladder) and to a tight band close to market
    on purpose: the dense zone is the actively-traded, price-following
    core, so reinvested capital should concentrate on the levels most
    likely to fill soon and compound quickly, not get diluted into levels
    far from where price actually is.

    Tiered fallback: if nothing qualifies at the tight `reinvest_band_pct`
    band, the same eligibility search is retried widened to
    `near_market_band_pct` (the existing, already-used-by-zone_shift.py
    10% band) before giving up — a fast move can temporarily empty the
    tight band even though there's still a reasonable nearby level. Only if
    even the widened search finds nothing does the share get queued in
    `state.pending_reinvest` instead of being dumped into permanent
    reserve — see retry_pending_reinvest(), called every poll cycle, which
    keeps trying to place it at the original tight band as new eligible
    levels arm.
    """
    if profit <= 0:
        return

    reserve_amount = profit * reserve_ratio
    distribute_amount = profit - reserve_amount
    state.reserved_total += reserve_amount
    state.total_profit_realized += profit

    lower_levels = _eligible_dense_buy_levels(state, current_price, reinvest_band_pct, dense_sell_offset)
    band_used = f"{reinvest_band_pct * 100:.1f}%"

    if not lower_levels and near_market_band_pct > reinvest_band_pct:
        lower_levels = _eligible_dense_buy_levels(state, current_price, near_market_band_pct, dense_sell_offset)
        band_used = f"{near_market_band_pct * 100:.1f}% (widened)"

    if not lower_levels:
        state.pending_reinvest += distribute_amount
        if state.pending_reinvest_since == 0.0:
            state.pending_reinvest_since = time.time()
        log.info(f"Profit {profit:.4f} USDT: no dense-zone buy orders within "
                  f"{reinvest_band_pct * 100:.1f}% or {near_market_band_pct * 100:.1f}% below "
                  f"market price ({current_price}) — {distribute_amount:.4f} queued as "
                  f"pending_reinvest (total pending {state.pending_reinvest:.4f})")
        return

    updated_orders = _apply_reinvestment(distribute_amount, lower_levels, trader, filters,
                                          taker_avoidance_buffer)

    log.info(
        f"Profit {profit:.4f} USDT -> reserved {reserve_amount:.4f}, "
        f"distributed {distribute_amount:.4f} across {len(lower_levels)} lower level(s) "
        f"within {band_used} ({distribute_amount / len(lower_levels):.6f} USDT each), "
        f"{updated_orders} open buy order(s) resized"
    )


def retry_pending_reinvest(state: BotState, current_price: float, trader, filters,
                            taker_avoidance_buffer: float = 0.0, dense_sell_offset: float = None,
                            reinvest_band_pct: float = 0.04, max_pending_hours: float = 24.0):
    """Called every poll cycle. Retries any amount queued by
    distribute_profit()'s fallback at the original tight `reinvest_band_pct`
    band as new eligible levels arm — so normal-market behavior stays
    exactly as tight/targeted as it was before pending_reinvest existed.
    Only falls back to a true permanent state.reserved_total if the amount
    stays unclaimed past `max_pending_hours`.
    """
    if state.pending_reinvest <= 0:
        return

    age_hours = (time.time() - state.pending_reinvest_since) / 3600.0 if state.pending_reinvest_since else 0.0
    if age_hours >= max_pending_hours:
        log.warning(f"pending_reinvest {state.pending_reinvest:.4f} USDT unclaimed for "
                     f"{age_hours:.1f}h (limit {max_pending_hours:.1f}h) — reserving permanently")
        state.reserved_total += state.pending_reinvest
        state.pending_reinvest = 0.0
        state.pending_reinvest_since = 0.0
        return

    lower_levels = _eligible_dense_buy_levels(state, current_price, reinvest_band_pct, dense_sell_offset)
    if not lower_levels:
        return

    amount = state.pending_reinvest
    updated_orders = _apply_reinvestment(amount, lower_levels, trader, filters, taker_avoidance_buffer)
    state.pending_reinvest = 0.0
    state.pending_reinvest_since = 0.0

    log.info(f"pending_reinvest {amount:.4f} USDT claimed: distributed across "
              f"{len(lower_levels)} level(s) within {reinvest_band_pct * 100:.1f}% "
              f"({updated_orders} resized)")
