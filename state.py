import json
import os
import time
from dataclasses import dataclass, field

from grid import GridLevel


@dataclass
class BotState:
    levels: list = field(default_factory=list)  # list[GridLevel]
    reserved_total: float = 0.0
    total_profit_realized: float = 0.0
    config_signature: dict = field(default_factory=dict)
    last_zone_shift_ts: float = 0.0
    next_level_index: int = 0
    price_history: list = field(default_factory=list)  # list[[ts, price]], trimmed to the circuit breaker window
    circuit_breaker_tripped: bool = False
    circuit_breaker_reason: str = ""
    circuit_breaker_tripped_ts: float = 0.0
    circuit_breaker_trip_low: float = 0.0  # lowest price seen since trip, for auto-resume hysteresis
    dense_window_center: float = None  # last DENSE_WINDOW_SNAP-rounded center built around; None = never run yet
    pending_reinvest: float = 0.0  # distribute-share with nowhere eligible to go yet, retried each cycle
    pending_reinvest_since: float = 0.0  # unix ts pending_reinvest started accumulating; 0.0 = none pending

    def to_dict(self):
        return {
            "levels": [lvl.to_dict() for lvl in self.levels],
            "reserved_total": self.reserved_total,
            "total_profit_realized": self.total_profit_realized,
            "config_signature": self.config_signature,
            "last_zone_shift_ts": self.last_zone_shift_ts,
            "next_level_index": self.next_level_index,
            "price_history": self.price_history,
            "circuit_breaker_tripped": self.circuit_breaker_tripped,
            "circuit_breaker_reason": self.circuit_breaker_reason,
            "circuit_breaker_tripped_ts": self.circuit_breaker_tripped_ts,
            "circuit_breaker_trip_low": self.circuit_breaker_trip_low,
            "dense_window_center": self.dense_window_center,
            "pending_reinvest": self.pending_reinvest,
            "pending_reinvest_since": self.pending_reinvest_since,
        }

    @staticmethod
    def from_dict(d):
        return BotState(
            levels=[GridLevel.from_dict(lvl) for lvl in d["levels"]],
            reserved_total=d.get("reserved_total", 0.0),
            total_profit_realized=d.get("total_profit_realized", 0.0),
            config_signature=d.get("config_signature", {}),
            last_zone_shift_ts=d.get("last_zone_shift_ts", 0.0),
            next_level_index=d.get("next_level_index",
                                    (max((lvl.index for lvl in
                                          [GridLevel.from_dict(x) for x in d["levels"]]), default=-1) + 1)),
            price_history=d.get("price_history", []),
            circuit_breaker_tripped=d.get("circuit_breaker_tripped", False),
            circuit_breaker_reason=d.get("circuit_breaker_reason", ""),
            circuit_breaker_tripped_ts=d.get("circuit_breaker_tripped_ts", 0.0),
            circuit_breaker_trip_low=d.get("circuit_breaker_trip_low", 0.0),
            dense_window_center=d.get("dense_window_center"),
            pending_reinvest=d.get("pending_reinvest", 0.0),
            pending_reinvest_since=d.get("pending_reinvest_since", 0.0),
        )


def load_state(path: str):
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return BotState.from_dict(json.load(f))


def save_state(path: str, state: BotState):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state.to_dict(), f, indent=2)
    os.replace(tmp_path, path)
