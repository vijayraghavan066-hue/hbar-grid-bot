import logging
import math
import uuid
from decimal import Decimal

from binance.client import Client

log = logging.getLogger("dash-grid-bot")


def _decimals_from_str(value_str: str) -> int:
    """Binance.US gives filter sizes as decimal strings like '0.01000000'.
    Derive the exact number of decimal places from the string itself,
    rather than from the float, since float conversion is exactly what
    causes '59.800000000000004'-style representation errors later."""
    d = Decimal(value_str).normalize()
    exponent = d.as_tuple().exponent
    return max(0, -exponent)


class SymbolFilters:
    def __init__(self, tick_size: float, step_size: float, min_notional: float,
                 price_decimals: int, qty_decimals: int):
        self.tick_size = tick_size
        self.step_size = step_size
        self.min_notional = min_notional
        self.price_decimals = price_decimals
        self.qty_decimals = qty_decimals

    def round_price(self, price: float) -> float:
        # The floor-then-multiply step can itself reintroduce binary-float
        # noise (e.g. land on 60.300000000000004 instead of 60.3), so a
        # plain decimal round back to the exchange's own decimal precision
        # cleans that up. This is what we store in our own bookkeeping
        # (lvl.buy_price etc.), not just what we send to the API.
        # price/tick can land a hair under a whole number (1.47/0.0001 =
        # 14699.999999999998) and floor() would then drop a full tick — round
        # away the float noise first so an exact price stays exact.
        return round(math.floor(round(price / self.tick_size, 6) + 1e-9) * self.tick_size, self.price_decimals)

    def round_qty(self, qty: float) -> float:
        return round(math.floor(round(qty / self.step_size, 6) + 1e-9) * self.step_size, self.qty_decimals)

    def format_price(self, price: float) -> str:
        return f"{self.round_price(price):.{self.price_decimals}f}"

    def format_qty(self, qty: float) -> str:
        return f"{self.round_qty(qty):.{self.qty_decimals}f}"


class BinanceUSTrader:
    """Thin wrapper around python-binance for Binance.US, with a dry-run mode
    that simulates order placement/fills without hitting the account."""

    def __init__(self, api_key: str, api_secret: str, symbol: str, dry_run: bool):
        self.symbol = symbol
        self.dry_run = dry_run
        self._client = None
        if not dry_run:
            self._client = Client(api_key, api_secret, tld="us")
        else:
            # Public endpoints (price lookups) don't need real keys, but the
            # python-binance client still requires a client for tld routing.
            self._client = Client(api_key or "dryrun", api_secret or "dryrun", tld="us")
        self._sim_orders = {}  # order_id -> dict, dry-run only
        self.filters = None

    def get_symbol_filters(self) -> SymbolFilters:
        info = self._client.get_symbol_info(self.symbol)
        if info is None:
            raise RuntimeError(f"Symbol {self.symbol} not found on Binance.US")
        tick_size = step_size = min_notional = None
        price_decimals = qty_decimals = 8
        for f in info["filters"]:
            if f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
                price_decimals = _decimals_from_str(f["tickSize"])
            elif f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
                qty_decimals = _decimals_from_str(f["stepSize"])
            elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = float(f.get("minNotional", f.get("minNotional", 0)) or 0)
        if tick_size is None or step_size is None:
            raise RuntimeError(f"Could not read PRICE_FILTER/LOT_SIZE for {self.symbol}")
        self.filters = SymbolFilters(tick_size, step_size, min_notional or 0.0,
                                      price_decimals, qty_decimals)
        return self.filters

    def get_price(self) -> float:
        ticker = self._client.get_symbol_ticker(symbol=self.symbol)
        return float(ticker["price"])

    def place_limit_buy(self, price: float, qty: float) -> str:
        if self.dry_run:
            order_id = f"SIM-BUY-{uuid.uuid4().hex[:10]}"
            self._sim_orders[order_id] = {
                "side": "BUY", "price": price, "qty": qty, "status": "NEW",
            }
            log.info(f"[DRY RUN] placed BUY {qty} {self.symbol} @ {price} -> {order_id}")
            return order_id
        price_str = self.filters.format_price(price)
        qty_str = self.filters.format_qty(qty)
        order = self._client.order_limit_buy(symbol=self.symbol, quantity=qty_str, price=price_str)
        return str(order["orderId"])

    def place_limit_sell(self, price: float, qty: float) -> str:
        if self.dry_run:
            order_id = f"SIM-SELL-{uuid.uuid4().hex[:10]}"
            self._sim_orders[order_id] = {
                "side": "SELL", "price": price, "qty": qty, "status": "NEW",
            }
            log.info(f"[DRY RUN] placed SELL {qty} {self.symbol} @ {price} -> {order_id}")
            return order_id
        price_str = self.filters.format_price(price)
        qty_str = self.filters.format_qty(qty)
        order = self._client.order_limit_sell(symbol=self.symbol, quantity=qty_str, price=price_str)
        return str(order["orderId"])

    def rehydrate_from_levels(self, levels):
        """Dry-run only: the simulated order book lives in memory and is lost
        on process restart. When resuming unchanged state after a crash/
        reboot, repopulate it from the persisted grid levels so is_filled()
        keeps recognizing their still-open orders."""
        if not self.dry_run:
            return
        for lvl in levels:
            if lvl.state == "buy_open" and lvl.buy_order_id:
                self._sim_orders[lvl.buy_order_id] = {
                    "side": "BUY", "price": lvl.buy_price, "qty": lvl.qty, "status": "NEW",
                }
            elif lvl.state == "sell_open" and lvl.sell_order_id:
                self._sim_orders[lvl.sell_order_id] = {
                    "side": "SELL", "price": lvl.sell_price, "qty": lvl.qty, "status": "NEW",
                }

    def cancel_order(self, order_id: str):
        if self.dry_run:
            self._sim_orders.pop(order_id, None)
            return
        self._client.cancel_order(symbol=self.symbol, orderId=int(order_id))

    def is_filled(self, order_id: str, current_price: float) -> bool:
        """`current_price` is the price already fetched once per poll cycle —
        passed in rather than re-fetched per order to avoid one API call per
        grid level (there can be hundreds)."""
        if self.dry_run:
            sim = self._sim_orders.get(order_id)
            if sim is None:
                return False
            if sim["side"] == "BUY" and current_price <= sim["price"]:
                sim["status"] = "FILLED"
            elif sim["side"] == "SELL" and current_price >= sim["price"]:
                sim["status"] = "FILLED"
            return sim["status"] == "FILLED"
        order = self._client.get_order(symbol=self.symbol, orderId=int(order_id))
        return order["status"] == "FILLED"
