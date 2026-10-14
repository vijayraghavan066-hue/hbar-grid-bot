"""Offline scenarios for inject_capital.py's ladder-skewed allocation (added
2026-10-14): Ladder 1 gets a fixed 10% floor (half of an unweighted 20%
share), the remaining 90% splits evenly across Ladders 2-5 (22.5% each),
and within each ladder the existing DCA-weighted formula still decides who
gets what. Run directly: python test_inject_capital.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

from grid import GridLevel
from inject_capital import _compute_shares, LADDER_WEIGHT, DEFAULT_WEIGHT


def make_cfg(**overrides):
    base = dict(
        ladder1_step_pct=0.01, ladder1_offset_pct=0.01, ladder1_window_pct=0.05,
        ladder2_step_pct=0.02, ladder2_offset_pct=0.02, ladder2_window_pct=0.10,
        ladder3_step_pct=0.03, ladder3_offset_pct=0.03, ladder3_window_pct=0.15,
        ladder4_step_pct=0.04, ladder4_offset_pct=0.04, ladder4_window_pct=0.20,
        ladder5_step_pct=0.05, ladder5_offset_pct=0.05, ladder5_window_pct=0.25,
        reference_price=1.40, dca_exponent=1.0,
    )
    base.update(overrides)
    return type("Cfg", (), base)()


def ladder_level(index, buy_price, offset_pct):
    return GridLevel(index=index, buy_price=buy_price, sell_price=round(buy_price * (1.0 + offset_pct), 10),
                      base_qty=1.0, state="idle")


class TestComputeShares:
    def test_weights_sum_to_the_full_amount(self):
        cfg = make_cfg()
        levels = [ladder_level(i, 1.40, pct) for i, pct in enumerate([0.01, 0.02, 0.03, 0.04, 0.05])]
        shares = _compute_shares(levels, 100.0, cfg)
        assert sum(s for _, s in shares) == pytest.approx(100.0)

    def test_ladder1_gets_the_10pct_floor_when_all_five_ladders_present(self):
        cfg = make_cfg()
        levels = [ladder_level(i, 1.40, pct) for i, pct in enumerate([0.01, 0.02, 0.03, 0.04, 0.05])]
        shares = _compute_shares(levels, 100.0, cfg)
        ladder1_share = next(s for lvl, s in shares if lvl.index == 0)
        assert ladder1_share == pytest.approx(10.0)  # 10% of 100

    def test_ladders_2_through_5_split_the_remaining_90pct_evenly(self):
        cfg = make_cfg()
        # One level per ladder, all at the SAME price -- isolates the ladder
        # skew from the DCA-within-ladder weighting (which would otherwise
        # also vary by price).
        levels = [ladder_level(i, 1.40, pct) for i, pct in enumerate([0.01, 0.02, 0.03, 0.04, 0.05])]
        shares = _compute_shares(levels, 100.0, cfg)
        by_index = {lvl.index: s for lvl, s in shares}
        assert by_index[1] == pytest.approx(22.5)  # ladder2
        assert by_index[2] == pytest.approx(22.5)  # ladder3
        assert by_index[3] == pytest.approx(22.5)  # ladder4
        assert by_index[4] == pytest.approx(22.5)  # ladder5

    def test_ladder1_gets_noticeably_less_per_level_than_wider_ladders(self):
        # The actual point of this feature: at equal price and equal level
        # count, Ladder 1's own levels receive meaningfully less than
        # Ladder 5's, not an even split.
        cfg = make_cfg()
        levels = [ladder_level(0, 1.40, 0.01), ladder_level(1, 1.40, 0.05)]
        shares = _compute_shares(levels, 100.0, cfg)
        by_index = {lvl.index: s for lvl, s in shares}
        assert by_index[0] < by_index[1]
        assert by_index[0] == pytest.approx(100.0 * LADDER_WEIGHT["ladder1"] /
                                             (LADDER_WEIGHT["ladder1"] + LADDER_WEIGHT["ladder5"]))

    def test_missing_ladder_bucket_renormalizes_instead_of_losing_money(self):
        # Only ladder1 and ladder3 have eligible levels right now (e.g. the
        # others are all sell_open) -- the money must still land entirely
        # on these two, re-weighted between just them, not silently dropped.
        cfg = make_cfg()
        levels = [ladder_level(0, 1.40, 0.01), ladder_level(1, 1.40, 0.03)]
        shares = _compute_shares(levels, 100.0, cfg)
        assert sum(s for _, s in shares) == pytest.approx(100.0)
        by_index = {lvl.index: s for lvl, s in shares}
        expected_total = LADDER_WEIGHT["ladder1"] + LADDER_WEIGHT["ladder3"]
        assert by_index[0] == pytest.approx(100.0 * LADDER_WEIGHT["ladder1"] / expected_total)
        assert by_index[1] == pytest.approx(100.0 * LADDER_WEIGHT["ladder3"] / expected_total)

    def test_non_ladder_level_falls_back_to_default_weight_not_dropped(self):
        # A legacy/bulk position whose gap matches none of the 5 ladders
        # exactly still gets capital -- at the unweighted 20% a normal
        # ladder would get, grouped into its own "other" bucket.
        cfg = make_cfg()
        legacy = GridLevel(index=0, buy_price=1.40, sell_price=1.40 * 1.075, base_qty=1.0, state="idle")  # 7.5%, no ladder matches
        ladder1 = ladder_level(1, 1.40, 0.01)
        shares = _compute_shares([legacy, ladder1], 100.0, cfg)
        assert sum(s for _, s in shares) == pytest.approx(100.0)
        by_index = {lvl.index: s for lvl, s in shares}
        expected_total = DEFAULT_WEIGHT + LADDER_WEIGHT["ladder1"]
        assert by_index[0] == pytest.approx(100.0 * DEFAULT_WEIGHT / expected_total)

    def test_dca_weighting_still_applies_within_a_single_ladder(self):
        # Two levels in the SAME ladder (ladder5) at different prices --
        # the cheaper one should still get a bigger share of ladder5's own
        # pool, same DCA formula as before this feature existed.
        cfg = make_cfg(reference_price=1.40)
        cheap = ladder_level(0, 1.00, 0.05)
        expensive = ladder_level(1, 2.00, 0.05)
        shares = _compute_shares([cheap, expensive], 100.0, cfg)
        by_index = {lvl.index: s for lvl, s in shares}
        assert by_index[0] > by_index[1]
        assert by_index[0] + by_index[1] == pytest.approx(100.0)  # all of ladder5's 100% (only ladder present)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
