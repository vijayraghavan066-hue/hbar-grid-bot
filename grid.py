from dataclasses import dataclass
from typing import Optional


@dataclass
class GridLevel:
    index: int
    buy_price: float
    sell_price: float
    base_qty: float
    extra_qty: float = 0.0  # grown over time by reinvested profit, in DASH
    # state: "idle" (no order), "buy_open", "sell_open", "closed" (one_shot, done for good)
    state: str = "idle"
    buy_order_id: Optional[str] = None
    sell_order_id: Optional[str] = None
    qty: float = 0.0  # actual qty of the currently open/filled order
    one_shot: bool = False  # bulk-ladder levels: never re-arm a buy after selling
    consolidated: bool = False  # already merged into a coarse far-from-market level

    def target_qty(self) -> float:
        return self.base_qty + self.extra_qty

    def to_dict(self):
        return {
            "index": self.index,
            "buy_price": self.buy_price,
            "sell_price": self.sell_price,
            "base_qty": self.base_qty,
            "extra_qty": self.extra_qty,
            "state": self.state,
            "buy_order_id": self.buy_order_id,
            "sell_order_id": self.sell_order_id,
            "qty": self.qty,
            "one_shot": self.one_shot,
            "consolidated": self.consolidated,
        }

    @staticmethod
    def from_dict(d):
        return GridLevel(**d)


def build_arithmetic_levels(lower: float, upper: float, spacing: float,
                             sell_offset: float, base_qty: float) -> list[GridLevel]:
    """Grid lines every `spacing` dollars from `lower` to `upper`. Each line
    buys at its own price and sells `sell_offset` dollars above — lines
    overlap in range when sell_offset > spacing, which is intentional here."""
    levels = []
    n_lines = int(round((upper - lower) / spacing)) + 1
    for i in range(n_lines):
        buy_price = round(lower + i * spacing, 10)
        levels.append(
            GridLevel(
                index=i,
                buy_price=buy_price,
                sell_price=round(buy_price + sell_offset, 10),
                base_qty=base_qty,
            )
        )
    return levels


def build_zone_price_points(anchor_low: float, anchor_high: float, anchor_step: float,
                             dense_low: float, dense_high: float, dense_step: float) -> list[float]:
    """Sparse 'anchor' points below and above a densely-spaced middle zone.
    e.g. anchor_low=10, dense_low=45, dense_high=65, anchor_high=100 with
    anchor_step=5, dense_step=1 gives: 10,15,...,40, 45,46,...,65, 70,...,100."""
    points = []
    p = anchor_low
    while p < dense_low - 1e-9:
        points.append(round(p, 10))
        p += anchor_step
    p = dense_low
    while p < dense_high + 1e-9:
        points.append(round(p, 10))
        p += dense_step
    p = dense_high + anchor_step  # dense loop already emitted dense_high itself
    while p <= anchor_high + 1e-9:
        points.append(round(p, 10))
        p += anchor_step
    return points


def build_hybrid_zoned_levels(anchor_low: float, dense_low: float, dense_high: float,
                               anchor_high: float, anchor_step: float, dense_step: float,
                               dense_sell_offset: float, total_capital: float,
                               reference_price: float, dca_exponent: float = 1.0) -> list[GridLevel]:
    """Sparse 'anchor' levels below and above a fine-grained dense zone.

    Anchor levels (both zones) chain to the next point within their own
    zone — wide, non-overlapping profit targets, and the low-anchor zone's
    last level bridges straight into dense_low. Dense-zone levels are each
    independent and always sell `dense_sell_offset` above their own buy
    price, deliberately overlapping their neighbors (e.g. $0.10 spacing
    with a $1.00 sell target) for tight, frequent profit-taking near the
    current price. This can easily produce 100+ levels in the dense zone
    alone, so pair it with the order-count window in the main loop.

    Sizing: same DCA-weighted normalization as build_zoned_levels — total
    capital across every level, worst case, never exceeds `total_capital`.
    """
    raw_levels = []  # list of (buy_price, sell_price)

    # Low anchor zone: chained, bridging into dense_low.
    points = []
    p = anchor_low
    while p < dense_low - 1e-9:
        points.append(round(p, 10))
        p += anchor_step
    points.append(round(dense_low, 10))
    for i in range(len(points) - 1):
        raw_levels.append((points[i], points[i + 1]))

    # Dense zone: independent, fixed sell offset, deliberately overlapping.
    p = dense_low
    while p <= dense_high + 1e-9:
        buy_price = round(p, 10)
        raw_levels.append((buy_price, round(buy_price + dense_sell_offset, 10)))
        p += dense_step

    # High anchor zone: chained, starting fresh above dense_high.
    points = []
    p = dense_high + anchor_step
    while p <= anchor_high + 1e-9:
        points.append(round(p, 10))
        p += anchor_step
    for i in range(len(points) - 1):
        raw_levels.append((points[i], points[i + 1]))

    weights = [(reference_price / buy) ** dca_exponent for buy, _ in raw_levels]
    total_weight = sum(weights)

    levels = []
    for i, (buy_price, sell_price) in enumerate(raw_levels):
        usdt_alloc = total_capital * weights[i] / total_weight
        qty = usdt_alloc / buy_price
        levels.append(GridLevel(index=i, buy_price=buy_price, sell_price=sell_price, base_qty=qty))
    return levels


def build_bulk_ladder_levels(buy_price: float, sell_low: float, sell_high: float,
                              sell_step: float, total_capital: float, start_index: int = 0) -> list[GridLevel]:
    """A one-time bulk position, sold off in equal slices at fine-spaced
    targets above it. Every level shares the same `buy_price` (they're all
    expected to fill together, right at current price) but each has its own
    higher `sell_price`. Marked `one_shot`: once a slice sells, it's closed
    for good — never re-arms a new buy, so the bot never chases price
    upward buying more as it climbs. Equal capital split across slices."""
    rungs = []
    p = sell_low
    while p <= sell_high + 1e-9:
        rungs.append(round(p, 10))
        p += sell_step

    usdt_per_slice = total_capital / len(rungs)
    qty_per_slice = usdt_per_slice / buy_price

    levels = []
    for i, sell_price in enumerate(rungs):
        levels.append(
            GridLevel(
                index=start_index + i,
                buy_price=buy_price,
                sell_price=sell_price,
                base_qty=qty_per_slice,
                one_shot=True,
            )
        )
    return levels


def build_zoned_levels(price_points: list[float], total_capital: float,
                        reference_price: float, dca_exponent: float = 1.0) -> list[GridLevel]:
    """Each consecutive pair of points forms one level: buy at the lower
    point, sell at the next point up — so anchor-zone levels naturally get
    wide profit targets and dense-zone levels get narrow ones.

    Capital per level is weighted by (reference_price / buy_price) **
    dca_exponent, so cheaper levels get a bigger USDT allocation (classic
    DCA: buy more the further price has dropped). Weights are normalized so
    the sum of every level's USDT allocation exactly equals total_capital —
    the worst case (every single level filled at once) never exceeds budget.
    """
    points = sorted(price_points)
    buy_prices = points[:-1]
    weights = [(reference_price / p) ** dca_exponent for p in buy_prices]
    total_weight = sum(weights)

    levels = []
    for i, buy_price in enumerate(buy_prices):
        usdt_alloc = total_capital * weights[i] / total_weight
        qty = usdt_alloc / buy_price
        levels.append(
            GridLevel(
                index=i,
                buy_price=buy_price,
                sell_price=points[i + 1],
                base_qty=qty,
            )
        )
    return levels
