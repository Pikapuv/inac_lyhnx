from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Dict, List, Tuple

import ccxt
import yaml

from agent_eth_settings import Settings
from agent_eth_state import State
from agent_eth_strategy import (
    MarketSnapshot,
    build_buy_proposal,
    check_tp_sl_time_stop,
)
from agent_eth_telegram import (
    build_application,
    send_buy_proposal_message,
    send_tp_sl_time_stop_message,
    expire_pending_proposal,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger("agent_eth")


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


async def sync_position_from_binance_trades(
    ex: ccxt.Exchange,
    settings: Settings,
    state: State,
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

    try:
        new_trades = ex.fetch_my_trades(settings.symbol, since=since_ms)
    except Exception as e:
        logger.warning("sync_position_from_binance_trades failed: %s", e)
        return

    # Always advance the cursor even if no trades found
    state.last_trade_check_ts = time.time()
    state.save()

    if not new_trades:
        return

    base, quote = _split_symbol(settings.symbol)

    # Sort by timestamp ascending
    new_trades = sorted(new_trades, key=lambda t: t.get("timestamp") or 0)

    # 1) Match BUY trade (entry)
    if state.buy_trade_id is None:
        for tr in new_trades:
            ts_s = (tr.get("timestamp") or 0) / 1000.0
            if ts_s < (state.position_open_time or 0):
                continue
            if tr.get("side") != "buy":
                continue

            state.buy_trade_id = str(tr.get("id"))
            state.entry_price = float(tr.get("price") or 0.0)

            buy_cost = _trade_cost_quote_usdt(tr)
            buy_fee = _trade_fee_quote_usdt(tr)
            state.buy_fee_usdt = buy_fee
            # size_usdt represents quote spent (approx, excluding fee or including it is small)
            state.size_usdt = buy_cost
            state.size_coin = float(tr.get("amount") or 0.0)
            state.save()
            await notify_message(
                f"Đã khớp lệnh BUY trên Binance.\n"
                f"Entry: {state.entry_price:.4f} ({settings.symbol})\n"
                f"UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts_s))}"
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
        if tr.get("side") != "sell":
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
        state.tp_alert_sent = False
        state.sl_alert_sent = False
        state.time_stop_alert_sent = False
        state.trades_closed += 1
        state.save()

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

    # Fetch OHLCV for main symbol on 5m timeframe, enough for RSI / MA / candle patterns
    ohlcv_main = _fetch_ohlcv(ex, main_symbol_ccxt, "5m", limit=200)
    if not ohlcv_main:
        raise RuntimeError(f"Không lấy được dữ liệu OHLCV cho {main_symbol_ccxt}")

    last_close = ohlcv_main[-1][4]

    change_5m_main = _compute_change_pct_from_ohlcv(ohlcv_main, bars=1)
    change_15m_main = _compute_change_pct_from_ohlcv(ohlcv_main, bars=3)
    vol_5m_main, vol_15m_main, vol_avg_1h_main, atr_main = _compute_volume_and_atr(
        ohlcv_main
    )

    prices: Dict[str, float] = {symbol_key: last_close}
    changes_5m: Dict[str, float] = {}
    changes_15m: Dict[str, float] = {}
    volumes: Dict[str, float] = {}
    atrs: Dict[str, float] = {}

    if change_5m_main is not None:
        changes_5m[symbol_key] = change_5m_main
    if change_15m_main is not None:
        changes_15m[symbol_key] = change_15m_main

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
        volumes=volumes,
        atrs=atrs,
        ohlcv_5m=ohlcv_main,
    )


async def main_loop() -> None:
    chat_id_env = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id_env:
        raise RuntimeError("Thiếu biến môi trường TELEGRAM_CHAT_ID")
    chat_id = int(chat_id_env)

    app = build_application()

    async with app:
        await app.start()

        cfg = load_config()
        settings = Settings.load()
        state = State.load(settings)

        poll_interval = load_config_poll_interval(default_seconds=30)

        # Khởi tạo Binance client với API key đọc từ config.yaml (binance_read)
        binance_cfg = cfg.get("binance_read") or {}
        api_key = binance_cfg.get("apiKey") or ""
        secret = binance_cfg.get("secret") or ""
        ex = ccxt.binance(
            {
                "apiKey": api_key,
                "secret": secret,
                "enableRateLimit": True,
            }
        )
        # Gửi thông báo khởi động tới chat
        await app.bot.send_message(
            chat_id=chat_id,
            text=(
                "[agent_eth] Bot V3-light đã khởi động.\n"
                f"Thời gian UTC: {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}\n"
                f"Symbol: {settings.symbol}, poll={poll_interval}s, data=Binance."
            ),
        )
        logger.info(
            "agent_eth V3-light loop started (symbol=%s, poll=%ss, data=Binance).",
            settings.symbol,
            poll_interval,
        )

        try:
            while True:
                settings = Settings.load()
                state = State.load(settings)

                mkt = await fetch_market_snapshot_from_binance(ex, settings)

                # Sync position open/close from Binance trades (auto tracking)
                if state.has_position:
                    async def notify_message(text: str) -> None:
                        await app.bot.send_message(chat_id=chat_id, text=text)

                    await sync_position_from_binance_trades(
                        ex, settings, state, notify_message=notify_message
                    )
                    # Reload state because sync_position may close the position
                    state = State.load(settings)

                # Daily risk lock + notify once
                if (
                    state.auto_trade_enabled
                    and not state.daily_limit_notified
                    and abs(state.pnl_day_usdt) >= state.daily_limit_usdt
                ):
                    state.daily_limit_reached = True
                    state.daily_limit_notified = True
                    state.save()
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "[agent_eth] Chạm giới hạn P&L/ngày.\n"
                            f"P&L ngày: {state.pnl_day_usdt:.4f} USDT "
                            f"(limit ±{state.daily_limit_usdt:.4f}).\n"
                            "Tạm dừng tạo tín hiệu BUY cho tới khi sang ngày mới (UTC)."
                        ),
                    )

                symbol_key = settings.symbol.replace("/", "")
                price_now = mkt.prices.get(symbol_key)
                change_5m_main = mkt.changes_5m.get(symbol_key)
                logger.info(
                    "Tick: %s price=%.4f change_5m=%s",
                    settings.symbol,
                    price_now if price_now is not None else -1.0,
                    f"{change_5m_main:.3f}%" if change_5m_main is not None else "n/a",
                )

                if not state.has_position:
                    ttl_just_expired = False
                    # Nếu đang có pending proposal và chưa hết hạn -> không sinh thêm tín hiệu
                    if (
                        state.pending_proposal_id
                        and state.pending_proposal_ts is not None
                        and time.time() - state.pending_proposal_ts
                        < settings.proposal_ttl_seconds
                    ):
                        await asyncio.sleep(0)  # yield
                        continue

                    # Nếu pending đã hết hạn mà user chưa ENTER/SKIP
                    if state.pending_proposal_id and state.pending_proposal_ts is not None:
                        if (
                            time.time()
                            - state.pending_proposal_ts
                            >= settings.proposal_ttl_seconds
                        ):
                            # Proposal hết hạn: gỡ khỏi bộ nhớ để user không thể ENTER/SKIP muộn
                            expire_pending_proposal(state.pending_proposal_id)
                            await app.bot.send_message(
                                chat_id=chat_id,
                                text=(
                                    f"[agent_eth] Tín hiệu BUY đã qua thời gian chờ "
                                    f"({settings.proposal_ttl_seconds}s). "
                                    f"Kiểm tra lại tín hiệu mới..."
                                ),
                            )
                            state.pending_proposal_id = None
                            state.pending_proposal_ts = None
                            state.save()
                            ttl_just_expired = True

                    # Sinh tín hiệu mới nếu entry mode ON và không còn pending
                    if state.auto_trade_enabled and not state.pending_proposal_id:
                        proposal = build_buy_proposal(settings, state, mkt)
                        if proposal:
                            state.pending_proposal_id = proposal.id
                            state.pending_proposal_ts = time.time()
                            state.save()

                            logger.info(
                                "BUY proposal generated at price %.4f",
                                proposal.price,
                            )
                            await send_buy_proposal_message(app, chat_id, proposal)
                        else:
                            # Không có tín hiệu mới tại thời điểm check
                            if ttl_just_expired:
                                await app.bot.send_message(
                                    chat_id=chat_id,
                                    text="[agent_eth] Không thấy tín hiệu phù hợp tại thời điểm này. Chờ tín hiệu mới...",
                                )
                    elif not state.auto_trade_enabled:
                        logger.info(
                            "Entry paused: skip BUY proposal generation."
                        )

                symbol_key = settings.symbol.replace("/", "")
                price_now = mkt.prices.get(symbol_key)
                if price_now is not None:
                    alerts = check_tp_sl_time_stop(
                        settings, state, price_now, mkt.ts
                    )
                    pnl_pct = alerts.get("pnl_pct")

                    if alerts.get("tp_alert") and pnl_pct is not None:
                        logger.info("TP alert at %.2f%%", pnl_pct)
                        await send_tp_sl_time_stop_message(
                            app, chat_id, "TP", pnl_pct
                        )
                        state.tp_alert_sent = True
                        state.save()

                    if alerts.get("sl_alert") and pnl_pct is not None:
                        logger.info("SL alert at %.2f%%", pnl_pct)
                        await send_tp_sl_time_stop_message(
                            app, chat_id, "SL", pnl_pct
                        )
                        state.sl_alert_sent = True
                        state.save()

                    if alerts.get("time_stop_alert") and pnl_pct is not None:
                        logger.info("TIME-STOP alert at %.2f%%", pnl_pct)
                        await send_tp_sl_time_stop_message(
                            app, chat_id, "TIME", pnl_pct
                        )
                        state.time_stop_alert_sent = True
                        state.save()

                await asyncio.sleep(poll_interval)
        finally:
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main_loop())

