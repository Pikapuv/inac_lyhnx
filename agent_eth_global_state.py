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
    # Per-symbol positions (multi-position mode).
    # key: "ETH/USDT", value: position payload.
    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    trades_opened_per_symbol: Dict[str, int] = field(default_factory=dict)

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
            positions=raw.get("positions") or {},
            trades_opened_per_symbol=raw.get("trades_opened_per_symbol") or {},
        )

        if st.trading_day != current_trading_day():
            prev_entry = st.auto_trade_enabled
            st = cls.new_for_day(settings)
            st.auto_trade_enabled = prev_entry
            st.save(path)

        # Backward-compatible migration: old single-position state -> positions map.
        if (
            not st.positions
            and st.has_position
            and st.active_symbol
            and st.entry_price is not None
        ):
            st.positions[st.active_symbol] = {
                "has_position": True,
                "entry_price": st.entry_price,
                "position_open_time": st.position_open_time,
                "size_usdt": st.size_usdt,
                "size_coin": st.size_coin,
                "buy_fee_usdt": st.buy_fee_usdt,
                "buy_trade_id": st.buy_trade_id,
                "last_trade_check_ts": st.last_trade_check_ts,
                "tp_alert_sent": st.tp_alert_sent,
                "sl_alert_sent": st.sl_alert_sent,
                "time_stop_alert_sent": st.time_stop_alert_sent,
                "cooldown_until_ts": st.cooldown_until_ts,
            }

        # Sanity normalize for positions map.
        normalized_positions: Dict[str, Dict[str, Any]] = {}
        for sym, pos in (st.positions or {}).items():
            if not sym or not isinstance(pos, dict):
                continue
            if not bool(pos.get("has_position")):
                continue
            normalized_positions[sym] = {
                "has_position": True,
                "entry_price": pos.get("entry_price"),
                "position_open_time": pos.get("position_open_time"),
                "size_usdt": pos.get("size_usdt"),
                "size_coin": pos.get("size_coin"),
                "buy_fee_usdt": pos.get("buy_fee_usdt"),
                "buy_trade_id": pos.get("buy_trade_id"),
                "last_trade_check_ts": pos.get("last_trade_check_ts"),
                "tp_alert_sent": bool(pos.get("tp_alert_sent", False)),
                "sl_alert_sent": bool(pos.get("sl_alert_sent", False)),
                "time_stop_alert_sent": bool(pos.get("time_stop_alert_sent", False)),
                "cooldown_until_ts": pos.get("cooldown_until_ts"),
            }
        st.positions = normalized_positions

        # Keep legacy fields in sync so old code paths still work.
        if st.positions:
            first_symbol = next(iter(st.positions.keys()))
            first_pos = st.positions[first_symbol]
            st.has_position = True
            st.active_symbol = first_symbol
            st.entry_price = first_pos.get("entry_price")
            st.position_open_time = first_pos.get("position_open_time")
            st.size_usdt = first_pos.get("size_usdt")
            st.size_coin = first_pos.get("size_coin")
            st.buy_fee_usdt = first_pos.get("buy_fee_usdt")
            st.buy_trade_id = first_pos.get("buy_trade_id")
            st.last_trade_check_ts = first_pos.get("last_trade_check_ts")
            st.tp_alert_sent = bool(first_pos.get("tp_alert_sent", False))
            st.sl_alert_sent = bool(first_pos.get("sl_alert_sent", False))
            st.time_stop_alert_sent = bool(first_pos.get("time_stop_alert_sent", False))
            st.cooldown_until_ts = first_pos.get("cooldown_until_ts")
        else:
            st.has_position = False
            st.active_symbol = None
            st.entry_price = None
            st.position_open_time = None
            st.size_usdt = None
            st.size_coin = None
            st.buy_fee_usdt = None
            st.buy_trade_id = None
            st.last_trade_check_ts = None
            st.tp_alert_sent = False
            st.sl_alert_sent = False
            st.time_stop_alert_sent = False
            st.cooldown_until_ts = None

        if not st.pending_proposal_id:
            st.pending_proposal_symbol = None
            st.pending_proposal_ts = None

        return st

    def open_positions_count(self) -> int:
        return len([1 for p in self.positions.values() if bool(p.get("has_position"))])

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    def save(self, path: Path = GLOBAL_STATE_PATH) -> None:
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_json(), f, ensure_ascii=False, indent=2)

