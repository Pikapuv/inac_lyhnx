from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from agent_eth_settings import Settings
from agent_eth_state import State


@dataclass
class MarketSnapshot:
    ts: float
    prices: Dict[str, float]
    changes_5m: Dict[str, float]
    changes_15m: Dict[str, float]
    volumes: Dict[str, float]
    atrs: Dict[str, float]


@dataclass
class BuyProposal:
    id: str
    symbol: str
    price: float
    ts: float
    size_usdt: float
    size_coin: float
    tp1: float
    tp2: float
    sl: float

    def to_message(self) -> str:
        dt = datetime.fromtimestamp(self.ts, tz=timezone.utc)
        return (
            f"[BUY PROPOSAL] {self.symbol}\n"
            f"Thời gian (UTC): {dt:%Y-%m-%d %H:%M:%S}\n"
            f"Giá: {self.price:.4f}\n"
            f"Kích thước: {self.size_usdt:.2f} USDT (~{self.size_coin:.5f})\n"
            f"TP1: {self.tp1:.4f} (+1%)\n"
            f"TP2: {self.tp2:.4f} (+2%)\n"
            f"SL: {self.sl:.4f} (-2%)"
        )


def within_trading_session(settings: Settings, now_utc: datetime) -> bool:
    hour = now_utc.hour
    sessions = settings.trading_sessions or []
    for s in sessions:
        if s.start_hour <= hour <= s.end_hour:
            return True
    return False


def can_open_new_trade(settings: Settings, state: State) -> bool:
    if abs(state.pnl_day_usdt) >= state.daily_limit_usdt:
        return False
    if state.trades_opened >= settings.max_trades_per_day:
        return False
    if state.has_position:
        return False
    return True


def build_buy_proposal(
    settings: Settings, state: State, mkt: MarketSnapshot
) -> Optional[BuyProposal]:
    now_utc = datetime.fromtimestamp(mkt.ts, tz=timezone.utc)

    if not within_trading_session(settings, now_utc):
        return None
    if not can_open_new_trade(settings, state):
        return None

    symbol_key = settings.symbol.replace("/", "")
    price_now = mkt.prices.get(symbol_key)
    if price_now is None:
        return None

    change_5m = mkt.changes_5m.get(symbol_key)
    if change_5m is None:
        return None

    # Không mua nếu đang pump mạnh hơn ngưỡng pump_threshold_pct
    # (ví dụ > +1.5%) – tránh FOMO đu đỉnh.
    if change_5m >= settings.pump_threshold_pct:
        return None

    # Chỉ quan tâm khi có dump đủ sâu so với dump_threshold_pct
    if change_5m >= settings.dump_threshold_pct:
        return None

    sol_change = change_5m
    btc_change = mkt.changes_5m.get("BTCUSDT")
    solbtc_change = mkt.changes_5m.get("SOLBTC")

    if btc_change is not None and not (sol_change < btc_change):
        return None
    if solbtc_change is not None and not (solbtc_change <= 0):
        return None

    vol_5m = mkt.volumes.get(symbol_key)
    vol_avg_1h = mkt.volumes.get(f"{symbol_key}_AVG1H")
    if vol_5m is not None and vol_avg_1h is not None:
        if not (vol_5m > 1.0 * vol_avg_1h):
            return None

    atr_5m = mkt.atrs.get(symbol_key)
    if atr_5m is not None:
        if atr_5m <= 0:
            return None

    change = change_5m
    c0 = settings.initial_capital_usdt
    if -0.5 <= change < -0.2:
        stake_pct = 0.15
    elif -1.0 <= change < -0.5:
        stake_pct = 0.25
    elif change < -1.0:
        stake_pct = 0.30
    else:
        return None

    size_usdt = stake_pct * c0
    size_coin = size_usdt / price_now

    tp1 = price_now * (1 + settings.tp_pct_min / 100.0)
    tp2 = price_now * (1 + settings.tp_pct_max / 100.0)
    sl = price_now * (1 - settings.sl_pct / 100.0)

    proposal_id = f"{int(mkt.ts)}"

    return BuyProposal(
        id=proposal_id,
        symbol=settings.symbol,
        price=price_now,
        ts=mkt.ts,
        size_usdt=size_usdt,
        size_coin=size_coin,
        tp1=tp1,
        tp2=tp2,
        sl=sl,
    )


def compute_position_pnl_pct(state: State, price_now: float) -> Optional[float]:
    if not state.has_position or state.entry_price is None:
        return None
    return (price_now - state.entry_price) / state.entry_price * 100.0


def check_tp_sl_time_stop(
    settings: Settings, state: State, price_now: float, now_ts: float
) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "tp_alert": False,
        "sl_alert": False,
        "time_stop_alert": False,
        "pnl_pct": None,
    }

    if not state.has_position or state.entry_price is None:
        return result

    pnl_pct = compute_position_pnl_pct(state, price_now)
    if pnl_pct is None:
        return result

    result["pnl_pct"] = pnl_pct

    if not state.tp_alert_sent and pnl_pct >= settings.tp_pct_min:
        result["tp_alert"] = True

    if not state.sl_alert_sent and pnl_pct <= -settings.sl_pct:
        result["sl_alert"] = True

    if state.position_open_time is not None:
        age_minutes = (now_ts - state.position_open_time) / 60.0
        if age_minutes >= 90 and abs(pnl_pct) <= 0.5:
            result["time_stop_alert"] = True

    return result

