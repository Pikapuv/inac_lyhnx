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
    kind: str = "DIP"  # DIP | BREAKOUT
    score: float = 0.0
    reason: str = ""

    def to_message(self) -> str:
        dt = datetime.fromtimestamp(self.ts, tz=timezone.utc)
        return (
            f"[BUY PROPOSAL] {self.symbol} ({self.kind}, score={self.score:.2f})\n"
            f"Thời gian (UTC): {dt:%Y-%m-%d %H:%M:%S}\n"
            f"Giá: {self.price:.4f}\n"
            f"Kích thước: {self.size_usdt:.2f} USDT (~{self.size_coin:.5f})\n"
            f"TP1: {self.tp1:.4f} (+1%)\n"
            f"TP2: {self.tp2:.4f} (+2%)\n"
            f"SL: {self.sl:.4f} (-2%)\n"
            f"Lý do: {self.reason}"
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


def _trend_metrics_from_ohlcv_5m(
    settings: Settings,
    ohlcv_5m: List[Tuple[float, float, float, float, float, float]],
    now_ts: float,
    price_now: float,
) -> Dict[str, Any]:
    closes_1h = _downsample_5m_to_1h_closes(ohlcv_5m, current_ts=now_ts)
    ema50 = _ema(closes_1h, settings.ema_trend_period_1h)
    ema20 = _ema(closes_1h, 20) if len(closes_1h) >= 20 else None
    trend_ok = (ema50 is not None) and (price_now > ema50)
    trend_strong = False
    if ema50 is not None and ema20 is not None:
        trend_strong = price_now > ema20 > ema50
    return {"ema50": ema50, "ema20": ema20, "trend_ok": trend_ok, "trend_strong": trend_strong}


def _volume_spike(mkt: MarketSnapshot, symbol_key: str) -> Optional[float]:
    vol_5m = mkt.volumes.get(symbol_key)
    vol_avg_1h = mkt.volumes.get(f"{symbol_key}_AVG1H")
    if vol_5m is None or vol_avg_1h is None or vol_avg_1h <= 0:
        return None
    return float(vol_5m) / float(vol_avg_1h)


def _rsi_5m(settings: Settings, mkt: MarketSnapshot) -> Optional[float]:
    if not mkt.ohlcv_5m:
        return None
    closes_5m = [row[4] for row in mkt.ohlcv_5m]
    return _rsi(closes_5m, settings.rsi_period)


def evaluate_entry_candidate(
    settings: Settings, state: State | GlobalState, mkt: MarketSnapshot
) -> Optional[BuyProposal]:
    """
    Pro entry:
    - Hard filters: session, risk/day, trend_ok, avoid-fomo
    - Two modes compete: DIP vs BREAKOUT
    - Use scoring + threshold entry_score_min
    """
    now_utc = datetime.fromtimestamp(mkt.ts, tz=timezone.utc)
    now_ts = mkt.ts
    if not within_trading_session(settings, now_utc):
        return None
    if not can_open_new_trade(settings, state, now_ts=now_ts):
        return None

    symbol_key = settings.symbol.replace("/", "")
    price_now = mkt.prices.get(symbol_key)
    if price_now is None or not mkt.ohlcv_5m:
        return None

    trend = _trend_metrics_from_ohlcv_5m(settings, mkt.ohlcv_5m, now_ts=now_ts, price_now=price_now)
    if not trend["trend_ok"]:
        return None

    # Avoid FOMO (still a hard filter for both modes)
    change_5m = mkt.changes_5m.get(symbol_key)
    if change_5m is not None and change_5m >= settings.pump_threshold_pct:
        return None

    rsi_val = _rsi_5m(settings, mkt)
    vol_spike = _volume_spike(mkt, symbol_key)
    change_1h = mkt.changes_1h.get(symbol_key)

    # Candle confirmation
    last_open = mkt.ohlcv_5m[-1][1]
    last_close = mkt.ohlcv_5m[-1][4]
    candle_green = last_close > last_open

    candidates: List[BuyProposal] = []

    # --- Mode A: DIP (adaptive thresholds) ---
    dip_thr_1h = settings.dump_threshold_1h_pct_trend_strong if trend["trend_strong"] else settings.dump_threshold_1h_pct
    rsi_thr = settings.rsi_reversal_threshold_5m_trend_strong if trend["trend_strong"] else settings.rsi_reversal_threshold_5m

    dip_ok = (change_1h is not None) and (change_1h <= dip_thr_1h)
    rsi_ok = (rsi_val is not None) and (rsi_val <= rsi_thr)
    vol_ok = (vol_spike is not None) and (vol_spike >= 1.0)

    dip_score = 0.0
    dip_reasons: List[str] = []
    if dip_ok and change_1h is not None:
        dip_score += min(3.0, abs(float(change_1h))) * 0.8
        dip_reasons.append(f"dip_1h={change_1h:.2f}%<= {dip_thr_1h:.2f}%")
    if rsi_ok and rsi_val is not None:
        dip_score += max(0.0, (rsi_thr - float(rsi_val))) * 0.10
        dip_reasons.append(f"rsi5m={rsi_val:.1f}<= {rsi_thr:.1f}")
    if vol_ok and vol_spike is not None:
        dip_score += min(2.0, float(vol_spike)) * 0.6
        dip_reasons.append(f"vol_spike={vol_spike:.2f}x")
    if candle_green:
        dip_score += 0.3
        dip_reasons.append("candle=green")

    if dip_score > 0:
        candidates.append(
            _proposal_from_price(
                settings=settings,
                mkt=mkt,
                price_now=price_now,
                kind="DIP",
                score=dip_score,
                reason="; ".join(dip_reasons) if dip_reasons else "dip-scan",
            )
        )

    # --- Mode B: BREAKOUT ---
    if settings.breakout_enabled:
        lookback_bars = max(1, settings.breakout_lookback_hours * 12)
        highs = [row[2] for row in mkt.ohlcv_5m[-lookback_bars:]]
        breakout_score = 0.0
        br_reasons: List[str] = []
        if len(highs) >= 2:
            prev_high = max(highs[:-1])  # exclude last candle high
            buffer = prev_high * (1 + settings.breakout_buffer_pct / 100.0)
            breakout_ok = last_close > buffer
            if breakout_ok:
                breakout_score += 2.0
                br_reasons.append(f"break>{prev_high:.4f} (+{settings.breakout_buffer_pct:.2f}%)")
                # Volume confirmation stronger for breakout
                if vol_spike is not None and vol_spike >= settings.breakout_volume_mult:
                    breakout_score += min(3.0, vol_spike) * 0.8
                    br_reasons.append(f"vol_spike={vol_spike:.2f}x>= {settings.breakout_volume_mult:.2f}x")
                # Candle bullish adds confidence
                if candle_green:
                    breakout_score += 0.3
                    br_reasons.append("candle=green")
        if breakout_score > 0:
            candidates.append(
                _proposal_from_price(
                    settings=settings,
                    mkt=mkt,
                    price_now=price_now,
                    kind="BREAKOUT",
                    score=breakout_score,
                    reason="; ".join(br_reasons) if br_reasons else "breakout-scan",
                )
            )

    if not candidates:
        return None

    best = max(candidates, key=lambda p: p.score)
    if best.score < settings.entry_score_min:
        return None
    return best


def _proposal_from_price(
    settings: Settings,
    mkt: MarketSnapshot,
    price_now: float,
    kind: str,
    score: float,
    reason: str,
) -> BuyProposal:
    symbol_key = settings.symbol.replace("/", "")
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
        kind=kind,
        score=float(score),
        reason=reason,
    )


