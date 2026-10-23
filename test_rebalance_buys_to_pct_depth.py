"""Offline scenarios for rebalance_buys_to_pct_depth.py (pytest)."""
import math

from grid import GridLevel
from state import BotState
import rebalance_buys_to_pct_depth as rb


class Filters:
    def __init__(self, tick=0.001, step=0.01, min_notional=1.0):
        self.tick_size, self.step_size, self.min_notional = tick, step, min_notional
        self._pd = max(0, -int(math.floor(math.log10(tick))))
        self._qd = max(0, -int(math.floor(math.log10(step))))

    def round_price(self, p):
        return round(math.floor(round(p / self.tick_size, 6) + 1e-9) * self.tick_size, self._pd)

    def round_qty(self, q):
        return round(math.floor(round(q / self.step_size, 6) + 1e-9) * self.step_size, self._qd)


class Cfg:
    anchor_low = 2.5
    reference_price = 7.0
    dca_exponent = 1.0
    max_open_orders = 190


SPECS = [(0.01, 0.01), (0.02, 0.02), (0.03, 0.03), (0.04, 0.04), (0.05, 0.05)]
PRICE = 7.17
F = Filters()


def buy(idx, price, qty, offset, state="buy_open", oid=None):
    return GridLevel(index=idx, buy_price=price, sell_price=round(price * (1 + offset), 10), base_qty=qty,
                     qty=qty if state == "buy_open" else 0.0, state=state,
                     buy_order_id=oid if oid else (f"b{idx}" if state == "buy_open" else None), lifetime_profit=0.3)


def held(idx, bprice, qty, offset):
    return GridLevel(index=idx, buy_price=bprice, sell_price=round(bprice * (1 + offset), 10), base_qty=qty,
                     qty=qty, state="sell_open", sell_order_id=f"s{idx}")


class FakeTrader:
    def __init__(self, fail=()):
        self.fail, self.cancelled = set(fail), []

    def cancel_order(self, oid):
        if oid in self.fail:
            raise RuntimeError("already filled")
        self.cancelled.append(oid)


class TestPlanLadder:
    def test_rungs_on_geometric_lattice_from_anchor_low_and_under_price(self):
        p = rb.plan_ladder(60.0, PRICE, 0.02, 2.5, [], F, 7.0, 1.0)
        assert p["top"] < PRICE and p["floor"] >= 2.5
        for (a, _), (b, _) in zip(p["points"], p["points"][1:]):
            assert 1.01 < b / a < 1.03

    def test_spends_no_more_than_capital_and_meets_minimum(self):
        p = rb.plan_ladder(60.0, PRICE, 0.02, 2.5, [], F, 7.0, 1.0)
        assert p["spent"] <= 60.0 + 1e-9
        assert all(q * pr >= 1.0 for pr, q in p["points"])

    def test_more_capital_reaches_deeper(self):
        a = rb.plan_ladder(20.0, PRICE, 0.02, 2.5, [], F, 7.0, 1.0)
        b = rb.plan_ladder(60.0, PRICE, 0.02, 2.5, [], F, 7.0, 1.0)
        assert b["floor"] < a["floor"] and len(b["points"]) > len(a["points"])

    def test_floor_is_anchor_low(self):
        p = rb.plan_ladder(5000.0, PRICE, 0.05, 2.5, [], F, 7.0, 1.0)
        assert p["floor"] >= 2.5 and p["clipped"]

    def test_held_position_blocks_rungs_at_and_above_its_buy(self):
        p = rb.plan_ladder(60.0, PRICE, 0.02, 2.5, [6.9], F, 7.0, 1.0)
        assert p["top"] <= 6.9 / 1.02 + 1e-6

    def test_max_rungs_trims_and_remainder_still_spent(self):
        p = rb.plan_ladder(60.0, PRICE, 0.02, 2.5, [], F, 7.0, 1.0, max_rungs=10)
        assert len(p["points"]) == 10 and p["spent"] > 55.0

    def test_skip_prices_left_alone(self):
        full = rb.plan_ladder(60.0, PRICE, 0.02, 2.5, [], F, 7.0, 1.0)
        taken = full["points"][-1][0]
        p = rb.plan_ladder(60.0, PRICE, 0.02, 2.5, [], F, 7.0, 1.0, skip_prices=[taken])
        assert taken not in [x[0] for x in p["points"]]

    def test_capital_too_small_lays_nothing(self):
        assert rb.plan_ladder(0.5, PRICE, 0.02, 2.5, [], F, 7.0, 1.0)["points"] == []


