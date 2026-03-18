from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from agent_eth_settings import Settings


GLOBAL_STATE_PATH = Path(__file__).with_name("global_state.json")


def current_trading_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class GlobalState:
    trading_day: str
    initial_capital_usdt: float
    daily_limit_usdt: float

    pnl_day_usdt: float = 0.0
    trades_opened: int = 0
    trades_closed: int = 0

    # Global position: only ONE active symbol at a time
    has_position: bool = False
    active_symbol: str | None = None
    entry_price: float | None = None
    position_open_time: float | None = None
    size_usdt: float | None = None
    size_coin: float | None = None
    buy_fee_usdt: float | None = None

    # For auto tracking via Binance my-trades
    buy_trade_id: str | None = None
    last_trade_check_ts: float | None = None

    # Pending proposal: only ONE pending at a time
    pending_proposal_id: str | None = None
    pending_proposal_symbol: str | None = None
    pending_proposal_ts: float | None = None

    tp_alert_sent: bool = False
    sl_alert_sent: bool = False
    time_stop_alert_sent: bool = False

    auto_trade_enabled: bool = True

    daily_limit_reached: bool = False
    daily_limit_notified: bool = False

    cooldown_until_ts: float | None = None

    # Proposal sending counters (used to throttle how many BUY proposals
    # we emit per day / per symbol).
    proposals_sent_today: int = 0
    proposals_sent_per_symbol: Dict[str, int] = field(default_factory=dict)

    @classmethod
    def new_for_day(cls, settings: Settings) -> "GlobalState":
        day = current_trading_day()
        return cls(
            trading_day=day,
            initial_capital_usdt=settings.initial_capital_usdt,
            daily_limit_usdt=settings.daily_limit_usdt,
            proposals_sent_today=0,
            proposals_sent_per_symbol={},
        )

    @classmethod
    def load(cls, settings: Settings, path: Path = GLOBAL_STATE_PATH) -> "GlobalState":
        if not path.exists():
            st = cls.new_for_day(settings)
            st.save(path)
            return st

        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f) or {}

        st = cls(
            trading_day=raw.get("trading_day", current_trading_day()),
            initial_capital_usdt=raw.get("initial_capital_usdt", settings.initial_capital_usdt),
            daily_limit_usdt=raw.get("daily_limit_usdt", settings.daily_limit_usdt),
            pnl_day_usdt=raw.get("pnl_day_usdt", 0.0),
            trades_opened=raw.get("trades_opened", 0),
            trades_closed=raw.get("trades_closed", 0),
            has_position=raw.get("has_position", False),
            active_symbol=raw.get("active_symbol"),
            entry_price=raw.get("entry_price"),
            position_open_time=raw.get("position_open_time"),
            size_usdt=raw.get("size_usdt"),
            size_coin=raw.get("size_coin"),
            buy_fee_usdt=raw.get("buy_fee_usdt"),
            buy_trade_id=raw.get("buy_trade_id"),
            last_trade_check_ts=raw.get("last_trade_check_ts"),
            pending_proposal_id=raw.get("pending_proposal_id"),
            pending_proposal_symbol=raw.get("pending_proposal_symbol"),
            pending_proposal_ts=raw.get("pending_proposal_ts"),
            tp_alert_sent=raw.get("tp_alert_sent", False),
            sl_alert_sent=raw.get("sl_alert_sent", False),
            time_stop_alert_sent=raw.get("time_stop_alert_sent", False),
            auto_trade_enabled=raw.get("auto_trade_enabled", True),
            daily_limit_reached=raw.get("daily_limit_reached", False),
            daily_limit_notified=raw.get("daily_limit_notified", False),
            cooldown_until_ts=raw.get("cooldown_until_ts"),
            proposals_sent_today=raw.get("proposals_sent_today", 0),
            proposals_sent_per_symbol=raw.get("proposals_sent_per_symbol") or {},
        )

        if st.trading_day != current_trading_day():
            prev_entry = st.auto_trade_enabled
            st = cls.new_for_day(settings)
            st.auto_trade_enabled = prev_entry
            st.save(path)

        # Sanity: if no position, clear active_symbol
        if not st.has_position:
            st.active_symbol = None
        if not st.pending_proposal_id:
            st.pending_proposal_symbol = None
            st.pending_proposal_ts = None

        return st

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Path = GLOBAL_STATE_PATH) -> None:
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False, indent=2)

