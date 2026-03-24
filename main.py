from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

import ccxt
import yaml

from agent_eth_settings import Settings
from agent_eth_state import State
from agent_eth_global_state import GlobalState
from agent_eth_strategy import (
    MarketSnapshot,
    build_buy_proposal,
    check_tp_sl_time_stop,
    score_buy_signal,
    scan_diagnostics,
)
from agent_eth_telegram import (
    build_application,
    send_buy_proposal_message,
    send_tp_sl_time_stop_message,
    expire_pending_proposal,
)
from agent_eth_state import state_path_for_symbol
from agent_eth_global_state import GLOBAL_STATE_PATH
from data_logger import DataLogger, ScanRow


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger("agent_eth")


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return default


def _build_binance_exchange_config(binance_cfg: Dict[str, object]) -> Dict[str, object]:
    api_key = str(binance_cfg.get("apiKey") or "")
    secret = str(binance_cfg.get("secret") or "")
    if not api_key or not secret:
        raise RuntimeError("Thiếu binance_read.apiKey/secret trong config.yaml")

    ex_cfg: Dict[str, object] = {
        "apiKey": api_key,
        "secret": secret,
        "enableRateLimit": True,
    }

    proxy_enabled = _as_bool(binance_cfg.get("proxy_enabled"), default=False)
    proxy_url = str(binance_cfg.get("proxy_url") or "").strip()
    if proxy_enabled:
        if not proxy_url:
            raise RuntimeError(
                "binance_read.proxy_enabled=true nhưng thiếu binance_read.proxy_url"
            )
        ex_cfg["httpsProxy"] = proxy_url
        logger.info("Binance proxy enabled via %s", proxy_url)
    else:
        logger.info("Binance proxy disabled.")
    return ex_cfg


def _binance_startup_healthcheck(
    ex: ccxt.Exchange,
    symbol: str,
    retries: int = 5,
    base_backoff_seconds: float = 2.0,
) -> None:
    """
    Verify Binance connectivity before entering the main loop.
    Retry with exponential backoff so transient network/proxy issues can recover.
    """
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            server_ms = ex.fetch_time()
            logger.info(
                "Binance healthcheck OK (attempt=%d/%d, server_time_ms=%s)",
                attempt,
                attempts,
                server_ms,
            )
            return
        except Exception as e_fetch_time:
            try:
                ticker = ex.fetch_ticker(symbol)
                last = ticker.get("last")
                logger.info(
                    "Binance healthcheck OK via fetch_ticker (attempt=%d/%d, symbol=%s, last=%s)",
                    attempt,
                    attempts,
                    symbol,
                    last,
                )
                return
            except Exception as e_fetch_ticker:
                logger.warning(
                    "Binance healthcheck failed (attempt=%d/%d): fetch_time_err=%s; fetch_ticker_err=%s",
                    attempt,
                    attempts,
                    e_fetch_time,
                    e_fetch_ticker,
                )
                if attempt >= attempts:
                    raise RuntimeError(
                        "Không kết nối được Binance sau nhiều lần thử (kiểm tra proxy/network/api key)."
                    ) from e_fetch_ticker
                wait_s = base_backoff_seconds * (2 ** (attempt - 1))
                logger.info("Retry Binance healthcheck sau %.1f giây...", wait_s)
                time.sleep(wait_s)


def _fmt_num(x: float | None, digits: int = 4) -> str:
    if x is None:
        return "n/a"
    try:
        return f"{float(x):.{digits}f}"
    except Exception:
        return "n/a"


def _render_market_table(rows: List[Dict[str, str]]) -> str:
    """
    Render a simple fixed-width table for logs.
    rows: [{"symbol": "...", "price": "...", "chg5m": "...", "chg1h": "..."}]
    """
    headers = ["SYMBOL", "PRICE", "CHG_5M", "CHG_1H"]
    data = [
        [r.get("symbol", ""), r.get("price", ""), r.get("chg5m", ""), r.get("chg1h", "")]
        for r in rows
    ]
    widths = [len(h) for h in headers]
    for row in data:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(sep: str = "-") -> str:
        return "+" + "+".join((sep * (w + 2)) for w in widths) + "+"

    def fmt_row(cells: List[str]) -> str:
        return "| " + " | ".join(cells[i].ljust(widths[i]) for i in range(len(widths))) + " |"

    out = [line("-"), fmt_row(headers), line("=")]
    out.extend(fmt_row(r) for r in data)
    out.append(line("-"))
    return "\n".join(out)