def scan_diagnostics(settings: Settings, mkt: MarketSnapshot) -> Dict[str, Any]:
    """
    Lightweight diagnostics per symbol to detect missed signals.
    Does NOT apply risk/session gating; caller can add `gated`.
    """
    symbol_key = settings.symbol.replace("/", "")
    price_now = mkt.prices.get(symbol_key)
    if price_now is None or not mkt.ohlcv_5m:
        return {
            "price": price_now,
            "chg5m": mkt.changes_5m.get(symbol_key),
            "chg1h": mkt.changes_1h.get(symbol_key),
            "rsi5m": None,
            "vol_spike": None,
            "trend_strong": None,
            "dip_score": None,
            "breakout_score": None,
            "best_kind": None,
            "best_score": None,
            "best_reason": "",
            "passed": False,
        }

    trend = _trend_metrics_from_ohlcv_5m(settings, mkt.ohlcv_5m, now_ts=mkt.ts, price_now=float(price_now))
    rsi_val = _rsi_5m(settings, mkt)
    vol_spike = _volume_spike(mkt, symbol_key)
    change_1h = mkt.changes_1h.get(symbol_key)

    last_open = mkt.ohlcv_5m[-1][1]
    last_close = mkt.ohlcv_5m[-1][4]
    candle_green = last_close > last_open

    dip_thr_1h = settings.dump_threshold_1h_pct_trend_strong if trend["trend_strong"] else settings.dump_threshold_1h_pct
    rsi_thr = settings.rsi_reversal_threshold_5m_trend_strong if trend["trend_strong"] else settings.rsi_reversal_threshold_5m

    dip_score = 0.0
    dip_reasons: List[str] = []
    if (change_1h is not None) and (change_1h <= dip_thr_1h):
        dip_score += min(3.0, abs(float(change_1h))) * 0.8
        dip_reasons.append(f"dip_1h={change_1h:.2f}%<= {dip_thr_1h:.2f}%")
    if (rsi_val is not None) and (rsi_val <= rsi_thr):
        dip_score += max(0.0, (rsi_thr - float(rsi_val))) * 0.10
        dip_reasons.append(f"rsi5m={rsi_val:.1f}<= {rsi_thr:.1f}")
    if (vol_spike is not None) and (vol_spike >= 1.0):
        dip_score += min(2.0, float(vol_spike)) * 0.6
        dip_reasons.append(f"vol_spike={vol_spike:.2f}x")
    if candle_green:
        dip_score += 0.3
        dip_reasons.append("candle=green")
    if dip_score <= 0:
        dip_score_val: float | None = None
    else:
        dip_score_val = float(dip_score)

    breakout_score = 0.0
    br_reasons: List[str] = []
    if settings.breakout_enabled:
        lookback_bars = max(1, settings.breakout_lookback_hours * 12)
        highs = [row[2] for row in mkt.ohlcv_5m[-lookback_bars:]]
        if len(highs) >= 2:
            prev_high = max(highs[:-1])
            buffer = prev_high * (1 + settings.breakout_buffer_pct / 100.0)
            if last_close > buffer:
                breakout_score += 2.0
                br_reasons.append(f"break>{prev_high:.4f} (+{settings.breakout_buffer_pct:.2f}%)")
                if vol_spike is not None and vol_spike >= settings.breakout_volume_mult:
                    breakout_score += min(3.0, vol_spike) * 0.8
                    br_reasons.append(f"vol_spike={vol_spike:.2f}x>= {settings.breakout_volume_mult:.2f}x")
                if candle_green:
                    breakout_score += 0.3
                    br_reasons.append("candle=green")
    breakout_score_val = float(breakout_score) if breakout_score > 0 else None

    best_kind = None
    best_score = None
    best_reason = ""
    if dip_score_val is not None or breakout_score_val is not None:
        if (breakout_score_val or 0.0) >= (dip_score_val or 0.0):
            best_kind = "BREAKOUT"
            best_score = breakout_score_val or 0.0
            best_reason = "; ".join(br_reasons)
        else:
            best_kind = "DIP"
            best_score = dip_score_val or 0.0
            best_reason = "; ".join(dip_reasons)

    passed = bool(best_score is not None and best_score >= settings.entry_score_min and trend["trend_ok"])
    return {
        "price": float(price_now),
        "chg5m": mkt.changes_5m.get(symbol_key),
        "chg1h": mkt.changes_1h.get(symbol_key),
        "rsi5m": float(rsi_val) if rsi_val is not None else None,
        "vol_spike": float(vol_spike) if vol_spike is not None else None,
        "trend_strong": bool(trend["trend_strong"]),
        "dip_score": dip_score_val,
        "breakout_score": breakout_score_val,
        "best_kind": best_kind,
        "best_score": float(best_score) if best_score is not None else None,
        "best_reason": best_reason,
        "passed": passed,
    }


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
    return evaluate_entry_candidate(settings, state, mkt)


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

