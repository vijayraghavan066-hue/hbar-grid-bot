import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _get(name, default=None, required=False):
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def _get_bool(name, default):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    api_key: str
    api_secret: str
    symbol: str
    grid_mode: str  # "hybrid" or "pilot"
    pilot_points: list
    anchor_low: float
    anchor_high: float
    anchor_step: float
    dense_low: float
    dense_high: float
    dense_step: float
    dense_sell_offset: float
    total_capital_usdt: float
    reference_price: float
    dca_exponent: float
    bulk_amount_usdt: float
    bulk_sell_high: float
    bulk_sell_step: float
    max_open_orders: int
    taker_avoidance_buffer: float
    zone_shift_enabled: bool
    zone_shift_interval_hours: float
    near_market_band_pct: float
    consolidation_step: float
    circuit_breaker_enabled: bool
    circuit_breaker_drop_pct: float
    circuit_breaker_window_hours: float
    circuit_breaker_resume_pct: float
    dense_window_enabled: bool
    dense_window_half_width: float
    dense_window_snap: float
    dry_run: bool
    profit_reserve_ratio: float
    reinvest_band_pct: float
    pending_reinvest_max_hours: float
    poll_interval_seconds: int
    state_file: str
    log_file: str
    log_level: str
    heartbeat_file: str
    ledger_file: str


