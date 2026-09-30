import logging
import logging.handlers
import os
import time
import traceback

from binance_client import BinanceUSTrader
from circuit_breaker import check_circuit_breaker
from config import load_config
from dense_window import maybe_shift_dense_window
from grid import GridLevel, build_bulk_ladder_levels, build_hybrid_zoned_levels, build_zoned_levels
from health import write_heartbeat
from ledger import log_buy_fill, log_sell_fill
from reinvest import distribute_profit, retry_pending_reinvest
from sell_fanout import merge_same_price_buys, merge_same_price_sells, place_fanout
from state import BotState, load_state, save_state
from zone_shift import maybe_shift_zones

log = logging.getLogger("dash-grid-bot")


def setup_logging(log_file: str, log_level: str):
    os.makedirs(os.path.dirname(log_file) or ".", exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[file_handler, logging.StreamHandler()],
    )


def apply_order_window(state, current_price, trader, filters, max_open_orders,
                        taker_avoidance_buffer, halted=False):
    """Binance.US caps open orders per symbol at 200. We never touch
    sell_open levels here (we already hold that DASH — its sell order must
    stay live no matter what). The remaining order-count budget goes to
    whichever idle/buy_open levels are closest to the current price;
    anything that drifts outside that window gets its buy order cancelled
    (no loss — it's an unfilled resting order) so the slot can go to a level
    nearer the action. As price moves, the window slides with it.

    `halted` comes from the circuit breaker: while True, no new buy orders
    are armed (existing out-of-window buy_open orders still get dropped
    normally, though there typically aren't any left — the circuit breaker
    already pulled every resting buy the moment it tripped)."""
    sell_open_count = sum(1 for lvl in state.levels if lvl.state == "sell_open")
    budget = max(0, max_open_orders - sell_open_count)

    eligible = [lvl for lvl in state.levels
                if lvl.buy_price < current_price and lvl.state in ("idle", "buy_open")]
    eligible.sort(key=lambda lvl: current_price - lvl.buy_price)
    desired_indices = {lvl.index for lvl in eligible[:budget]}

    # A fresh price snapshot right before placing — the `current_price` this
    # function was called with may already be a few seconds old by the time
    # we get here (a full cycle of fill-checks runs first). If a level's own
    # buy_price has already been reached/crossed by the time we'd place it,
    # placing there would fill immediately as a taker (0.02% fee vs 0%
    # maker). At scale that adds up, so instead we place a bit below the
    # fresh price — still a real resting maker order, capturing effectively
    # the same trade a moment later instead of paying to jump the queue.
    to_place = [lvl for lvl in eligible
                if lvl.index in desired_indices and lvl.state == "idle"] if not halted else []
    fresh_price = trader.get_price() if to_place else current_price

    armed = 0
    dropped = 0
    rejected = 0
    for lvl in eligible:
        in_window = lvl.index in desired_indices
        if in_window and lvl.state == "idle" and not halted:
            qty = filters.round_qty(lvl.target_qty())
            if qty <= 0 or qty * lvl.buy_price < filters.min_notional:
                continue

            place_price = lvl.buy_price
            if place_price >= fresh_price:
                adjusted = filters.round_price(fresh_price - taker_avoidance_buffer)
                if adjusted > 0 and qty * adjusted >= filters.min_notional:
                    log.info(f"Grid {lvl.index}: buy price ${lvl.buy_price} already at/above "
                              f"fresh market ${fresh_price} — placing ${adjusted} instead to "
                              f"stay a maker order (sell target ${lvl.sell_price} unchanged, "
                              f"so this level now captures a wider margin)")
                    place_price = adjusted
                # else: adjusted price would be unusable (too small / below
                # min notional) — fall through and place at the original
                # price; worst case it's a taker fill this one time.

            try:
                lvl.buy_order_id = trader.place_limit_buy(place_price, qty)
                lvl.buy_price = place_price
                lvl.qty = qty
                lvl.state = "buy_open"
                armed += 1
            except Exception as exc:
                # e.g. Binance's PERCENT_PRICE filter rejecting a price too
                # far from the current market — leave it idle, it'll be
                # retried automatically next cycle if conditions change,
                # without blocking every other level's processing.
                rejected += 1
                log.warning(f"Grid {lvl.index} (${lvl.buy_price}): buy order rejected: {exc}")
        elif not in_window and lvl.state == "buy_open":
            try:
                trader.cancel_order(lvl.buy_order_id)
            except Exception as exc:
                log.warning(f"Grid {lvl.index}: could not cancel order {lvl.buy_order_id}: {exc}")
            lvl.buy_order_id = None
            lvl.qty = 0.0
            lvl.state = "idle"
            dropped += 1

    if armed or dropped or rejected:
        log.info(f"Order window: {len(desired_indices)} level(s) targeted "
                  f"(budget={budget}, sell_open={sell_open_count}) — "
                  f"armed {armed} new, dropped {dropped} out-of-window, {rejected} rejected")


