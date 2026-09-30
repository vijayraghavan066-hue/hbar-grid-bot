import logging
import time

log = logging.getLogger("dash-grid-bot")


def _cancel_open_buys(state, trader):
    """Pull every resting buy order — this is the actual exposure that would
    otherwise keep filling as price falls through it, which is how a grid
    bot normally accumulates a full position during a crash even though it
    stays inside its configured range the whole time. Same cancel-or-leave-
    alone safety as zone_shift._cancel_and_collect: a cancel failure almost
    certainly means the order just filled for real, so that level is left
    completely untouched for the normal fill-check loop to detect and
    protect with a sell next cycle."""
    cancelled = 0
    for lvl in state.levels:
        if lvl.state == "buy_open" and lvl.buy_order_id is not None:
            try:
                trader.cancel_order(lvl.buy_order_id)
            except Exception as exc:
                log.warning(f"Circuit breaker: Grid {lvl.index} order {lvl.buy_order_id} "
                             f"not cancellable ({exc}) — likely already filled, "
                             f"leaving untouched for normal fill detection")
                continue
            lvl.buy_order_id = None
            lvl.qty = 0.0
            lvl.state = "idle"
            cancelled += 1
    return cancelled


def check_circuit_breaker(state, current_price, trader, cfg) -> bool:
    """Returns True if new buy orders should be halted this cycle.

    Two trip conditions, either sufficient:
    1. Velocity: current price is CIRCUIT_BREAKER_DROP_PCT below the
       trailing high seen in the last CIRCUIT_BREAKER_WINDOW_HOURS. This is
       the one that matters — it catches a fast drop through orders that
       were already resting on the exchange from before the crash, which
       stay "in range" the whole time and so a static min/max check alone
       would never see them as a problem.
    2. Floor breach: current price has fallen below ANCHOR_LOW outright —
       catches a slower grind past the whole configured range.

    On trip: cancels every resting buy order (never touches sell_open —
    owned inventory keeps its exit order exactly where it is; no forced
    liquidation, ever) and records the event in state. Auto-resumes once
    price recovers CIRCUIT_BREAKER_RESUME_PCT off the lowest price seen
    since the trip, with no human action required.
    """
    now = time.time()
    state.price_history.append([now, current_price])
    window_seconds = cfg.circuit_breaker_window_hours * 3600
    state.price_history = [p for p in state.price_history if now - p[0] <= window_seconds]

    if not cfg.circuit_breaker_enabled:
        return False

    if state.circuit_breaker_tripped:
        state.circuit_breaker_trip_low = min(state.circuit_breaker_trip_low, current_price)
        resume_price = state.circuit_breaker_trip_low * (1 + cfg.circuit_breaker_resume_pct)
        if current_price >= resume_price:
            log.warning(f"Circuit breaker CLEARED: price ${current_price} recovered "
                         f"{cfg.circuit_breaker_resume_pct * 100:.1f}% off the low of "
                         f"${state.circuit_breaker_trip_low} reached since the trip "
                         f"({state.circuit_breaker_reason})")
            state.circuit_breaker_tripped = False
            state.circuit_breaker_reason = ""
            state.circuit_breaker_tripped_ts = 0.0
            state.circuit_breaker_trip_low = 0.0
            return False
        return True

    trailing_high = max((p[1] for p in state.price_history), default=current_price)
    drop_pct = (trailing_high - current_price) / trailing_high if trailing_high > 0 else 0.0

    reason = None
    if drop_pct >= cfg.circuit_breaker_drop_pct:
        reason = (f"price ${current_price} is {drop_pct * 100:.1f}% below the trailing "
                   f"{cfg.circuit_breaker_window_hours:.0f}h high of ${trailing_high:.4f}")
    elif current_price < cfg.anchor_low:
        reason = f"price ${current_price} fell below the configured floor ${cfg.anchor_low}"

    if reason is None:
        return False

    cancelled = _cancel_open_buys(state, trader)
    state.circuit_breaker_tripped = True
    state.circuit_breaker_reason = reason
    state.circuit_breaker_tripped_ts = now
    state.circuit_breaker_trip_low = current_price
    log.error(f"CIRCUIT BREAKER TRIPPED: {reason} — cancelled {cancelled} resting buy "
               f"order(s), new buys halted until price recovers "
               f"{cfg.circuit_breaker_resume_pct * 100:.1f}% off the low "
               f"(sell_open positions untouched)")
    return True