class TestBudgetAndAll:
    def test_allocate_budget_no_trim_when_it_fits(self):
        assert rb.allocate_budget([10, 10], [50.0, 50.0], 30) == [None, None]

    def test_allocate_budget_trims_in_proportion_to_capital(self):
        assert rb.allocate_budget([40, 20], [80.0, 40.0], 30) == [20, 10]

    def test_plan_all_respects_order_budget(self):
        cfg = Cfg()
        cfg.max_open_orders = 60
        levels = [buy(i, round(6.0 - i * 0.05, 3), 4.0, 0.02, oid=f"b{i}") for i in range(20)]
        levels += [held(100 + i, 7.5 + i * 0.1, 1.0, 0.02) for i in range(10)]
        plans, budget = rb.plan_all(levels, PRICE, SPECS, cfg, F)
        assert budget == 60 - 10 - rb.MARGIN
        assert sum(len(p["points"]) for p in plans.values()) <= budget
        assert rb.verify(plans, cfg, F) == []

    def test_other_buys_outside_ladders_reduce_budget(self):
        cfg = Cfg()
        stray = GridLevel(index=900, buy_price=6.0, sell_price=6.5, base_qty=2.0, qty=2.0,
                          state="buy_open", buy_order_id="x")
        _, budget = rb.plan_all([buy(1, 6.0, 4.0, 0.02), stray], PRICE, SPECS, cfg, F)
        assert budget == cfg.max_open_orders - rb.MARGIN - 1


class TestApply:
    def make_state(self):
        levels = [buy(1, 6.9, 5.0, 0.02, oid="B1"), buy(2, 6.8, 5.0, 0.02, oid="B2"),
                  buy(3, 6.0, 4.0, 0.02, state="idle"), buy(4, 6.5, 8.0, 0.05, oid="B4"),
                  held(10, 7.0, 2.0, 0.02), held(11, 7.2, 1.0, 0.03)]
        bulk = buy(20, 3.0, 5.0, 0.02)
        bulk.one_shot = True
        levels.append(bulk)
        return BotState(levels=levels, next_level_index=500)

    def test_cancels_only_own_live_buys_and_leaves_held_and_bulk(self):
        st = self.make_state()
        tr = FakeTrader()
        new, fails = rb.apply_plan(st, PRICE, SPECS, Cfg(), F, tr)
        assert fails == [] and sorted(tr.cancelled) == ["B1", "B2", "B4"]
        assert any(l.index == 10 for l in st.levels) and any(l.index == 11 for l in st.levels)
        assert any(l.index == 20 for l in st.levels)
        assert all(l.state == "idle" for l in new)
        assert min(l.index for l in new) == 500 and st.next_level_index == 500 + len(new)

    def test_new_levels_match_ladder_offsets(self):
        st = self.make_state()
        new, _ = rb.apply_plan(st, PRICE, SPECS, Cfg(), F, FakeTrader())
        for l in new:
            gap = l.sell_price / l.buy_price - 1.0
            assert min(abs(gap - o) for _, o in SPECS) < 1e-9

    def test_capital_not_increased(self):
        st = self.make_state()
        before = sum(rb.level_capital(l) for l in st.levels
                     if l.state in ("buy_open", "idle") and not l.one_shot)
        new, _ = rb.apply_plan(st, PRICE, SPECS, Cfg(), F, FakeTrader())
        assert sum(rb.level_capital(l) for l in new) <= before + 1e-6

    def test_cancel_failure_keeps_level_and_its_price_is_not_reused(self):
        st = self.make_state()
        kept = next(l for l in st.levels if l.index == 1)
        new, fails = rb.apply_plan(st, PRICE, SPECS, Cfg(), F, FakeTrader(fail={"B1"}))
        assert len(fails) == 1 and kept in st.levels and kept.buy_order_id == "B1"
        assert 6.9 not in [l.buy_price for l in new]

    def test_lifetime_profit_conserved(self):
        st = self.make_state()
        own_profit = sum(l.lifetime_profit for l in st.levels
                         if l.state in ("buy_open", "idle") and not l.one_shot)
        new, _ = rb.apply_plan(st, PRICE, SPECS, Cfg(), F, FakeTrader())
        assert abs(sum(l.lifetime_profit for l in new) - own_profit) < 1e-6