def _split_symbol(symbol: str) -> tuple[str, str]:
    if "/" not in symbol:
        return symbol, ""
    base, quote = symbol.split("/", 1)
    return base, quote


def _trade_cost_quote_usdt(trade: Dict) -> float:
    # ccxt spot trade: `cost` is usually quote amount.
    if trade.get("cost") is not None:
        return float(trade["cost"])
    price = float(trade.get("price") or 0.0)
    amount = float(trade.get("amount") or 0.0)
    return price * amount


def _trade_fee_quote_usdt(trade: Dict) -> float:
    fee = trade.get("fee") or {}
    cost = fee.get("cost")
    if cost is None:
        return 0.0
    return float(cost)


def _format_symbol(symbol: str) -> str:
    return symbol.replace("/", "")


@dataclass
class SymbolRuntimeState:
    # Fields required by strategy/TP-SL helpers (duck-typed)
    pnl_day_usdt: float
    daily_limit_usdt: float
    daily_limit_reached: bool
    trades_opened: int
    trades_closed: int
    has_position: bool
    entry_price: float | None
    position_open_time: float | None
    size_usdt: float | None
    size_coin: float | None
    buy_fee_usdt: float | None
    buy_trade_id: str | None
    last_trade_check_ts: float | None
    tp_alert_sent: bool
    sl_alert_sent: bool
    time_stop_alert_sent: bool
    cooldown_until_ts: float | None


def _position_to_runtime_state(gstate: GlobalState, symbol: str) -> SymbolRuntimeState:
    pos = gstate.positions.get(symbol) or {}
    return SymbolRuntimeState(
        pnl_day_usdt=gstate.pnl_day_usdt,
        daily_limit_usdt=gstate.daily_limit_usdt,
        daily_limit_reached=gstate.daily_limit_reached,
        trades_opened=int(gstate.trades_opened_per_symbol.get(symbol, 0)),
        trades_closed=int(gstate.trades_closed),
        has_position=bool(pos.get("has_position", False)),
        entry_price=pos.get("entry_price"),
        position_open_time=pos.get("position_open_time"),
        size_usdt=pos.get("size_usdt"),
        size_coin=pos.get("size_coin"),
        buy_fee_usdt=pos.get("buy_fee_usdt"),
        buy_trade_id=pos.get("buy_trade_id"),
        last_trade_check_ts=pos.get("last_trade_check_ts"),
        tp_alert_sent=bool(pos.get("tp_alert_sent", False)),
        sl_alert_sent=bool(pos.get("sl_alert_sent", False)),
        time_stop_alert_sent=bool(pos.get("time_stop_alert_sent", False)),
        cooldown_until_ts=pos.get("cooldown_until_ts"),
    )


