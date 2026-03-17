from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List, Tuple

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


def can_open_new_trade(settings: Settings, state: State) -> bool:
    if state.daily_limit_reached:
        return False
    if abs(state.pnl_day_usdt) >= state.daily_limit_usdt:
        state.daily_limit_reached = True
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

    # "Ít lệnh - chất lượng": yêu cầu dump lan sang khung 15m
    # Nếu chỉ dump 5m nhưng 15m không dump đủ sâu, bỏ tín hiệu.
    change_15m = mkt.changes_15m.get(symbol_key)
    if change_15m is None or change_15m > settings.dump_threshold_pct:
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

    # -------- "Tín hiệu đẹp" (quality filters) --------
    # Mục tiêu: chỉ BUY khi có nhiều yếu tố xác nhận cùng lúc.
    if not mkt.ohlcv_5m or len(mkt.ohlcv_5m) < max(
        settings.support_lookback_bars,
        settings.ma_trend_period,
        settings.rsi_period + 1,
    ):
        return None

    ohlcv = mkt.ohlcv_5m
    closes = [row[4] for row in ohlcv]
    lows = [row[3] for row in ohlcv]
    last_close = closes[-1]
    last_low = lows[-1]

    recent_low = min(lows[-settings.support_lookback_bars :])
    cond_support = last_low <= recent_low * (
        1.0 + settings.support_margin_pct / 100.0
    )

    cond_bullish_candle = _is_bullish_candle(ohlcv)

    rsi = _rsi(closes, settings.rsi_period)
    rsi_prev = _rsi(closes[:-1], settings.rsi_period)
    # RSI quay đầu (momentum tăng dần) để tăng winrate
    cond_rsi_turn = (
        rsi is not None
        and rsi_prev is not None
        and rsi_prev <= settings.rsi_oversold
        and rsi >= rsi_prev
    )
    cond_rsi = rsi is not None and rsi <= settings.rsi_oversold

    ma = _sma(closes, settings.ma_trend_period)
    cond_trend = ma is not None and last_close >= ma

    quality_score = sum(
        [cond_support, cond_bullish_candle, cond_rsi, cond_rsi_turn, cond_trend]
    )  # type: ignore[arg-type]

    # Ít lệnh - chất lượng: yêu cầu nhiều điều kiện.
    # Nhưng để "mỗi ngày UTC vẫn ít nhất 1 lệnh", nếu gần cuối ngày mà chưa có lệnh nào,
    # nới yêu cầu quality.
    min_required = settings.quality_min_conditions
    if state.trades_opened == 0 and now_utc.hour >= settings.force_min_trades_from_hour_utc:
        min_required = settings.quality_fallback_min_conditions
    if (
        state.trades_opened == 0
        and now_utc.hour >= settings.force_min_trades_from_hour_utc + 2
    ):
        min_required = 1

    if quality_score < min_required:
        return None

    c0 = settings.initial_capital_usdt
    # Mỗi lệnh dùng cố định 70% vốn (theo yêu cầu "ít lệnh - chất lượng").
    stake_pct = 0.70
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
        if (
            (not state.time_stop_alert_sent)
            and age_minutes >= 90
            and abs(pnl_pct) <= 0.5
        ):
            result["time_stop_alert"] = True

    return result

