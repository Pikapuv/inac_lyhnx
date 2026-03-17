from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any

from agent_eth_settings import Settings


STATE_PATH = Path(__file__).with_name("state.json")


def current_trading_day() -> str:
    # Simplified: use UTC calendar date for trading day id
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class State:
    trading_day: str
    initial_capital_usdt: float
    daily_limit_usdt: float

    pnl_day_usdt: float = 0.0
    trades_opened: int = 0
    trades_closed: int = 0

    has_position: bool = False
    entry_price: float | None = None
    position_open_time: float | None = None
    size_usdt: float | None = None
    size_coin: float | None = None
    buy_fee_usdt: float | None = None
    # For auto tracking via Binance my-trades
    buy_trade_id: str | None = None
    last_trade_check_ts: float | None = None

    # Proposal đang chờ người dùng bấm ENTER/SKIP
    pending_proposal_id: str | None = None
    pending_proposal_ts: float | None = None
    tp_alert_sent: bool = False
    sl_alert_sent: bool = False
    time_stop_alert_sent: bool = False

    auto_trade_enabled: bool = False

    # V3-light V2 risk/day: khi chạm limit thì "khóa" giao dịch cả ngày.
    daily_limit_reached: bool = False
    daily_limit_notified: bool = False

    # Cooldown sau khi bot phát hiện position đã đóng (SELL fill)
    cooldown_until_ts: float | None = None

    @classmethod
    def new_for_day(cls, settings: Settings) -> "State":
        day = current_trading_day()
        return cls(
            trading_day=day,
            initial_capital_usdt=settings.initial_capital_usdt,
            daily_limit_usdt=settings.daily_limit_usdt,
        )

    @classmethod
    def load(cls, settings: Settings, path: Path = STATE_PATH) -> "State":
        if not path.exists():
            state = cls.new_for_day(settings)
            state.save(path)
            return state

        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        state = cls(
            trading_day=raw.get("trading_day", current_trading_day()),
            initial_capital_usdt=raw.get(
                "initial_capital_usdt", settings.initial_capital_usdt
            ),
            daily_limit_usdt=raw.get("daily_limit_usdt", settings.daily_limit_usdt),
            pnl_day_usdt=raw.get("pnl_day_usdt", 0.0),
            trades_opened=raw.get("trades_opened", 0),
            trades_closed=raw.get("trades_closed", 0),
            has_position=raw.get("has_position", False),
            entry_price=raw.get("entry_price"),
            position_open_time=raw.get("position_open_time"),
            size_usdt=raw.get("size_usdt"),
            size_coin=raw.get("size_coin"),
            buy_trade_id=raw.get("buy_trade_id"),
            last_trade_check_ts=raw.get("last_trade_check_ts"),
            pending_proposal_id=raw.get("pending_proposal_id"),
            pending_proposal_ts=raw.get("pending_proposal_ts"),
            tp_alert_sent=raw.get("tp_alert_sent", False),
            sl_alert_sent=raw.get("sl_alert_sent", False),
            time_stop_alert_sent=raw.get("time_stop_alert_sent", False),
            auto_trade_enabled=raw.get("auto_trade_enabled", False),
            daily_limit_reached=raw.get("daily_limit_reached", False),
            daily_limit_notified=raw.get("daily_limit_notified", False),
            cooldown_until_ts=raw.get("cooldown_until_ts"),
        )

        if state.trading_day != current_trading_day():
            state = cls.new_for_day(settings)
            state.save(path)

        return state

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Path = STATE_PATH) -> None:
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False, indent=2)