def _runtime_state_to_position(gstate: GlobalState, symbol: str, st: SymbolRuntimeState) -> None:
    if st.has_position:
        gstate.positions[symbol] = {
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
    else:
        gstate.positions.pop(symbol, None)

    gstate.trades_opened_per_symbol[symbol] = max(0, int(st.trades_opened))
    gstate.trades_closed = max(int(gstate.trades_closed), int(st.trades_closed))
    gstate.daily_limit_reached = bool(st.daily_limit_reached)
    gstate.pnl_day_usdt = float(st.pnl_day_usdt)


async def sync_position_from_binance_trades(
    ex: ccxt.Exchange,
    settings: Settings,
    state: State | GlobalState,
    notify_message,
) -> None:
    """
    Auto tracking for manual entry/exit:
    - When user pressed ENTER, we mark state.has_position + position_open_time.
    - This function scans `fetch_my_trades` since the last check:
      * find the BUY trade after position_open_time (if not matched yet)
      * when a SELL trade arrives after BUY, treat it as closing the position and update pnl_day_usdt.
    """
    if not state.has_position or state.position_open_time is None:
        return

    since_ts = state.last_trade_check_ts or state.position_open_time
    since_ms = int(since_ts * 1000)

    logger.info(
        "sync_position: symbol=%s position_open_time=%.0f last_trade_check_ts=%s since_ms=%d buy_trade_id=%s",
        settings.symbol,
        state.position_open_time,
        state.last_trade_check_ts,
        since_ms,
        getattr(state, "buy_trade_id", None),
    )
    try:
        new_trades = ex.fetch_my_trades(settings.symbol, since=since_ms)
    except Exception as e:
        logger.warning("sync_position_from_binance_trades failed: %s", e)
        return

    # Always advance the cursor even if no trades found
    state.last_trade_check_ts = time.time()
    if isinstance(state, GlobalState):
        state.save(GLOBAL_STATE_PATH)
    elif isinstance(state, State):
        state.save(state_path_for_symbol(settings.symbol))

    if not new_trades:
        return

    base, quote = _split_symbol(settings.symbol)

    # Sort by timestamp ascending
    new_trades = sorted(new_trades, key=lambda t: t.get("timestamp") or 0)
    # Debug sample: show first few trades (side + timestamp) so we can see casing/format.
    sample = [
        {
            "id": str(t.get("id") or ""),
            "side": str(t.get("side") or ""),
            "ts": int((t.get("timestamp") or 0)),
        }
        for t in new_trades[:3]
    ]
    logger.info(
        "sync_position: fetched_trades=%d sample_first3=%s",
        len(new_trades),
        sample,
    )

    # 1) Match BUY trade (entry)
    if state.buy_trade_id is None:
        for tr in new_trades:
            ts_s = (tr.get("timestamp") or 0) / 1000.0
            if ts_s < (state.position_open_time or 0):
                continue
            side = str(tr.get("side") or "").lower()
            if side != "buy":
                continue

            state.buy_trade_id = str(tr.get("id"))
            state.entry_price = float(tr.get("price") or 0.0)

            buy_cost = _trade_cost_quote_usdt(tr)
            buy_fee = _trade_fee_quote_usdt(tr)
            state.buy_fee_usdt = buy_fee
            # size_usdt represents quote spent (approx, excluding fee or including it is small)
            state.size_usdt = buy_cost
            state.size_coin = float(tr.get("amount") or 0.0)
            if isinstance(state, GlobalState):
                state.save(GLOBAL_STATE_PATH)
            elif isinstance(state, State):
                state.save(state_path_for_symbol(settings.symbol))
            await notify_message(
                f"Đã khớp lệnh BUY trên Binance.\n"
                f"Entry: {state.entry_price:.4f} ({settings.symbol})\n"
                f"UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts_s))}"
            )
            logger.info(
                "sync_position: matched BUY trade id=%s side=%s ts_s=%.3f price=%.8f",
                state.buy_trade_id,
                side,
                ts_s,
                state.entry_price,
            )
            break

    # 2) If we have an entry buy, look for the first SELL after it
    if state.buy_trade_id is None:
        return

    buy_cost_usdt = state.size_usdt or 0.0
    buy_fee_usdt = state.buy_fee_usdt or 0.0
    if buy_cost_usdt <= 0:
        return

    for tr in new_trades:
        side = str(tr.get("side") or "").lower()
        if side != "sell":
            continue

        ts_s = (tr.get("timestamp") or 0) / 1000.0
        if ts_s < (state.position_open_time or 0):
            continue

        sell_cost_usdt = _trade_cost_quote_usdt(tr)
        sell_fee_usdt = _trade_fee_quote_usdt(tr)

        # Spot PnL approximation in quote currency:
        # PnL = sell proceeds - buy cost - (fees)
        # We treat fees as quote costs.
        pnl_usdt = (sell_cost_usdt - buy_cost_usdt) - (buy_fee_usdt + sell_fee_usdt)

        state.pnl_day_usdt += pnl_usdt
        state.has_position = False
        state.entry_price = None
        state.position_open_time = None
        state.size_usdt = None
        state.size_coin = None
        state.buy_trade_id = None
        state.buy_fee_usdt = None
        state.cooldown_until_ts = time.time() + settings.cooldown_minutes_after_close * 60
        state.tp_alert_sent = False
        state.sl_alert_sent = False
        state.time_stop_alert_sent = False
        state.trades_closed += 1
        if isinstance(state, GlobalState):
            state.save(GLOBAL_STATE_PATH)
        elif isinstance(state, State):
            state.save(state_path_for_symbol(settings.symbol))

        logger.info("Auto-tracked CLOSE pnl_day_usdt=%.4f (pnl=%.4f)", state.pnl_day_usdt, pnl_usdt)
        label = "CHỐT LỜI" if pnl_usdt >= 0 else "CẮT LỖ"
        daily_limit = state.daily_limit_usdt
        left = daily_limit - abs(state.pnl_day_usdt)
        await notify_message(
            f"Đã khớp lệnh SELL: {label}\n"
            f"P&L lệnh: {pnl_usdt:.4f} USDT\n"
            f"P&L ngày hiện tại: {state.pnl_day_usdt:.4f} USDT "
            f"(limit ±{daily_limit:.4f})\n"
            f"Còn dư biên hôm nay: {left:.4f} USDT"
        )
        return


def load_config() -> Dict:
    cfg_path = Path("config.yaml")
    if not cfg_path.exists():
        return {}
    try:
        return yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}

