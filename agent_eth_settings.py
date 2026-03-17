from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Dict, Any


SETTINGS_PATH = Path(__file__).with_name("settings.json")


@dataclass
class TradingSession:
    start_hour: int
    end_hour: int


@dataclass
class Settings:
    initial_capital_usdt: float = 20.0
    daily_limit_pct: float = 3.0
    max_trades_per_day: int = 3

    tp_pct_min: float = 1.0
    tp_pct_max: float = 2.0
    sl_pct: float = 2.0

    dump_threshold_pct: float = -0.3
    pump_threshold_pct: float = 1.5

    symbol: str = "ETH/USDT"

    trading_sessions: List[TradingSession] | None = None
    auto_trade_day: bool = False
    auto_trade_night: bool = False

    @property
    def daily_limit_usdt(self) -> float:
        return self.initial_capital_usdt * self.daily_limit_pct / 100.0

    @staticmethod
    def default_sessions() -> List[TradingSession]:
        return [TradingSession(start_hour=0, end_hour=23)]

    @classmethod
    def load(cls, path: Path = SETTINGS_PATH) -> "Settings":
        if not path.exists():
            settings = cls(trading_sessions=cls.default_sessions())
            settings.save(path)
            return settings

        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        sessions_raw = raw.get("trading_sessions") or []
        sessions = [
            TradingSession(start_hour=s["start_hour"], end_hour=s["end_hour"])
            for s in sessions_raw
        ]

        return cls(
            initial_capital_usdt=raw.get("initial_capital_usdt", 20.0),
            daily_limit_pct=raw.get("daily_limit_pct", 3.0),
            max_trades_per_day=raw.get("max_trades_per_day", 3),
            tp_pct_min=raw.get("tp_pct_min", 1.0),
            tp_pct_max=raw.get("tp_pct_max", 2.0),
            sl_pct=raw.get("sl_pct", 2.0),
            dump_threshold_pct=raw.get("dump_threshold_pct", -0.3),
            pump_threshold_pct=raw.get("pump_threshold_pct", 1.5),
            symbol=raw.get("symbol", "ETH/USDT"),
            trading_sessions=sessions or cls.default_sessions(),
            auto_trade_day=raw.get("auto_trade_day", False),
            auto_trade_night=raw.get("auto_trade_night", False),
        )

    def to_json(self) -> Dict[str, Any]:
        data = asdict(self)
        data["trading_sessions"] = [
            {"start_hour": s.start_hour, "end_hour": s.end_hour}
            for s in (self.trading_sessions or [])
        ]
        return data

    def save(self, path: Path = SETTINGS_PATH) -> None:
        if self.trading_sessions is None:
            self.trading_sessions = self.default_sessions()

        data = self.to_json()
        with path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

