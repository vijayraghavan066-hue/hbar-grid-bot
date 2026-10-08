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
        reserved_draw_floor_pct=0.5, reserved_topup_threshold_usd=20.0,
        reference_price=0.09, dca_exponent=1.0, profit_reserve_ratio=0.5,
        pct_ladder_enabled=True,
    )
    base.update(overrides)
    return type("Cfg", (), base)()


class FakeTrader:
    def __init__(self, fail_cancel_ids=None):
        self.cancelled = []
        self.fail_cancel_ids = fail_cancel_ids or set()

    def cancel_order(self, order_id):
        if order_id in self.fail_cancel_ids:
            raise RuntimeError("already filled")
        self.cancelled.append(order_id)


class FakeFilters:
    def __init__(self, min_notional=0.01):
        self.min_notional = min_notional

    def round_qty(self, qty):
        return round(qty, 6)


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


class TestNearestLadderName:
    def test_exact_match(self):
        cfg = make_cfg()
        assert pl.nearest_ladder_name(0.04, cfg) == "ladder2"

    def test_legacy_offset_maps_to_closest(self):
        # HBAR's old DENSE_SELL_OFFSET/DENSE_LOW ~ 4.6% -- closer to
        # ladder2 (4%) than ladder1 (2%) or ladder3 (6%)
        cfg = make_cfg()
        assert pl.nearest_ladder_name(0.046, cfg) == "ladder2"

    def test_boundary_rounds_to_nearer_side(self):
        cfg = make_cfg()
        assert pl.nearest_ladder_name(0.029, cfg) == "ladder1"  # closer to 2% than 4%
        assert pl.nearest_ladder_name(0.031, cfg) == "ladder2"  # closer to 4% than 2%


class TestDistributeProfitLadderAware:
    def test_splits_and_routes_to_owning_ladder(self):
        cfg = make_cfg()
        state = BotState()
        name = pl.distribute_profit_ladder_aware(state, profit=1.0, sold_offset_pct=0.02, cfg=cfg)
        assert name == "ladder1"
        assert state.ladder_reserved["ladder1"] == pytest.approx(0.5)
        assert state.ladder_reserved_contributed["ladder1"] == pytest.approx(0.5)
        assert state.pending_reinvest_by_band[pl._band_key(0.02)] == pytest.approx(0.5)
        assert state.total_profit_realized == pytest.approx(1.0)

    def test_other_ladders_untouched(self):
        cfg = make_cfg()
        state = BotState()
        pl.distribute_profit_ladder_aware(state, profit=1.0, sold_offset_pct=0.02, cfg=cfg)
        assert state.ladder_reserved.get("ladder2", 0.0) == 0.0
        assert state.ladder_reserved.get("ladder3", 0.0) == 0.0

    def test_zero_or_negative_profit_is_noop(self):
        cfg = make_cfg()
        state = BotState()
        pl.distribute_profit_ladder_aware(state, profit=0.0, sold_offset_pct=0.02, cfg=cfg)
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