def load_config_required() -> Dict:
    cfg = load_config()
    if not cfg:
        raise RuntimeError("Thiếu config.yaml hoặc config.yaml không đọc được.")
    return cfg


def load_config_poll_interval(default_seconds: int = 30) -> int:
    raw = load_config()
    strategy = raw.get("strategy") or {}
    try:
        return int(strategy.get("poll_interval_sec", default_seconds))
    except Exception:
        return default_seconds


def _fetch_ohlcv(
    ex: ccxt.Exchange, symbol: str, timeframe: str, limit: int
) -> List[Tuple[float, float, float, float, float]]:
    ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    return [(t, o, h, l, c, v) for (t, o, h, l, c, v) in ohlcv]


def _compute_change_pct_from_ohlcv(
    ohlcv: List[Tuple[float, float, float, float, float]], bars: int
) -> float | None:
    if len(ohlcv) < bars + 1:
        return None
    prev_close = ohlcv[-(bars + 1)][4]
    last_close = ohlcv[-1][4]
    if prev_close <= 0:
        return None
    return (last_close - prev_close) / prev_close * 100.0


def _compute_volume_and_atr(
    ohlcv: List[Tuple[float, float, float, float, float]],
) -> Tuple[float, float, float]:
    if not ohlcv:
        return 0.0, 0.0, 0.0
    # Volumes: last 5m (1 bar), last 15m (3 bars), avg 1h (12 bars)
    vols = [row[5] for row in ohlcv]
    vol_5m = vols[-1]
    vol_15m = sum(vols[-3:]) if len(vols) >= 3 else sum(vols)
    last_12 = vols[-12:] if len(vols) >= 12 else vols
    vol_avg_1h = sum(last_12) / len(last_12)

    # Simple ATR: average high-low over last 12 bars
    trs = [(row[2] - row[3]) for row in ohlcv[-12:]]
    atr = sum(trs) / len(trs)
    return vol_5m, vol_15m, vol_avg_1h, atr


