import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from grid import GridLevel
from state import BotState
import pct_ladder as pl


def make_cfg(**overrides):
    base = dict(
        anchor_low=0.02, anchor_high=0.20,
        ladder1_step_pct=0.01, ladder1_offset_pct=0.02, ladder1_window_pct=0.10,
        ladder2_step_pct=0.02, ladder2_offset_pct=0.04, ladder2_window_pct=0.20,
        ladder3_step_pct=0.03, ladder3_offset_pct=0.06, ladder3_window_pct=0.30,
        ladder4_step_pct=0.04, ladder4_offset_pct=0.08, ladder4_window_pct=0.40,
        ladder5_step_pct=0.05, ladder5_offset_pct=0.10, ladder5_window_pct=0.50,
        reserved_draw_floor_pct=0.5, reserved_topup_threshold_usd=20.0,
        reference_price=0.09, dca_exponent=1.0, profit_reserve_ratio=0.5,
        pct_ladder_enabled=True, taker_avoidance_buffer=0.0005,
        # Every ladder-native sale's profit splits 4 ways (must sum to 1.0)
        # -- accumulation (never drawn), buy-side compounding, ladder
        # reserve (still auto-drawable), coin reserve. Equal 25% default,
        # same as live.
        accum_ratio=0.25, compound_ratio=0.25,
        ladder_reserve_ratio=0.25, coin_reserve_ratio=0.25,
    )
    base.update(overrides)
    return type("Cfg", (), base)()


class FakeTrader:
    def __init__(self, fail_cancel_ids=None, price=0.09, fail_buy=False, fail_sell=False):
        self.cancelled = []
        self.fail_cancel_ids = fail_cancel_ids or set()
        self.price = price
        self.fail_buy = fail_buy
        self.fail_sell = fail_sell
        self.placed_buys = []   # list[(price, qty)]
        self.placed_sells = []  # list[(price, qty)]
        self._next_id = 0

    def cancel_order(self, order_id):
        if order_id in self.fail_cancel_ids:
            raise RuntimeError("already filled")
        self.cancelled.append(order_id)

    def get_price(self):
        return self.price

    def place_limit_buy(self, price, qty):
        if self.fail_buy:
            raise RuntimeError("buy rejected")
        self.placed_buys.append((price, qty))
        self._next_id += 1
        return f"buy-{self._next_id}"

    def place_limit_sell(self, price, qty):
        if self.fail_sell:
            raise RuntimeError("sell rejected")
        self.placed_sells.append((price, qty))
        self._next_id += 1
        return f"sell-{self._next_id}"


class FakeFilters:
    def __init__(self, min_notional=0.01):
        self.min_notional = min_notional

    def round_qty(self, qty):
        return round(qty, 6)

    def round_price(self, price):
        return round(price, 6)


class TestBuildLadderPoints:
    def test_geometric_spacing_covers_range(self):
        points = pl.build_ladder_points(0.08, 0.10, 0.01)
        assert points[0] == pytest.approx(0.08)
        assert points[-1] <= 0.10 + 1e-9
        for a, b in zip(points, points[1:]):
            assert b / a == pytest.approx(1.01, rel=1e-6)

    def test_empty_when_hi_below_lo(self):
        assert pl.build_ladder_points(0.10, 0.08, 0.01) == []

    def test_empty_on_nonpositive_bounds(self):
        assert pl.build_ladder_points(0.0, 0.10, 0.01) == []

    def test_rejects_nonpositive_step(self):
        with pytest.raises(ValueError):
            pl.build_ladder_points(0.08, 0.10, 0.0)


class TestComputeLadderZones:
    def test_one_sided_window_ends_at_current_price(self):
        cfg = make_cfg()
        zones = pl.compute_ladder_zones(0.10, cfg)
        lo, hi = zones["ladder1"]
        assert hi == pytest.approx(0.10)
        assert lo == pytest.approx(0.10 * 0.90)  # 10% window

    def test_clipped_to_anchor_bounds(self):
        cfg = make_cfg(anchor_low=0.095)
        zones = pl.compute_ladder_zones(0.10, cfg)
        lo, _ = zones["ladder1"]
        assert lo == pytest.approx(0.095)  # would be 0.09 unclipped, anchor_low wins

    def test_different_ladders_have_different_window_widths(self):
        cfg = make_cfg()
        zones = pl.compute_ladder_zones(0.10, cfg)
        lo1, _ = zones["ladder1"]
        lo3, _ = zones["ladder3"]
        assert lo3 < lo1  # ladder3's wider window_pct reaches further down


class TestIsStuckIdle:
    def test_idle_above_current_price_is_stuck(self):
        lvl = GridLevel(index=0, buy_price=0.10, sell_price=0.102, base_qty=10, state="idle")
        assert pl._is_stuck_idle(lvl, current_price=0.09, min_notional=0.01) is True

    def test_idle_below_current_price_not_stuck(self):
        lvl = GridLevel(index=0, buy_price=0.08, sell_price=0.082, base_qty=10, state="idle")
        assert pl._is_stuck_idle(lvl, current_price=0.09, min_notional=0.01) is False

    def test_non_idle_never_stuck(self):
        lvl = GridLevel(index=0, buy_price=0.10, sell_price=0.102, base_qty=10, state="buy_open")
        assert pl._is_stuck_idle(lvl, current_price=0.09, min_notional=0.01) is False

    def test_decayed_notional_below_minimum_is_stuck(self):
        lvl = GridLevel(index=0, buy_price=0.08, sell_price=0.082, base_qty=0.05, state="idle")
        assert pl._is_stuck_idle(lvl, current_price=0.09, min_notional=0.01) is True


