from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

from agent_eth_settings import Settings
from agent_eth_state import State
from agent_eth_global_state import GlobalState


@dataclass
class MarketSnapshot:
    ts: float
    prices: Dict[str, float]
    changes_5m: Dict[str, float]
    changes_15m: Dict[str, float]
    changes_1h: Dict[str, float]
    volumes: Dict[str, float]
    atrs: Dict[str, float]
    # OHLCV candles for main symbol (5m), used for quality filters (RSI / support / candle pattern)
    # item: (ts, open, high, low, close, volume)
    ohlcv_5m: List[Tuple[float, float, float, float, float, float]]


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


def can_open_new_trade(settings: Settings, state: State | GlobalState, now_ts: float) -> bool:
    if state.daily_limit_reached:
        return False
    if abs(state.pnl_day_usdt) >= state.daily_limit_usdt:
        state.daily_limit_reached = True
        return False
    if state.cooldown_until_ts is not None and now_ts < state.cooldown_until_ts:
        return False
    if state.trades_opened >= settings.max_trades_per_day:
        return False
    if state.has_position:
        return False
    return True


def _sma(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / len(window)


def _rsi(closes: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(closes) < period + 1:
        return None
    # Simple RSI (mean gain/loss over the last `period`)
    gains: List[float] = []
    losses: List[float] = []
    for i in range(-period, 0):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains.append(delta)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(-delta)
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema(values: List[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + (1.0 - k) * ema
    return ema


def _downsample_5m_to_1h_closes(
    ohlcv_5m: List[Tuple[float, float, float, float, float, float]],
    current_ts: float,
) -> List[float]:
    """
    Downsample 5m candles into completed 1h closes using candle timestamps.
    We drop the current/incomplete 1h bucket.
    """
    if not ohlcv_5m:
        return []
    latest_hour_start = int(current_ts // 3600) * 3600
    bucket: Dict[int, float] = {}
    for row in ohlcv_5m:
        ts = row[0]
        hour_start = int(ts // 3600) * 3600
        if hour_start >= latest_hour_start:
            continue
        bucket[hour_start] = row[4]  # last close in the hour
    if not bucket:
        return []
    return [bucket[h] for h in sorted(bucket.keys())]


def _is_bullish_candle(ohlcv: List[Tuple[float, float, float, float, float, float]]) -> bool:
    if len(ohlcv) < 2:
        return False
    # (t, o, h, l, c, v)
    prev = ohlcv[-2]
    curr = ohlcv[-1]
    prev_o, prev_h, prev_l, prev_c = prev[1], prev[2], prev[3], prev[4]
    curr_o, curr_h, curr_l, curr_c = curr[1], curr[2], curr[3], curr[4]

    prev_bearish = prev_c < prev_o
    curr_bullish = curr_c > curr_o

    # Bullish engulfing: current body engulfs previous body
    engulfing = (
        prev_bearish
        and curr_bullish
        and curr_c >= prev_o
        and curr_o <= prev_c
    )

    # Hammer / pin bar (approx):
    body = abs(curr_c - curr_o)
    if body == 0:
        body = 1e-12
    lower_wick = min(curr_o, curr_c) - curr_l
    upper_wick = curr_h - max(curr_o, curr_c)
    hammer = (
        curr_bullish
        and lower_wick >= 2.0 * body
        and upper_wick <= body
    )

    return engulfing or hammer


def score_buy_signal(settings: Settings, mkt: MarketSnapshot) -> Optional[float]:
    """
    Higher score = better "kèo".
    Score idea (simple + robust):
      - stronger 1h dip (more negative) => higher score
      - lower RSI_5m (more oversold) => higher score
      - bigger volume spike vs avg 1h => higher score
    Only uses data already fetched in MarketSnapshot.
    """
    symbol_key = settings.symbol.replace("/", "")

    change_1h = mkt.changes_1h.get(symbol_key)
    if change_1h is None:
        return None

    if not mkt.ohlcv_5m:
        return None
    closes_5m = [row[4] for row in mkt.ohlcv_5m]
    rsi_5m = _rsi(closes_5m, settings.rsi_period)
    if rsi_5m is None:
        return None

    vol_5m = mkt.volumes.get(symbol_key)
    vol_avg_1h = mkt.volumes.get(f"{symbol_key}_AVG1H")
    if vol_5m is None or vol_avg_1h is None or vol_avg_1h <= 0:
        return None

    dip_strength = max(0.0, -float(change_1h))  # only count downside
    rsi_bonus = max(0.0, float(settings.rsi_reversal_threshold_5m) - float(rsi_5m))
    volume_spike = float(vol_5m) / float(vol_avg_1h)

    # Weighted sum
    return dip_strength * 0.4 + rsi_bonus * 0.3 + volume_spike * 0.3


def build_buy_proposal(
    settings: Settings, state: State | GlobalState, mkt: MarketSnapshot
) -> Optional[BuyProposal]:
    now_utc = datetime.fromtimestamp(mkt.ts, tz=timezone.utc)
    now_ts = mkt.ts

    if not within_trading_session(settings, now_utc):
        return None
    if not can_open_new_trade(settings, state, now_ts=now_ts):
        return None

    symbol_key = settings.symbol.replace("/", "")
    price_now = mkt.prices.get(symbol_key)
    if price_now is None:
        return None

    if not mkt.ohlcv_5m:
        return None

    # Trend filter: EMA50_1h (chỉ dùng 1h đóng đủ từ chuỗi 5m downsample theo hour bucket)
    closes_1h = _downsample_5m_to_1h_closes(mkt.ohlcv_5m, current_ts=now_ts)
    ema = _ema(closes_1h, settings.ema_trend_period_1h)
    if ema is None:
        return None
    trend_ok = price_now > ema
    if not trend_ok:
        return None

    # Dip (1h): change_1h <= -1.5% (config bằng dump_threshold_1h_pct)
    change_1h = mkt.changes_1h.get(symbol_key)
    if change_1h is None or change_1h > settings.dump_threshold_1h_pct:
        return None

    # Avoid FOMO: nếu 5m đang pump mạnh thì không vào
    change_5m = mkt.changes_5m.get(symbol_key)
    if change_5m is not None and change_5m >= settings.pump_threshold_pct:
        return None

    # Stabilizing: RSI_5m thấp + last candle green + volume_increase
    closes_5m = [row[4] for row in mkt.ohlcv_5m]
    last_open = mkt.ohlcv_5m[-1][1]
    last_close = mkt.ohlcv_5m[-1][4]
    last_candle_green = last_close > last_open
    if not last_candle_green:
        return None

    rsi_5m = _rsi(closes_5m, settings.rsi_period)
    if rsi_5m is None or rsi_5m > settings.rsi_reversal_threshold_5m:
        return None

    vol_5m = mkt.volumes.get(symbol_key)
    vol_avg_1h = mkt.volumes.get(f"{symbol_key}_AVG1H")
    if vol_5m is None or vol_avg_1h is None:
        return None
    volume_increase = vol_5m > vol_avg_1h
    if not volume_increase:
        return None

    # Not near resistance: recent_high trong 3h, cần còn upside > 1%
    lookback_bars = max(1, settings.resistance_lookback_hours * 12)
    highs = [row[2] for row in mkt.ohlcv_5m[-lookback_bars:]]
    if not highs:
        return None
    recent_high = max(highs)
    distance_to_recent_high_pct = (recent_high - price_now) / price_now * 100.0
    if distance_to_recent_high_pct <= settings.resistance_distance_pct_min:
        return None

    # Size & TP/SL (70% vốn mỗi lệnh)
    c0 = settings.initial_capital_usdt
    stake_pct = 0.70
    size_usdt = stake_pct * c0
    size_coin = size_usdt / price_now

    tp1 = price_now * (1 + settings.tp_pct_min / 100.0)
    tp2 = price_now * (1 + settings.tp_pct_max / 100.0)
    sl = price_now * (1 - settings.sl_pct / 100.0)

    proposal_id = f"{symbol_key}-{int(mkt.ts)}"
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
        if (
            (not state.time_stop_alert_sent)
            and age_minutes >= 90
            and abs(pnl_pct) <= 0.5
        ):
            result["time_stop_alert"] = True

    return result