async def fetch_market_snapshot_from_binance(
    ex: ccxt.Exchange, settings: Settings
) -> MarketSnapshot:
    ts = time.time()

    main_symbol = settings.symbol
    main_symbol_ccxt = main_symbol
    symbol_key = main_symbol.replace("/", "")

    # Fetch enough 5m candles so that downsampled 1h closes can cover EMA trend.
    # Note: downsample drops the current/incomplete 1h bucket, so add a small buffer.
    min_limit_5m = 200
    ema_period_1h = int(getattr(settings, "ema_trend_period_1h", 50) or 50)
    limit_5m = max(min_limit_5m, (ema_period_1h + 2) * 12)

    # Fetch OHLCV for main symbol on 5m timeframe, enough for RSI / MA / candle patterns
    ohlcv_main = _fetch_ohlcv(ex, main_symbol_ccxt, "5m", limit=limit_5m)
    if not ohlcv_main:
        raise RuntimeError(f"Không lấy được dữ liệu OHLCV cho {main_symbol_ccxt}")

    last_close = ohlcv_main[-1][4]

    change_5m_main = _compute_change_pct_from_ohlcv(ohlcv_main, bars=1)
    change_15m_main = _compute_change_pct_from_ohlcv(ohlcv_main, bars=3)
    # 1h = 12 cây 5m
    change_1h_main = _compute_change_pct_from_ohlcv(ohlcv_main, bars=12)
    vol_5m_main, vol_15m_main, vol_avg_1h_main, atr_main = _compute_volume_and_atr(
        ohlcv_main
    )

    prices: Dict[str, float] = {symbol_key: last_close}
    changes_5m: Dict[str, float] = {}
    changes_15m: Dict[str, float] = {}
    changes_1h: Dict[str, float] = {}
    volumes: Dict[str, float] = {}
    atrs: Dict[str, float] = {}

    if change_5m_main is not None:
        changes_5m[symbol_key] = change_5m_main
    if change_15m_main is not None:
        changes_15m[symbol_key] = change_15m_main
    if change_1h_main is not None:
        changes_1h[symbol_key] = change_1h_main

    volumes[symbol_key] = vol_5m_main
    volumes[f"{symbol_key}_15M"] = vol_15m_main
    volumes[f"{symbol_key}_AVG1H"] = vol_avg_1h_main
    atrs[symbol_key] = atr_main

    # BTCUSDT as reference
    try:
        ohlcv_btc = _fetch_ohlcv(ex, "BTC/USDT", "5m", limit=5)
        if ohlcv_btc:
            btc_last = ohlcv_btc[-1][4]
            prices["BTCUSDT"] = btc_last
            change_5m_btc = _compute_change_pct_from_ohlcv(ohlcv_btc, bars=1)
            if change_5m_btc is not None:
                changes_5m["BTCUSDT"] = change_5m_btc
    except Exception as e:
        logger.warning("Không lấy được BTCUSDT: %s", e)

    # SOLBTC as optional reference when trading SOL
    try:
        ohlcv_solbtc = _fetch_ohlcv(ex, "SOL/BTC", "5m", limit=5)
        if ohlcv_solbtc:
            solbtc_last = ohlcv_solbtc[-1][4]
            prices["SOLBTC"] = solbtc_last
            change_5m_solbtc = _compute_change_pct_from_ohlcv(ohlcv_solbtc, bars=1)
            if change_5m_solbtc is not None:
                changes_5m["SOLBTC"] = change_5m_solbtc
    except Exception:
        # Không bắt buộc, chỉ là filter thêm
        pass

    return MarketSnapshot(
        ts=ts,
        prices=prices,
        changes_5m=changes_5m,
        changes_15m=changes_15m,
        changes_1h=changes_1h,
        volumes=volumes,
        atrs=atrs,
        ohlcv_5m=ohlcv_main,
    )