def load_config() -> Config:
    cfg = Config(
        api_key=_get("BINANCE_US_API_KEY", required=True) if not _get_bool("DRY_RUN", True) else _get("BINANCE_US_API_KEY", default=""),
        api_secret=_get("BINANCE_US_API_SECRET", required=True) if not _get_bool("DRY_RUN", True) else _get("BINANCE_US_API_SECRET", default=""),
        symbol=_get("SYMBOL", "DASHUSDT"),
        grid_mode=_get("GRID_MODE", "hybrid").lower(),
        pilot_points=[float(x) for x in _get("PILOT_POINTS", "").split(",") if x.strip()],
        anchor_low=float(_get("ANCHOR_LOW", "10.0")),
        anchor_high=float(_get("ANCHOR_HIGH", "100.0")),
        anchor_step=float(_get("ANCHOR_STEP", "5.0")),
        dense_low=float(_get("DENSE_LOW", "45.0")),
        dense_high=float(_get("DENSE_HIGH", "65.0")),
        dense_step=float(_get("DENSE_STEP", "0.1")),
        dense_sell_offset=float(_get("DENSE_SELL_OFFSET", "1.0")),
        total_capital_usdt=float(_get("TOTAL_CAPITAL_USDT", "2518.64")),
        reference_price=float(_get("REFERENCE_PRICE", "55.0")),
        dca_exponent=float(_get("DCA_EXPONENT", "1.0")),
        bulk_amount_usdt=float(_get("BULK_AMOUNT_USDT", "0")),
        bulk_sell_high=float(_get("BULK_SELL_HIGH", "100.0")),
        bulk_sell_step=float(_get("BULK_SELL_STEP", "0.25")),
        max_open_orders=int(_get("MAX_OPEN_ORDERS", "180")),
        taker_avoidance_buffer=float(_get("TAKER_AVOIDANCE_BUFFER", "0.30")),
        zone_shift_enabled=_get_bool("ZONE_SHIFT_ENABLED", True),
        zone_shift_interval_hours=float(_get("ZONE_SHIFT_INTERVAL_HOURS", "2")),
        near_market_band_pct=float(_get("NEAR_MARKET_BAND_PCT", "0.10")),
        consolidation_step=float(_get("CONSOLIDATION_STEP", "1.0")),
        circuit_breaker_enabled=_get_bool("CIRCUIT_BREAKER_ENABLED", False),
        circuit_breaker_drop_pct=float(_get("CIRCUIT_BREAKER_DROP_PCT", "0.15")),
        circuit_breaker_window_hours=float(_get("CIRCUIT_BREAKER_WINDOW_HOURS", "6")),
        circuit_breaker_resume_pct=float(_get("CIRCUIT_BREAKER_RESUME_PCT", "0.05")),
        dense_window_enabled=_get_bool("DENSE_WINDOW_ENABLED", False),
        dense_window_half_width=float(_get("DENSE_WINDOW_HALF_WIDTH", "0.35")),
        dense_window_snap=float(_get("DENSE_WINDOW_SNAP", "0.05")),
        dry_run=_get_bool("DRY_RUN", True),
        profit_reserve_ratio=float(_get("PROFIT_RESERVE_RATIO", "0.5")),
        reinvest_band_pct=float(_get("REINVEST_BAND_PCT", "0.04")),
        pending_reinvest_max_hours=float(_get("PENDING_REINVEST_MAX_HOURS", "24")),
        poll_interval_seconds=int(_get("POLL_INTERVAL_SECONDS", "300")),
        state_file=_get("STATE_FILE", "state.json"),
        log_file=_get("LOG_FILE", "logs/bot.log"),
        log_level=_get("LOG_LEVEL", "INFO").upper(),
        heartbeat_file=_get("HEARTBEAT_FILE", "heartbeat.json"),
        ledger_file=_get("LEDGER_FILE", "trades.csv"),
    )

    if cfg.grid_mode not in ("hybrid", "pilot"):
        raise RuntimeError("GRID_MODE must be 'hybrid' or 'pilot'")
    if cfg.grid_mode == "pilot" and len(cfg.pilot_points) < 2:
        raise RuntimeError("PILOT_POINTS must have at least 2 comma-separated price points "
                            "when GRID_MODE=pilot")
    if not (cfg.anchor_low < cfg.dense_low < cfg.dense_high <= cfg.anchor_high):
        raise RuntimeError("Require ANCHOR_LOW < DENSE_LOW < DENSE_HIGH <= ANCHOR_HIGH "
                            "(set ANCHOR_HIGH = DENSE_HIGH for no upper anchor zone)")
    if cfg.anchor_step <= 0 or cfg.dense_step <= 0:
        raise RuntimeError("ANCHOR_STEP and DENSE_STEP must be positive")
    if cfg.dense_sell_offset <= 0:
        raise RuntimeError("DENSE_SELL_OFFSET must be positive")
    if cfg.total_capital_usdt <= 0:
        raise RuntimeError("TOTAL_CAPITAL_USDT must be positive")
    if cfg.bulk_amount_usdt < 0:
        raise RuntimeError("BULK_AMOUNT_USDT must be zero or positive")
    if cfg.bulk_amount_usdt > 0 and cfg.bulk_sell_step <= 0:
        raise RuntimeError("BULK_SELL_STEP must be positive when BULK_AMOUNT_USDT is set")
    if cfg.reference_price <= 0:
        raise RuntimeError("REFERENCE_PRICE must be positive")
    if not (1 <= cfg.max_open_orders <= 200):
        raise RuntimeError("MAX_OPEN_ORDERS must be between 1 and 200 "
                            "(Binance.US caps open orders per symbol at 200)")
    if cfg.taker_avoidance_buffer < 0:
        raise RuntimeError("TAKER_AVOIDANCE_BUFFER must be zero or positive")
    if cfg.zone_shift_interval_hours <= 0:
        raise RuntimeError("ZONE_SHIFT_INTERVAL_HOURS must be positive")
    if not (0.0 < cfg.near_market_band_pct < 1.0):
        raise RuntimeError("NEAR_MARKET_BAND_PCT must be between 0 and 1 (e.g. 0.10 for 10%)")
    if cfg.consolidation_step <= 0:
        raise RuntimeError("CONSOLIDATION_STEP must be positive")
    if not (0.0 < cfg.circuit_breaker_drop_pct < 1.0):
        raise RuntimeError("CIRCUIT_BREAKER_DROP_PCT must be between 0 and 1 (e.g. 0.15 for 15%)")
    if cfg.circuit_breaker_window_hours <= 0:
        raise RuntimeError("CIRCUIT_BREAKER_WINDOW_HOURS must be positive")
    if not (0.0 < cfg.circuit_breaker_resume_pct < 1.0):
        raise RuntimeError("CIRCUIT_BREAKER_RESUME_PCT must be between 0 and 1 (e.g. 0.05 for 5%)")
    if not (0.0 <= cfg.profit_reserve_ratio <= 1.0):
        raise RuntimeError("PROFIT_RESERVE_RATIO must be between 0 and 1")
    if not (0.0 < cfg.reinvest_band_pct < 1.0):
        raise RuntimeError("REINVEST_BAND_PCT must be between 0 and 1 (e.g. 0.04 for 4%)")
    if cfg.pending_reinvest_max_hours <= 0:
        raise RuntimeError("PENDING_REINVEST_MAX_HOURS must be positive")
    if cfg.dense_window_half_width <= 0:
        raise RuntimeError("DENSE_WINDOW_HALF_WIDTH must be positive")
    if cfg.dense_window_snap <= 0:
        raise RuntimeError("DENSE_WINDOW_SNAP must be positive")

    return cfg