class TestExactLadderMatch:
    def test_exact_match(self):
        cfg = make_cfg()
        assert pl._exact_ladder_match(0.04, cfg) == "ladder2"

    def test_legacy_offset_matches_nothing(self):
        # HBAR's old DENSE_SELL_OFFSET/DENSE_LOW ~ 4.6% -- close to
        # ladder2's 4% but NOT exact, so this must return None (routes to
        # the even-split path), not "ladder2".
        cfg = make_cfg()
        assert pl._exact_ladder_match(0.046, cfg) is None

    def test_within_tolerance_still_counts_as_exact(self):
        cfg = make_cfg()
        assert pl._exact_ladder_match(0.0401, cfg) == "ladder2"  # within OFFSET_TOLERANCE_PCT of 0.04

    def test_boundary_outside_tolerance_matches_nothing(self):
        cfg = make_cfg()
        assert pl._exact_ladder_match(0.029, cfg) is None  # 0.9pp off ladder1's 2%, outside tolerance


class TestDistributeProfitLadderAware:
    def test_exact_match_routes_fully_to_owning_ladder(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(), FakeFilters()
        name = pl.distribute_profit_ladder_aware(state, profit=1.0, sold_offset_pct=0.02, cfg=cfg,
                                                   trader=trader, filters=filters)
        assert name == "ladder1"
        assert state.ladder_reserved["ladder1"] == pytest.approx(0.25)
        assert state.ladder_reserved_contributed["ladder1"] == pytest.approx(0.25)
        assert state.pending_reinvest_by_band[pl._band_key(0.02)] == pytest.approx(0.25)
        assert state.reserved_total == pytest.approx(0.25)
        assert state.total_profit_realized == pytest.approx(1.0)

    def test_exact_match_other_ladders_untouched(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(), FakeFilters()
        pl.distribute_profit_ladder_aware(state, profit=1.0, sold_offset_pct=0.02, cfg=cfg,
                                            trader=trader, filters=filters)
        assert state.ladder_reserved.get("ladder2", 0.0) == 0.0
        assert state.ladder_reserved.get("ladder3", 0.0) == 0.0

    def test_legacy_position_splits_evenly_across_all_five(self):
        # Reproduces the real scenario: legacy positions whose gap is
        # closest to one ladder's offset but not an exact match must NOT
        # have their profit funneled entirely into that one ladder -- it
        # splits evenly across all 5 instead (HBAR extended to 5 ladders
        # 2026-10-11, matching XRP/UNI, after "nearest ladder" routing was
        # found there to starve the other ladders).
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(), FakeFilters()
        name = pl.distribute_profit_ladder_aware(state, profit=3.0, sold_offset_pct=0.046, cfg=cfg,
                                                   trader=trader, filters=filters)
        assert name == "legacy-even-split"
        for n in range(1, 6):
            ladder = f"ladder{n}"
            assert state.ladder_reserved[ladder] == pytest.approx(0.15)  # 3.0/5=0.6 share, 25% to ladder reserve
            assert state.ladder_reserved_contributed[ladder] == pytest.approx(0.15)
        assert state.total_profit_realized == pytest.approx(3.0)

    def test_legacy_position_queues_pending_reinvest_for_every_ladder(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(), FakeFilters()
        pl.distribute_profit_ladder_aware(state, profit=3.0, sold_offset_pct=0.046, cfg=cfg,
                                            trader=trader, filters=filters)
        for _, offset_pct, _, _ in pl.ladder_specs(cfg):
            assert state.pending_reinvest_by_band[pl._band_key(offset_pct)] == pytest.approx(0.15)

    def test_zero_or_negative_profit_is_noop(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(), FakeFilters()
        pl.distribute_profit_ladder_aware(state, profit=0.0, sold_offset_pct=0.02, cfg=cfg,
                                            trader=trader, filters=filters)
        assert state.ladder_reserved == {}
        assert state.total_profit_realized == 0.0


class TestDrawFromLadderReserve:
    def test_caps_at_floor_pct(self):
        cfg = make_cfg(reserved_draw_floor_pct=0.5)
        state = BotState(ladder_reserved={"ladder1": 10.0})
        draw = pl._draw_from_ladder_reserve(state, cfg, "ladder1")
        assert draw == pytest.approx(5.0)
        assert state.ladder_reserved["ladder1"] == pytest.approx(5.0)

    def test_never_touches_a_different_ladders_pool(self):
        cfg = make_cfg()
        state = BotState(ladder_reserved={"ladder1": 10.0, "ladder2": 50.0})
        pl._draw_from_ladder_reserve(state, cfg, "ladder1")
        assert state.ladder_reserved["ladder2"] == 50.0  # untouched

    def test_empty_pool_draws_nothing(self):
        cfg = make_cfg()
        state = BotState(ladder_reserved={"ladder1": 0.0})
        assert pl._draw_from_ladder_reserve(state, cfg, "ladder1") == 0.0

    def test_max_amount_caps_below_floor_limit(self):
        cfg = make_cfg(reserved_draw_floor_pct=0.5)
        state = BotState(ladder_reserved={"ladder1": 10.0})
        draw = pl._draw_from_ladder_reserve(state, cfg, "ladder1", max_amount=2.0)
        assert draw == pytest.approx(2.0)
        assert state.ladder_reserved["ladder1"] == pytest.approx(8.0)

    def test_records_cumulative_drawn(self):
        cfg = make_cfg(reserved_draw_floor_pct=0.5)
        state = BotState(ladder_reserved={"ladder1": 10.0})
        pl._draw_from_ladder_reserve(state, cfg, "ladder1")
        pl._draw_from_ladder_reserve(state, cfg, "ladder1")
        assert state.ladder_reserved_drawn["ladder1"] == pytest.approx(5.0 + 2.5)

    def test_already_negligible_reserve_snaps_to_zero_without_drawing(self):
        # Reproduces the infinite no-op loop found live 2026-10-13: a
        # long-dry ladder's reserve decayed (via repeated floor-capped
        # draws' geometric decay) down to subatomic float dust (1e-96,
        # 1e-210 seen on DASH/HBAR) that never reaches exactly zero,
        # triggering a no-op reshape every single poll cycle forever.
        cfg = make_cfg(reserved_draw_floor_pct=0.5)
        state = BotState(ladder_reserved={"ladder1": 1e-96})
        draw = pl._draw_from_ladder_reserve(state, cfg, "ladder1")
        assert draw == 0.0
        assert state.ladder_reserved["ladder1"] == 0.0  # snapped, not left as dust

    def test_draw_that_would_leave_negligible_dust_takes_it_all_instead(self):
        cfg = make_cfg(reserved_draw_floor_pct=0.5)
        state = BotState(ladder_reserved={"ladder1": 2e-6})  # floor-capped remainder would be 1e-6 dust
        draw = pl._draw_from_ladder_reserve(state, cfg, "ladder1")
        assert state.ladder_reserved["ladder1"] == 0.0  # no dust left behind
        assert state.ladder_reserved_drawn["ladder1"] == pytest.approx(draw)


class TestReshapeLadder:
    def test_builds_fresh_levels_filling_the_window(self):
        cfg = make_cfg()
        state = BotState(levels=[], ladder_reserved={"ladder1": 5.0})
        trader, filters = FakeTrader(), FakeFilters()
        n = pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                               trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert n > 0
        assert len(state.levels) == n
        for lvl in state.levels:
            assert lvl.buy_price < 0.09
            assert lvl.sell_price == pytest.approx(lvl.buy_price * 1.02)
            assert lvl.state == "idle"

    def test_draws_from_its_own_dry_reserve_to_reseed(self):
        cfg = make_cfg()
        state = BotState(levels=[], ladder_reserved={"ladder1": 20.0})
        trader, filters = FakeTrader(), FakeFilters()
        pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                           trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert state.ladder_reserved["ladder1"] < 20.0  # drew from itself to seed the empty ladder
        assert sum(l.target_qty() * l.buy_price for l in state.levels) > 0

    def test_never_draws_from_a_different_ladders_reserve(self):
        cfg = make_cfg()
        state = BotState(levels=[], ladder_reserved={"ladder1": 20.0, "ladder2": 999.0})
        trader, filters = FakeTrader(), FakeFilters()
        pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                           trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert state.ladder_reserved["ladder2"] == 999.0

    def test_held_sell_open_levels_never_touched(self):
        cfg = make_cfg()
        held = GridLevel(index=0, buy_price=0.085, sell_price=0.0867, base_qty=10, state="sell_open")
        state = BotState(levels=[held], ladder_reserved={"ladder1": 5.0})
        trader, filters = FakeTrader(), FakeFilters()
        pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                           trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert held in state.levels
        assert held.state == "sell_open"
        assert held.buy_price == 0.085  # untouched

    def test_one_shot_levels_never_touched(self):
        cfg = make_cfg()
        bulk = GridLevel(index=0, buy_price=0.085, sell_price=0.0867, base_qty=10, state="idle", one_shot=True)
        state = BotState(levels=[bulk], ladder_reserved={"ladder1": 5.0})
        trader, filters = FakeTrader(), FakeFilters()
        pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                           trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert bulk in state.levels
        assert bulk.buy_price == 0.085

    def test_cancel_failure_leaves_level_untouched(self):
        """A level whose cancel fails (already filled for real) must be
        left completely alone -- same safety property as DASH's version."""
        cfg = make_cfg()
        stuck = GridLevel(index=0, buy_price=0.05, sell_price=0.051, base_qty=10,
                           state="buy_open", buy_order_id="order-1")  # outside the new window -> mismatched
        state = BotState(levels=[stuck], ladder_reserved={"ladder1": 5.0})
        trader = FakeTrader(fail_cancel_ids={"order-1"})
        filters = FakeFilters()
        pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                           trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert stuck in state.levels
        assert stuck.buy_price == 0.05
        assert stuck.state == "buy_open"

    def test_noop_when_window_already_fully_covered_and_reserve_empty(self):
        cfg = make_cfg()
        points = pl.build_ladder_points(0.081, 0.09, 0.01)
        existing = [GridLevel(index=i, buy_price=p, sell_price=p * 1.02, base_qty=5, state="idle")
                    for i, p in enumerate(points)]
        state = BotState(levels=list(existing), ladder_reserved={"ladder1": 0.0})
        trader, filters = FakeTrader(), FakeFilters()
        n = pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                               trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert n == 0
        assert len(state.levels) == len(existing)

    def test_draw_too_thin_to_build_grows_existing_survivor_instead_of_vanishing(self):
        # Reproduces the 2026-10-08 bug: a reserve draw small enough that no
        # NEW price point can clear min_notional must not be silently
        # discarded -- with a real survivor already in the window (whose
        # own notional comfortably clears min_notional, so it isn't itself
        # flagged stuck), the draw should grow that survivor instead.
        cfg = make_cfg(reserved_topup_threshold_usd=100.0)
        survivor = GridLevel(index=0, buy_price=0.085, sell_price=0.085 * 1.02, base_qty=20.0, state="idle")
        state = BotState(levels=[survivor], ladder_reserved={"ladder1": 5.0})
        trader = FakeTrader()
        # survivor's own notional (20*0.085=1.7) clears this; a thin
        # gap-fill share of the ~$2.5 draw split across ~10 other points
        # in this window (~$0.25 each) does not.
        filters = FakeFilters(min_notional=1.0)
        before_qty = survivor.target_qty()
        before_reserve = state.ladder_reserved["ladder1"]
        n = pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                               trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert n == 0
        assert len(state.levels) == 1  # no new level fabricated
        assert survivor.target_qty() > before_qty  # the draw was applied to it, not lost
        assert state.ladder_reserved["ladder1"] < before_reserve  # something was genuinely drawn

    def test_draw_too_thin_to_build_and_no_survivor_returns_capital_to_reserve(self):
        # Same too-thin-to-build scenario but with NO existing level to
        # grow either -- the only safe outcome is giving the drawn capital
        # back to the ladder's own reserve, not discarding it.
        cfg = make_cfg(reserved_topup_threshold_usd=100.0)
        state = BotState(levels=[], ladder_reserved={"ladder1": 5.0})
        trader = FakeTrader()
        filters = FakeFilters(min_notional=1000.0)  # impossibly high -- nothing can ever clear this
        n = pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                               trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert n == 0
        assert state.levels == []
        assert state.ladder_reserved["ladder1"] == pytest.approx(5.0)  # drawn, then fully returned -- not lost
        assert state.ladder_reserved_drawn.get("ladder1", 0.0) == pytest.approx(0.0)  # draw-then-return must not inflate the all-time counter

    def test_completely_dry_bypasses_retention_floor_when_only_full_reserve_is_enough(self):
        # The normal floor-capped draw (reserved_draw_floor_pct) can leave a
        # completely dry ladder permanently stuck at zero rungs even with
        # real money sitting in its own reserve, because a dry ladder earns
        # no profit of its own to ever grow past that point -- found live
        # 2026-10-12 across XRP/UNI/ZEC/HBAR (manually fixed each time by
        # injecting fresh capital). When the FULL reserve -- not just the
        # floor-capped share -- would be enough to place one real order,
        # escaping zero rungs takes priority over the normal retention floor.
        cfg = make_cfg(reserved_topup_threshold_usd=100.0, reserved_draw_floor_pct=0.5)
        filters = FakeFilters(min_notional=1.0)
        needed_min = pl._min_capital_for_one_level(lo=1.33, hi=1.40, step_pct=0.01,
                                                     reference_price=cfg.reference_price,
                                                     dca_exponent=cfg.dca_exponent,
                                                     min_notional=filters.min_notional)
        reserve = needed_min + 0.50  # 50% of this is NOT enough; 100% is
        assert reserve * 0.5 < needed_min  # sanity: confirms the scenario is real
        state = BotState(levels=[], ladder_reserved={"ladder1": reserve})
        trader = FakeTrader()
        n = pl.reshape_ladder(state, "ladder1", lo=1.33, hi=1.40, step_pct=0.01, offset_pct=0.01,
                               trader=trader, filters=filters, cfg=cfg, current_price=1.40)
        assert n > 0  # at least one real rung got built, not left stuck at zero
        assert all(lvl.state == "idle" for lvl in state.levels)
        assert state.ladder_reserved.get("ladder1", 0.0) < reserve  # the bypass draw actually happened

    def test_completely_dry_still_respects_floor_when_capped_draw_is_already_enough(self):
        # In the common, healthy case -- the floor-capped draw alone already
        # clears min_notional -- the bypass must NOT fire. The retention
        # floor only gets overridden as a last resort, never routinely.
        cfg = make_cfg(reserved_topup_threshold_usd=100.0, reserved_draw_floor_pct=0.5)
        filters = FakeFilters(min_notional=1.0)
        needed_min = pl._min_capital_for_one_level(lo=1.33, hi=1.40, step_pct=0.01,
                                                     reference_price=cfg.reference_price,
                                                     dca_exponent=cfg.dca_exponent,
                                                     min_notional=filters.min_notional)
        reserve = needed_min * 3  # comfortably more than enough even at 50%
        state = BotState(levels=[], ladder_reserved={"ladder1": reserve})
        trader = FakeTrader()
        n = pl.reshape_ladder(state, "ladder1", lo=1.33, hi=1.40, step_pct=0.01, offset_pct=0.01,
                               trader=trader, filters=filters, cfg=cfg, current_price=1.40)
        assert n > 0
        assert state.ladder_reserved.get("ladder1", 0.0) >= reserve * 0.5 - 0.01

    def test_completely_dry_gracefully_stays_dry_when_even_full_reserve_isnt_enough(self):
        # If even 100% of the reserve would not clear min_notional, there is
        # nothing the bootstrap override can do -- falls back to the existing
        # graceful behavior (capital returned, never lost, ladder stays dry
        # until real outside capital arrives).
        cfg = make_cfg(reserved_topup_threshold_usd=100.0, reserved_draw_floor_pct=0.5)
        filters = FakeFilters(min_notional=1.0)
        needed_min = pl._min_capital_for_one_level(lo=1.33, hi=1.40, step_pct=0.01,
                                                     reference_price=cfg.reference_price,
                                                     dca_exponent=cfg.dca_exponent,
                                                     min_notional=filters.min_notional)
        reserve = needed_min * 0.1  # nowhere near enough even at 100%
        state = BotState(levels=[], ladder_reserved={"ladder1": reserve})
        trader = FakeTrader()
        n = pl.reshape_ladder(state, "ladder1", lo=1.33, hi=1.40, step_pct=0.01, offset_pct=0.01,
                               trader=trader, filters=filters, cfg=cfg, current_price=1.40)
        assert n == 0
        assert state.ladder_reserved.get("ladder1", 0.0) == pytest.approx(reserve)

    def test_growing_existing_levels_records_per_rung_reinvest_note(self):
        # Per-rung lifetime tracking: when reshape_ladder grows SURVIVING
        # levels instead of building fresh ones (window already fully
        # covered -- every geometric point already has a survivor), that
        # specific rung should carry a record of what just got reinvested
        # into it -- shown on the dashboard as a "reinvested" note next to
        # that exact price.
        cfg = make_cfg(reserved_topup_threshold_usd=0.0)
        lo, hi, step_pct = 1.33, 1.36, 0.01
        points = pl.build_ladder_points(lo, hi, step_pct)
        survivors = [GridLevel(index=i, buy_price=p, sell_price=round(p * 1.01, 10), base_qty=10.0,
                                state="idle") for i, p in enumerate(points)]
        state = BotState(levels=list(survivors),
                          pending_reinvest_by_band={pl._band_key(0.01): 2.0})
        trader = FakeTrader()
        filters = FakeFilters(min_notional=1.0)
        n = pl.reshape_ladder(state, "ladder1", lo=lo, hi=hi, step_pct=step_pct, offset_pct=0.01,
                               trader=trader, filters=filters, cfg=cfg, current_price=1.36)
        assert n == 0  # grew survivors, didn't build fresh levels (window already fully covered)
        expected_per_level = 2.0 / len(survivors)
        for lvl in survivors:
            assert lvl.extra_qty > 0
            assert lvl.last_reinvest_usd == pytest.approx(expected_per_level)
            assert lvl.last_reinvest_ts > 0
        # A level this function never touches keeps its reinvest fields untouched.
        untouched = GridLevel(index=99, buy_price=0.50, sell_price=0.50 * 1.05, base_qty=5.0, state="idle")
        assert untouched.last_reinvest_usd == 0.0
        assert untouched.last_reinvest_ts == 0.0

    def test_fresh_gap_fill_levels_start_with_no_reinvest_history(self):
        # A brand-new rung built to fill a genuine window gap is NOT the
        # same thing as reinvesting into an existing one -- it should start
        # with zero lifetime profit and no reinvest note, not inherit
        # anything from the capital that funded it.
        cfg = make_cfg(reserved_topup_threshold_usd=0.0)
        state = BotState(levels=[], pending_reinvest_by_band={pl._band_key(0.01): 50.0})
        trader = FakeTrader()
        filters = FakeFilters(min_notional=1.0)
        n = pl.reshape_ladder(state, "ladder1", lo=1.33, hi=1.36, step_pct=0.01, offset_pct=0.01,
                               trader=trader, filters=filters, cfg=cfg, current_price=1.36)
        assert n > 0
        for lvl in state.levels:
            assert lvl.lifetime_profit == 0.0
            assert lvl.last_reinvest_usd == 0.0
            assert lvl.last_reinvest_ts == 0.0

    def test_out_of_window_buy_open_level_is_never_cancelled(self):
        # 2026-10-15, explicit request: a real resting order, once live,
        # should never be cancelled just because the window has since
        # drifted past it -- coverage should only ever grow, funded from
        # the ladder's own reserve, never by uprooting existing orders.
        # A buy_open level far outside [lo, hi] must survive reshape
        # completely untouched: still in state.levels, never cancelled.
        cfg = make_cfg(reserved_topup_threshold_usd=0.0)
        stale = GridLevel(index=0, buy_price=1.00, sell_price=1.00 * 1.01, base_qty=20.0,
                           state="buy_open", buy_order_id="abc")
        state = BotState(levels=[stale], ladder_reserved={"ladder1": 0.0})
        trader = FakeTrader()
        filters = FakeFilters(min_notional=1.0)
        n = pl.reshape_ladder(state, "ladder1", lo=1.33, hi=1.40, step_pct=0.01, offset_pct=0.01,
                               trader=trader, filters=filters, cfg=cfg, current_price=1.40)
        assert n == 0
        assert state.levels == [stale]  # untouched -- never collected, never cancelled
        assert trader.cancelled == []

    def test_stuck_idle_level_collected_alone_is_not_discarded(self):
        # Reproduces the bug found live 2026-10-12, now via the only path
        # that can still collect a level: a genuinely stuck IDLE level
        # (never a real order) whose collected capital then cannot clear
        # min_notional and has no survivor to grow was being silently
        # discarded -- not refunded to ladder_reserved, not logged, just
        # gone from the ladder's own books, even though no real order (and
        # therefore no real money) was ever placed.
        cfg = make_cfg(reserved_topup_threshold_usd=100.0)
        # buy_price >= current_price makes this idle level structurally
        # stuck (would cross the spread as a taker) -- the only remaining
        # trigger inside needs_reshaping.
        stuck = GridLevel(index=0, buy_price=1.40, sell_price=1.40 * 1.01, base_qty=20.0, state="idle")
        state = BotState(levels=[stuck], ladder_reserved={"ladder1": 0.0})
        trader = FakeTrader()
        filters = FakeFilters(min_notional=1000.0)  # impossibly high -- nothing can ever clear this
        n = pl.reshape_ladder(state, "ladder1", lo=1.33, hi=1.40, step_pct=0.01, offset_pct=0.01,
                               trader=trader, filters=filters, cfg=cfg, current_price=1.40)
        assert n == 0
        assert state.levels == []  # the stuck idle level was collected and removed
        assert state.ladder_reserved["ladder1"] == pytest.approx(28.0)  # collected capital landed in reserve, not lost

    def test_repeated_draw_and_return_does_not_inflate_drawn_counter(self):
        # Reproduces the bug found live 2026-10-08: Ladder 3 had drawn
        # $3,108 "all-time" against only $5.92 ever contributed, because a
        # ladder stuck in this draw-then-return loop accumulated the draw
        # amount every single cycle even though no real capital was ever
        # actually lost (it's returned right back every time).
        cfg = make_cfg(reserved_topup_threshold_usd=100.0)
        state = BotState(levels=[], ladder_reserved={"ladder1": 5.0})
        trader = FakeTrader()
        filters = FakeFilters(min_notional=1000.0)
        for _ in range(20):
            pl.reshape_ladder(state, "ladder1", lo=0.081, hi=0.09, step_pct=0.01, offset_pct=0.02,
                               trader=trader, filters=filters, cfg=cfg, current_price=0.09)
        assert state.ladder_reserved["ladder1"] == pytest.approx(5.0)
        assert state.ladder_reserved_drawn.get("ladder1", 0.0) == pytest.approx(0.0)


class TestExtendLadderCoverage:
    def test_no_existing_levels_is_a_noop(self):
        # A ladder with nothing built yet is reshape_ladder's own bootstrap
        # job, not this function's -- extending downward from nothing
        # isn't meaningful.
        cfg = make_cfg()
        state = BotState(levels=[], ladder_reserved={"ladder1": 100.0})
        n = pl.extend_ladder_coverage(state, "ladder1", step_pct=0.01, offset_pct=0.02,
                                       cfg=cfg, filters=FakeFilters(min_notional=1.0))
        assert n == 0
        assert state.levels == []
        assert state.ladder_reserved["ladder1"] == pytest.approx(100.0)  # untouched, no draw happened

    def test_already_at_anchor_low_is_a_noop(self):
        cfg = make_cfg(anchor_low=0.08)
        existing = GridLevel(index=0, buy_price=0.08, sell_price=0.08 * 1.02, base_qty=10.0, state="sell_open")
        state = BotState(levels=[existing], ladder_reserved={"ladder1": 100.0})
        n = pl.extend_ladder_coverage(state, "ladder1", step_pct=0.01, offset_pct=0.02,
                                       cfg=cfg, filters=FakeFilters(min_notional=1.0))
        assert n == 0
        assert state.levels == [existing]
        assert state.ladder_reserved["ladder1"] == pytest.approx(100.0)

    def test_builds_new_rungs_below_the_existing_lowest(self):
        cfg = make_cfg(anchor_low=0.02)
        existing = GridLevel(index=0, buy_price=0.085, sell_price=0.085 * 1.02, base_qty=10.0, state="sell_open")
        state = BotState(levels=[existing], ladder_reserved={"ladder1": 50.0})
        n = pl.extend_ladder_coverage(state, "ladder1", step_pct=0.01, offset_pct=0.02,
                                       cfg=cfg, filters=FakeFilters(min_notional=1.0))
        assert n > 0
        new_levels = [lvl for lvl in state.levels if lvl is not existing]
        assert len(new_levels) == n
        for lvl in new_levels:
            assert lvl.buy_price < existing.buy_price  # strictly below the old lowest, never rebuilding it
            assert lvl.state == "idle"
        # Reserve floor-respecting draw: half of the 50.0 available.
        assert state.ladder_reserved["ladder1"] == pytest.approx(25.0)

    def test_batch_is_capped_even_with_abundant_capital(self):
        # Huge reserve and a far-away floor shouldn't build past
        # EXTEND_BATCH_SIZE rungs in a single cycle -- later cycles keep
        # inching the floor down instead of jumping straight to it.
        cfg = make_cfg(anchor_low=0.001, reserved_draw_floor_pct=0.5)
        existing = GridLevel(index=0, buy_price=1.0, sell_price=1.0 * 1.02, base_qty=10.0, state="sell_open")
        state = BotState(levels=[existing], ladder_reserved={"ladder1": 1_000_000.0})
        n = pl.extend_ladder_coverage(state, "ladder1", step_pct=0.01, offset_pct=0.02,
                                       cfg=cfg, filters=FakeFilters(min_notional=1.0))
        assert n == pl.EXTEND_BATCH_SIZE

    def test_nothing_buildable_returns_the_draw_untouched(self):
        cfg = make_cfg(reserved_topup_threshold_usd=100.0, anchor_low=0.02)
        existing = GridLevel(index=0, buy_price=0.085, sell_price=0.085 * 1.02, base_qty=10.0, state="sell_open")
        state = BotState(levels=[existing], ladder_reserved={"ladder1": 50.0})
        filters = FakeFilters(min_notional=100000.0)  # impossibly high -- nothing can ever clear this
        n = pl.extend_ladder_coverage(state, "ladder1", step_pct=0.01, offset_pct=0.02, cfg=cfg, filters=filters)
        assert n == 0
        assert state.levels == [existing]  # nothing added
        assert state.ladder_reserved["ladder1"] == pytest.approx(50.0)  # draw undone, not lost
        assert state.ladder_reserved_drawn.get("ladder1", 0.0) == pytest.approx(0.0)


class TestMaybeReshapeLadders:
    def test_disabled_flag_is_a_full_noop(self):
        cfg = make_cfg(pct_ladder_enabled=False)
        state = BotState(levels=[], ladder_reserved={"ladder1": 100.0, "ladder2": 100.0, "ladder3": 100.0})
        trader, filters = FakeTrader(), FakeFilters()
        n = pl.maybe_reshape_ladders(state, 0.09, trader, filters, cfg)
        assert n == 0
        assert state.levels == []

    def test_enabled_builds_all_three_ladders(self):
        cfg = make_cfg()
        state = BotState(levels=[], ladder_reserved={"ladder1": 10.0, "ladder2": 10.0, "ladder3": 10.0})
        trader, filters = FakeTrader(), FakeFilters()
        pl.maybe_reshape_ladders(state, 0.09, trader, filters, cfg)
        gaps = {round(lvl.sell_price / lvl.buy_price - 1.0, 4) for lvl in state.levels}
        assert round(0.02, 4) in gaps
        assert round(0.04, 4) in gaps
        assert round(0.06, 4) in gaps


class TestGrowCoinReserve:
    """_grow_coin_reserve takes an already-sized coin amount directly -- the
    splitting decision (how much of a sale's profit is coin-eligible) is
    made by _split_profit_four_ways, tested separately below."""

    def test_buys_coin_with_the_given_amount(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=0.01)
        pl._grow_coin_reserve(state, "ladder2", 0.10, trader, filters, cfg)
        assert state.coin_reserve_qty["ladder2"] > 0
        assert state.pending_coin_growth.get("ladder2", 0.0) == pytest.approx(0.0)

    def test_growth_grows_an_already_nonzero_reserve_with_weighted_avg_cost_basis(self):
        cfg = make_cfg()
        state = BotState(coin_reserve_qty={"ladder2": 100.0}, coin_reserve_cost_basis={"ladder2": 0.08})
        trader, filters = FakeTrader(price=0.12), FakeFilters(min_notional=0.01)  # buy at a different price
        pl._grow_coin_reserve(state, "ladder2", 1.0, trader, filters, cfg)
        new_qty = state.coin_reserve_qty["ladder2"]
        assert new_qty > 100.0  # grew, didn't just overwrite
        assert 0.08 < state.coin_reserve_cost_basis["ladder2"] < 0.1203

    def test_failed_growth_buy_accumulates_in_pending_coin_growth_not_lost(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09, fail_buy=True), FakeFilters(min_notional=0.01)
        pl._grow_coin_reserve(state, "ladder2", 0.10, trader, filters, cfg)
        assert state.coin_reserve_qty.get("ladder2", 0.0) == 0.0
        assert state.pending_coin_growth["ladder2"] == pytest.approx(0.10)  # waiting, not lost

    def test_too_thin_amount_accumulates_in_pending_coin_growth(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=1000.0)
        pl._grow_coin_reserve(state, "ladder2", 0.10, trader, filters, cfg)
        assert state.coin_reserve_qty.get("ladder2", 0.0) == 0.0
        assert state.pending_coin_growth["ladder2"] == pytest.approx(0.10)

    def test_accumulates_across_too_thin_calls_until_it_clears_min_notional(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=0.01)
        pl._grow_coin_reserve(state, "ladder2", 0.005, trader, filters, cfg)
        assert state.coin_reserve_qty.get("ladder2", 0.0) == 0.0  # still too thin alone
        assert state.pending_coin_growth["ladder2"] == pytest.approx(0.005)
        pl._grow_coin_reserve(state, "ladder2", 0.02, trader, filters, cfg)
        assert state.coin_reserve_qty["ladder2"] > 0  # combined $0.025 clears min_notional
        assert state.pending_coin_growth["ladder2"] == pytest.approx(0.0)


class TestSplitProfitFourWays:
    def test_equal_default_split(self):
        cfg = make_cfg()  # 0.25 each
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=0.01)
        pl._split_profit_four_ways(state, "ladder2", 1.0, trader, filters, cfg)
        assert state.reserved_total == pytest.approx(0.25)
        assert state.pending_reinvest_by_band[pl._band_key(0.04)] == pytest.approx(0.25)
        assert state.ladder_reserved["ladder2"] == pytest.approx(0.25)
        assert state.coin_reserve_qty.get("ladder2", 0.0) > 0 or state.pending_coin_growth.get("ladder2", 0.0) > 0

    def test_custom_ratios(self):
        cfg = make_cfg(accum_ratio=0.1, compound_ratio=0.4, ladder_reserve_ratio=0.3, coin_reserve_ratio=0.2)
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=0.01)
        pl._split_profit_four_ways(state, "ladder2", 1.0, trader, filters, cfg)
        assert state.reserved_total == pytest.approx(0.1)
        assert state.pending_reinvest_by_band[pl._band_key(0.04)] == pytest.approx(0.4)
        assert state.ladder_reserved["ladder2"] == pytest.approx(0.3)

    def test_accumulation_is_bot_wide_not_per_ladder(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=0.01)
        pl._split_profit_four_ways(state, "ladder1", 1.0, trader, filters, cfg)
        pl._split_profit_four_ways(state, "ladder2", 1.0, trader, filters, cfg)
        assert state.reserved_total == pytest.approx(0.5)  # 0.25 + 0.25, one shared total, not per-ladder

    def test_zero_profit_is_a_no_op(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=0.01)
        pl._split_profit_four_ways(state, "ladder2", 0.0, trader, filters, cfg)
        assert state.reserved_total == 0.0
        assert state.ladder_reserved == {}


class TestReplenishCoinReserve:
    def test_rebuys_from_principal(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=0.01)
        refilled = pl.replenish_coin_reserve(state, "ladder2", 1.0, trader, filters, cfg)
        assert refilled is True
        assert state.coin_reserve_qty["ladder2"] > 0
        assert state.coin_reserve_cost_basis["ladder2"] == pytest.approx(0.09 * 1.002)
        assert len(trader.placed_buys) == 1

    def test_zero_or_negative_principal_is_noop(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(), FakeFilters()
        assert pl.replenish_coin_reserve(state, "ladder2", 0.0, trader, filters, cfg) is False
        assert state.coin_reserve_qty == {}

    def test_too_thin_returns_false_without_buying(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09), FakeFilters(min_notional=1000.0)
        refilled = pl.replenish_coin_reserve(state, "ladder2", 1.0, trader, filters, cfg)
        assert refilled is False
        assert trader.placed_buys == []
        assert state.coin_reserve_qty == {}

    def test_buy_failure_returns_false(self):
        cfg = make_cfg()
        state = BotState()
        trader, filters = FakeTrader(price=0.09, fail_buy=True), FakeFilters(min_notional=0.01)
        refilled = pl.replenish_coin_reserve(state, "ladder2", 1.0, trader, filters, cfg)
        assert refilled is False
        assert state.coin_reserve_qty == {}


class TestMaybeFundSellFromReserve:
    def test_lists_reserve_when_ladder_has_zero_sells(self):
        cfg = make_cfg(taker_avoidance_buffer=0.0005)
        state = BotState(levels=[], coin_reserve_qty={"ladder1": 10.0},
                          coin_reserve_cost_basis={"ladder1": 0.085})
        trader, filters = FakeTrader(price=0.086), FakeFilters(min_notional=0.01)
        listed = pl.maybe_fund_sell_from_reserve(state, 0.086, trader, filters, cfg)
        assert listed == 1
        assert len(state.levels) == 1
        lvl = state.levels[0]
        assert lvl.state == "sell_open"
        assert lvl.from_coin_reserve is True
        assert lvl.coin_reserve_ladder == "ladder1"
        assert lvl.buy_price == 0.085
        assert lvl.qty == 10.0
        assert state.coin_reserve_qty["ladder1"] == 0.0
        assert len(trader.placed_sells) == 1

    def test_extends_sell_coverage_above_highest_when_ladder_already_has_a_live_sell(self):
        cfg = make_cfg()
        existing = GridLevel(index=1, buy_price=0.080, sell_price=0.080 * 1.02, base_qty=5.0,
                              qty=5.0, state="sell_open")  # ladder1's own 2% gap, highest sell so far
        state = BotState(levels=[existing], coin_reserve_qty={"ladder1": 10.0},
                          coin_reserve_cost_basis={"ladder1": 0.085})
        trader, filters = FakeTrader(price=0.086), FakeFilters(min_notional=0.01)
        listed = pl.maybe_fund_sell_from_reserve(state, 0.086, trader, filters, cfg)
        assert listed == 1
        assert len(state.levels) == 2  # existing untouched, new one added
        assert state.levels[0] is existing
        new_lvl = state.levels[1]
        assert new_lvl.sell_price > existing.sell_price  # strictly above the old highest
        assert state.coin_reserve_qty["ladder1"] == 0.0  # reserve consumed

    def test_noop_when_reserve_empty(self):
        cfg = make_cfg()
        state = BotState(levels=[], coin_reserve_qty={"ladder1": 0.0})
        trader, filters = FakeTrader(), FakeFilters()
        listed = pl.maybe_fund_sell_from_reserve(state, 0.086, trader, filters, cfg)
        assert listed == 0
        assert state.levels == []

    def test_placement_failure_leaves_reserve_untouched(self):
        cfg = make_cfg()
        state = BotState(levels=[], coin_reserve_qty={"ladder1": 10.0},
                          coin_reserve_cost_basis={"ladder1": 0.085})
        trader, filters = FakeTrader(price=0.086, fail_sell=True), FakeFilters(min_notional=0.01)
        listed = pl.maybe_fund_sell_from_reserve(state, 0.086, trader, filters, cfg)
        assert listed == 0
        assert state.levels == []
        assert state.coin_reserve_qty["ladder1"] == 10.0  # not lost

    def test_too_thin_to_clear_min_notional_leaves_reserve_untouched(self):
        cfg = make_cfg()
        state = BotState(levels=[], coin_reserve_qty={"ladder1": 0.01},
                          coin_reserve_cost_basis={"ladder1": 0.085})
        trader, filters = FakeTrader(price=0.086), FakeFilters(min_notional=1000.0)
        listed = pl.maybe_fund_sell_from_reserve(state, 0.086, trader, filters, cfg)
        assert listed == 0
        assert state.coin_reserve_qty["ladder1"] == 0.01


class TestLiquidateCoinReservePosition:
    def test_profit_splits_and_principal_replenishes(self):
        cfg = make_cfg()
        state = BotState()
        lvl = GridLevel(index=1, buy_price=0.085, sell_price=0.085 * 1.02, base_qty=10.0, qty=10.0,
                         state="sell_open", from_coin_reserve=True, coin_reserve_ladder="ladder1")
        profit = (lvl.sell_price - lvl.buy_price) * 10.0
        trader, filters = FakeTrader(price=0.086), FakeFilters(min_notional=0.01)
        refilled = pl.liquidate_coin_reserve_position(state, lvl, profit, trader, filters, cfg)
        assert refilled is True
        assert state.ladder_reserved["ladder1"] == pytest.approx(profit * 0.25)
        assert state.reserved_total == pytest.approx(profit * 0.25)
        assert state.total_profit_realized == pytest.approx(profit)
        assert state.coin_reserve_qty["ladder1"] > 0

    def test_replenishment_failure_sends_principal_to_pending_reinvest(self):
        cfg = make_cfg()
        state = BotState()
        lvl = GridLevel(index=1, buy_price=0.085, sell_price=0.085 * 1.02, base_qty=10.0, qty=10.0,
                         state="sell_open", from_coin_reserve=True, coin_reserve_ladder="ladder1")
        profit = (lvl.sell_price - lvl.buy_price) * 10.0
        principal = 0.085 * 10.0
        trader, filters = FakeTrader(price=0.086, fail_buy=True), FakeFilters(min_notional=0.01)
        refilled = pl.liquidate_coin_reserve_position(state, lvl, profit, trader, filters, cfg)
        assert refilled is False
        assert state.coin_reserve_qty == {}
        expected_band = state.pending_reinvest_by_band[pl._band_key(0.02)]
        assert expected_band == pytest.approx(profit * 0.25 + principal)

    def test_ladder_identity_comes_from_the_level_not_the_gap(self):
        cfg = make_cfg()
        state = BotState()
        lvl = GridLevel(index=1, buy_price=0.08, sell_price=0.10, base_qty=10.0, qty=10.0,
                         state="sell_open", from_coin_reserve=True, coin_reserve_ladder="ladder1")
        profit = (0.10 - 0.08) * 10.0
        trader, filters = FakeTrader(price=0.099), FakeFilters(min_notional=0.01)
        pl.liquidate_coin_reserve_position(state, lvl, profit, trader, filters, cfg)
        assert state.ladder_reserved.get("ladder1", 0.0) == pytest.approx(profit * 0.25)
        assert state.ladder_reserved.get("ladder2", 0.0) == 0.0