async def main_loop() -> None:
    cfg = load_config_required()
    telegram_cfg = cfg.get("telegram") or {}
    token = telegram_cfg.get("bot_token")
    chat_id_raw = telegram_cfg.get("chat_id")
    if not token:
        raise RuntimeError("Thiếu telegram.bot_token trong config.yaml")
    if not chat_id_raw:
        raise RuntimeError("Thiếu telegram.chat_id trong config.yaml")
    chat_id = int(chat_id_raw)

    settings = Settings.from_config(cfg)
    app = build_application(token=token, settings=settings)
    strategy_cfg = cfg.get("strategy") or {}
    data_log_path = strategy_cfg.get("data_log_path") or "data.txt"
    retention_days = int(strategy_cfg.get("data_retention_days") or 3)
    data_logger = DataLogger(path=str(data_log_path), retention_days=retention_days)

    async with app:
        await app.start()

        settings = Settings.from_config(cfg)
        gstate = GlobalState.load(settings)

        poll_interval = load_config_poll_interval(default_seconds=30)

        # Khởi tạo Binance client với API key đọc từ config.yaml (binance_read)
        binance_cfg = cfg.get("binance_read") or {}
        if not isinstance(binance_cfg, dict):
            raise RuntimeError("binance_read trong config.yaml phải là object.")
        ex_cfg = _build_binance_exchange_config(binance_cfg)
        ex = ccxt.binance(ex_cfg)
        symbols = settings.effective_symbols()
        health_symbol = symbols[0] if symbols else settings.symbol
        _binance_startup_healthcheck(ex, symbol=health_symbol)
        # Gửi thông báo khởi động tới chat
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                "[agent_eth] Bot V3-light đã khởi động.\n"
                f"Thời gian UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}\n"
                f"Symbols: {', '.join(symbols)}, poll={poll_interval}s, data=Binance."
            ),
        )
        logger.info(
            "agent_eth V3-light loop started (symbols=%s, poll=%ss, data=Binance).",
            ",".join(symbols),
            poll_interval,
        )

        try:
            while True:
                cfg = load_config_required()
                settings = Settings.from_config(cfg)
                strategy_cfg = cfg.get("strategy") or {}
                symbols = settings.effective_symbols()
                max_open_positions = int(strategy_cfg.get("max_open_positions") or 3)
                table_rows: List[Dict[str, str]] = []
                gstate = GlobalState.load(settings)
                scan_rows: List[ScanRow] = []
                candidates: List[Dict[str, object]] = []

                for sym in symbols:
                    sym_settings = Settings.from_config(cfg)
                    sym_settings.symbol = sym
                    mkt = await fetch_market_snapshot_from_binance(ex, sym_settings)

                    symbol_key = sym_settings.symbol.replace("/", "")
                    price_now = mkt.prices.get(symbol_key)
                    change_5m_main = mkt.changes_5m.get(symbol_key)
                    change_1h_main = mkt.changes_1h.get(symbol_key)

                    table_rows.append(
                        {
                            "symbol": sym_settings.symbol,
                            "price": _fmt_num(price_now, digits=4),
                            "chg5m": f"{_fmt_num(change_5m_main, digits=3)}%" if change_5m_main is not None else "n/a",
                            "chg1h": f"{_fmt_num(change_1h_main, digits=3)}%" if change_1h_main is not None else "n/a",
                        }
                    )
                    # Keep per-symbol logs quiet; table summary is logged once per scan cycle.

                    # data.txt diagnostics (even when gated), to evaluate "missed trades"
                    diag = scan_diagnostics(sym_settings, mkt)
                    gated = "OK"
                    if not gstate.auto_trade_enabled:
                        gated = "ENTRY_OFF"
                    elif bool((gstate.positions.get(sym) or {}).get("has_position")):
                        gated = "HAS_POSITION"
                    scan_rows.append(
                        ScanRow(
                            ts_utc=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(mkt.ts)),
                            symbol=sym_settings.symbol,
                            price=diag.get("price"),
                            chg_5m=diag.get("chg5m"),
                            chg_1h=diag.get("chg1h"),
                            rsi_5m=diag.get("rsi5m"),
                            vol_spike=diag.get("vol_spike"),
                            trend_strong=diag.get("trend_strong"),
                            dip_score=diag.get("dip_score"),
                            breakout_score=diag.get("breakout_score"),
                            best_kind=diag.get("best_kind"),
                            best_score=diag.get("best_score"),
                            passed=bool(diag.get("passed")),
                            reason=str(diag.get("best_reason") or ""),
                            gated=gated,
                        )
                    )

                    # Candidate picking (multi-position): evaluate per symbol state.
                    sym_state = _position_to_runtime_state(gstate, sym)
                    if gstate.auto_trade_enabled and gstate.open_positions_count() < max_open_positions:
                        proposal = build_buy_proposal(sym_settings, sym_state, mkt)
                        if proposal:
                            score = score_buy_signal(sym_settings, mkt) or 0.0
                            if float(score) >= float(sym_settings.entry_score_min):
                                candidates.append(
                                    {
                                        "proposal": proposal,
                                        "score": float(score),
                                        "sym": sym,
                                    }
                                )
                        # Keep daily/risk flags in sync if strategy updated them.
                        _runtime_state_to_position(gstate, sym, sym_state)
                        gstate.save(GLOBAL_STATE_PATH)

                # Position management (MULTI): sync trades + TP/SL/TIME per open symbol.
                open_symbols = [
                    sym for sym, pos in gstate.positions.items() if bool((pos or {}).get("has_position"))
                ]
                for open_sym in open_symbols:
                    active_settings = Settings.from_config(cfg)
                    active_settings.symbol = open_sym
                    runtime_state = _position_to_runtime_state(gstate, open_sym)

                    async def notify_message(text: str) -> None:
                        await app.bot.send_message(chat_id=chat_id, text=text)

                    await sync_position_from_binance_trades(
                        ex, active_settings, runtime_state, notify_message=notify_message
                    )
                    _runtime_state_to_position(gstate, open_sym, runtime_state)
                    gstate.save(GLOBAL_STATE_PATH)

                    if runtime_state.has_position:
                        mkt_active = await fetch_market_snapshot_from_binance(ex, active_settings)
                        symbol_key = active_settings.symbol.replace("/", "")
                        price_now = mkt_active.prices.get(symbol_key)
                        if price_now is not None:
                            alerts = check_tp_sl_time_stop(active_settings, runtime_state, price_now, mkt_active.ts)
                            pnl_pct = alerts.get("pnl_pct")
                            if alerts.get("tp_alert") and pnl_pct is not None:
                                logger.info("TP alert %s at %.2f%%", active_settings.symbol, pnl_pct)
                                await send_tp_sl_time_stop_message(app, chat_id, active_settings.symbol, "TP", pnl_pct)
                                runtime_state.tp_alert_sent = True
                            if alerts.get("sl_alert") and pnl_pct is not None:
                                logger.info("SL alert %s at %.2f%%", active_settings.symbol, pnl_pct)
                                await send_tp_sl_time_stop_message(app, chat_id, active_settings.symbol, "SL", pnl_pct)
                                runtime_state.sl_alert_sent = True
                            if alerts.get("time_stop_alert") and pnl_pct is not None:
                                logger.info("TIME-STOP alert %s at %.2f%%", active_settings.symbol, pnl_pct)
                                await send_tp_sl_time_stop_message(app, chat_id, active_settings.symbol, "TIME", pnl_pct)
                                runtime_state.time_stop_alert_sent = True
                        _runtime_state_to_position(gstate, open_sym, runtime_state)
                        gstate.save(GLOBAL_STATE_PATH)

                # Emit multiple proposals globally + per symbol (multi-position mode).
                if (
                    gstate.auto_trade_enabled
                    and candidates
                    and gstate.proposals_sent_today < int(settings.proposals_limit_global)
                ):
                    limit_global = int(settings.proposals_limit_global)
                    limit_per_symbol = int(settings.proposals_limit_per_symbol)
                    ttl_seconds = int(settings.proposal_ttl_seconds)

                    # Send in score order: highest score first.
                    candidates_sorted = sorted(candidates, key=lambda c: float(c["score"]), reverse=True)  # type: ignore[index]
                    for cand in candidates_sorted:
                        if gstate.proposals_sent_today >= limit_global:
                            break
                        if gstate.open_positions_count() >= max_open_positions:
                            break

                        proposal = cand["proposal"]  # type: ignore[index]
                        sym = str(cand.get("sym") or proposal.symbol)  # type: ignore[union-attr]
                        if bool((gstate.positions.get(sym) or {}).get("has_position")):
                            continue

                        current_per_sym = int(gstate.proposals_sent_per_symbol.get(sym, 0))
                        if current_per_sym >= limit_per_symbol:
                            continue

                        # Track "pending" for UI only; we no longer block sending on pending.
                        gstate.pending_proposal_id = proposal.id
                        gstate.pending_proposal_symbol = proposal.symbol
                        gstate.pending_proposal_ts = time.time()
                        gstate.save(GLOBAL_STATE_PATH)

                        await send_buy_proposal_message(app, chat_id, proposal)  # type: ignore[arg-type]
                        gstate.proposals_sent_today += 1
                        gstate.proposals_sent_per_symbol[sym] = current_per_sym + 1
                        gstate.save(GLOBAL_STATE_PATH)

                        async def _expire(proposal_id: str, delay_s: int) -> None:
                            await asyncio.sleep(delay_s)
                            expire_pending_proposal(proposal_id)

                        asyncio.create_task(_expire(proposal.id, ttl_seconds))

                        logger.info(
                            "SENT BUY proposal: %s score=%.4f (sent_today=%d/%d, per_sym=%d/%d)",
                            sym,
                            float(cand["score"]),  # type: ignore[index]
                            gstate.proposals_sent_today,
                            limit_global,
                            gstate.proposals_sent_per_symbol.get(sym, 0),
                            limit_per_symbol,
                        )

                # One compact table per scan cycle (all symbols)
                if table_rows:
                    logger.info("Market snapshot (last scan):\n%s", _render_market_table(table_rows))

                # Persist scan rows to data.txt (rolling retention by day)
                try:
                    data_logger.append_scan(scan_rows)
                except Exception as e:
                    logger.warning("data_logger append failed: %s", e)

                await asyncio.sleep(poll_interval)
        finally:
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main_loop())