def cancel_all_open_orders(trader, levels):
    """Cancel any orders left open from a previous run before rebuilding the
    grid fresh — avoids stale/duplicate orders sitting on the book when the
    config or code changes between restarts."""
    cancelled = 0
    for lvl in levels:
        for order_id in (lvl.buy_order_id, lvl.sell_order_id):
            if order_id is not None:
                try:
                    trader.cancel_order(order_id)
                    cancelled += 1
                except Exception as exc:
                    log.warning(f"Could not cancel stale order {order_id}: {exc}")
    return cancelled


def run():
    cfg = load_config()
    setup_logging(cfg.log_file, cfg.log_level)

    log.info(f"Starting dash-grid-bot | symbol={cfg.symbol} dry_run={cfg.dry_run}")
    if not cfg.dry_run:
        log.warning("LIVE TRADING ENABLED — real orders will be placed on Binance.US")

    trader = BinanceUSTrader(cfg.api_key, cfg.api_secret, cfg.symbol, cfg.dry_run)
    filters = trader.get_symbol_filters()

    current_signature = {
        "symbol": cfg.symbol,
        "dry_run": cfg.dry_run,  # switching sim<->live must never resume fake order IDs as real
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

    prior_state = load_state(cfg.state_file)

    if prior_state is not None and prior_state.config_signature == current_signature:
        # Same grid config as last run — this is a crash/reboot restart, not
        # a deliberate reconfiguration. Resume exactly where we left off:
        # open positions, compounded quantities, reserved/profit totals.
        state = prior_state
        trader.rehydrate_from_levels(state.levels)
        open_buys = sum(1 for l in state.levels if l.state == "buy_open")
        open_sells = sum(1 for l in state.levels if l.state == "sell_open")
        log.info(f"Resumed prior state (config unchanged): {len(state.levels)} levels, "
                  f"{open_buys} open buy(s), {open_sells} open sell(s), "
                  f"reserved_total={state.reserved_total:.4f}, "
                  f"total_profit_realized={state.total_profit_realized:.4f}")
    else:
        reserved_total = 0.0
        total_profit_realized = 0.0
        if prior_state is not None:
            cancelled = cancel_all_open_orders(trader, prior_state.levels)
            reserved_total = prior_state.reserved_total
            total_profit_realized = prior_state.total_profit_realized
            log.info(f"Grid config changed since last run — cancelled {cancelled} "
                      f"stale order(s), rebuilding fresh (carrying forward "
                      f"reserved_total={reserved_total:.4f}, "
                      f"total_profit_realized={total_profit_realized:.4f})")

        below_capital = cfg.total_capital_usdt - cfg.bulk_amount_usdt

        if cfg.grid_mode == "pilot":
            levels = build_zoned_levels(
                cfg.pilot_points, below_capital, cfg.reference_price, cfg.dca_exponent,
            )
        else:
            levels = build_hybrid_zoned_levels(
                cfg.anchor_low, cfg.dense_low, cfg.dense_high, cfg.anchor_high,
                cfg.anchor_step, cfg.dense_step, cfg.dense_sell_offset,
                below_capital, cfg.reference_price, cfg.dca_exponent,
            )
        for lvl in levels:
            lvl.buy_price = filters.round_price(lvl.buy_price)
            lvl.sell_price = filters.round_price(lvl.sell_price)
            lvl.base_qty = filters.round_qty(lvl.base_qty)

        if cfg.bulk_amount_usdt > 0:
            build_price = trader.get_price()
            bulk_buy_price = filters.round_price(build_price * 1.002)
            bulk_sell_low = filters.round_price(bulk_buy_price + cfg.bulk_sell_step)
            bulk_levels = build_bulk_ladder_levels(
                bulk_buy_price, bulk_sell_low, cfg.bulk_sell_high, cfg.bulk_sell_step,
                cfg.bulk_amount_usdt, start_index=len(levels),
            )
            armed_bulk = 0
            for lvl in bulk_levels:
                lvl.sell_price = filters.round_price(lvl.sell_price)
                qty = filters.round_qty(lvl.base_qty)
                lvl.base_qty = qty
                # Placed unconditionally here, not through the normal
                # price-gated arming path — a tiny tick between fetching
                # build_price and this point could otherwise leave the buy
                # price just above current price and skip it entirely,
                # defeating the whole point of an immediate bulk buy.
                if qty > 0 and qty * bulk_buy_price >= filters.min_notional:
                    try:
                        lvl.buy_order_id = trader.place_limit_buy(bulk_buy_price, qty)
                        lvl.qty = qty
                        lvl.state = "buy_open"
                        armed_bulk += 1
                    except Exception as exc:
                        log.warning(f"Bulk slice {lvl.index} (${lvl.sell_price}): "
                                     f"buy order rejected: {exc}")
            levels.extend(bulk_levels)
            log.info(f"Bulk ladder: buy_price={bulk_buy_price} (immediate), "
                      f"{armed_bulk}/{len(bulk_levels)} sell slices armed now from {bulk_sell_low} "
                      f"to {cfg.bulk_sell_high} (step {cfg.bulk_sell_step}), "
                      f"{cfg.bulk_amount_usdt:.2f} USDT total")

        state = BotState(levels=levels, reserved_total=reserved_total,
                          total_profit_realized=total_profit_realized,
                          config_signature=current_signature,
                          next_level_index=len(levels))
        save_state(cfg.state_file, state)
        total_committed = sum(l.base_qty * l.buy_price for l in levels)
        log.info(f"Grid rebuilt fresh: mode={cfg.grid_mode}, {len(levels)} levels, capital="
                  f"{cfg.total_capital_usdt:.2f} USDT (bulk={cfg.bulk_amount_usdt:.2f}), "
                  f"worst-case committed={total_committed:.2f} USDT")

    try:
        consecutive_errors = 0
        while True:
            last_error = None
            current_price = None
            try:
                current_price = trader.get_price()

                # Runs first, ahead of fill-checks, so a tripped breaker
                # pulls resting buy orders before this cycle's fill-check
                # loop would otherwise process any of them filling for real.
                halted = check_circuit_breaker(state, current_price, trader, cfg)

                # Fill-checks (and placing the resulting protective sell for
                # anything that just bought) run BEFORE the order window, so
                # protecting an already-owned position always gets first
                # claim on the exchange's 200-order budget — never crowded
                # out by the window opening brand-new speculative buys.
                # New levels from a successful fan-out are collected here and
                # applied to state.levels AFTER the loop — mutating the list
                # while iterating it directly would be unsafe.
                levels_to_add = []
                levels_to_remove = []
                for lvl in state.levels:
                    if lvl.state == "buy_open":
                        if trader.is_filled(lvl.buy_order_id, current_price):
                            log.info(f"Grid {lvl.index} (${lvl.buy_price}): BUY filled qty={lvl.qty}")
                            log_buy_fill(cfg.ledger_file, lvl.index, lvl.buy_price, lvl.qty,
                                         lvl.one_shot, cfg.dry_run)

                            # A filled zone-shift-consolidated bucket gets its
                            # sell fanned out into several smaller laddered
                            # orders (never below lvl.sell_price, so nothing
                            # sells for less than already promised) instead
                            # of one lump — restores near-market ladder
                            # texture for positions that got coarse while
                            # waiting to fill. Falls back to the normal
                            # single-sell path below on any fan-out failure.
                            is_fannable = (lvl.consolidated and not lvl.one_shot)
                            fanned = False
                            if is_fannable:
                                rungs = place_fanout(trader, filters, lvl.qty, lvl.sell_price)
                                if rungs:
                                    levels_to_remove.append(lvl)
                                    for order_id, price, qty in rungs:
                                        new_idx = state.next_level_index
                                        state.next_level_index += 1
                                        levels_to_add.append(GridLevel(
                                            index=new_idx, buy_price=lvl.buy_price, sell_price=price,
                                            base_qty=qty, qty=qty, state="sell_open",
                                            sell_order_id=order_id, consolidated=True,
                                        ))
                                    log.info(f"Grid {lvl.index}: filled consolidated bucket fanned "
                                              f"into {len(rungs)} laddered sell(s), "
                                              f"${rungs[0][1]}-${rungs[-1][1]}")
                                    fanned = True

                            if not fanned:
                                try:
                                    lvl.sell_order_id = trader.place_limit_sell(lvl.sell_price, lvl.qty)
                                    lvl.state = "sell_open"
                                except Exception as exc:
                                    # We already own this UNI — leave state as buy_open so
                                    # is_filled() (idempotent on an already-filled order) keeps
                                    # detecting the fill and retries placing the sell next cycle.
                                    log.warning(f"Grid {lvl.index} (${lvl.sell_price}): sell order "
                                                 f"rejected after buy filled, will retry: {exc}")

                    elif lvl.state == "sell_open":
                        if trader.is_filled(lvl.sell_order_id, current_price):
                            profit = (lvl.sell_price - lvl.buy_price) * lvl.qty
                            log.info(f"Grid {lvl.index} (${lvl.buy_price}): SELL filled "
                                      f"qty={lvl.qty} profit={profit:.4f} USDT"
                                      + (" [one_shot, closing for good]" if lvl.one_shot else ""))
                            reserved_amount = profit * cfg.profit_reserve_ratio
                            log_sell_fill(cfg.ledger_file, lvl.index, lvl.buy_price, lvl.sell_price,
                                          lvl.qty, profit, reserved_amount, profit - reserved_amount,
                                          lvl.one_shot, cfg.dry_run)
                            lvl.state = "closed" if lvl.one_shot else "idle"
                            lvl.buy_order_id = None
                            lvl.sell_order_id = None
                            lvl.qty = 0.0
                            distribute_profit(state, profit, current_price,
                                               cfg.profit_reserve_ratio, trader, filters,
                                               cfg.taker_avoidance_buffer,
                                               dense_sell_offset=cfg.dense_sell_offset,
                                               reinvest_band_pct=cfg.reinvest_band_pct,
                                               near_market_band_pct=cfg.near_market_band_pct)

                for lvl in levels_to_remove:
                    state.levels.remove(lvl)
                state.levels.extend(levels_to_add)

                # Retry any distribute-share still queued from a prior cycle
                # (both this cycle's fallback tiers came up empty at fill
                # time) at the original tight band, now that fill-checks
                # above may have armed fresh eligible levels.
                retry_pending_reinvest(state, current_price, trader, filters,
                                        cfg.taker_avoidance_buffer,
                                        dense_sell_offset=cfg.dense_sell_offset,
                                        reinvest_band_pct=cfg.reinvest_band_pct,
                                        max_pending_hours=cfg.pending_reinvest_max_hours)

                merge_same_price_sells(state, trader, filters)
                merge_same_price_buys(state, trader, filters)

                if cfg.zone_shift_enabled:
                    maybe_shift_zones(state, current_price, trader, filters, cfg)

                maybe_shift_dense_window(state, current_price, trader, filters, cfg)

                apply_order_window(state, current_price, trader, filters, cfg.max_open_orders,
                                    cfg.taker_avoidance_buffer, halted=halted)

                save_state(cfg.state_file, state)
                consecutive_errors = 0

            except KeyboardInterrupt:
                raise

            except Exception as exc:
                consecutive_errors += 1
                last_error = f"{exc.__class__.__name__}: {exc}"
                log.error(f"Iteration failed (consecutive_errors={consecutive_errors}): {last_error}")
                log.debug(traceback.format_exc())
                if consecutive_errors >= 5:
                    log.error("5+ consecutive failures — backing off for 5x the normal poll interval")
                    time.sleep(cfg.poll_interval_seconds * 5)

            write_heartbeat(cfg.heartbeat_file, current_price, state, last_error, cfg.dry_run, zone_config={
                "anchor_low": cfg.anchor_low,
                "dense_low": cfg.dense_low,
                "dense_high": cfg.dense_high,
                "anchor_high": cfg.anchor_high,
                "dense_step": cfg.dense_step,
                "dense_sell_offset": cfg.dense_sell_offset,
                "dense_window_enabled": cfg.dense_window_enabled,
                "dense_window_center": state.dense_window_center,
                "dense_window_half_width": cfg.dense_window_half_width,
            })
            time.sleep(cfg.poll_interval_seconds)

    except KeyboardInterrupt:
        log.info("Shutdown requested, saving state...")
        save_state(cfg.state_file, state)
        log.info("Stopped cleanly.")


if __name__ == "__main__":
    run()
