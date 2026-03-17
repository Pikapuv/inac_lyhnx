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
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s – %(message)s",
)
logger = logging.getLogger("agent_eth")


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

    # Fetch OHLCV for main symbol on 5m timeframe, ~60 minutes (12 bars)
    ohlcv_main = _fetch_ohlcv(ex, main_symbol_ccxt, "5m", limit=20)
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

                symbol_key = settings.symbol.replace("/", "")
                price_now = mkt.prices.get(symbol_key)
                change_5m_main = mkt.changes_5m.get(symbol_key)
                logger.info(
                    "Tick: %s price=%.4f change_5m=%s",
                    settings.symbol,
                    price_now if price_now is not None else -1.0,
                    f"{change_5m_main:.3f}%" if change_5m_main is not None else "n/a",
                )

                proposal = build_buy_proposal(settings, state, mkt)
                if proposal:
                    logger.info("BUY proposal generated at price %.4f", proposal.price)
                    await send_buy_proposal_message(app, chat_id, proposal)

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

                await asyncio.sleep(poll_interval)
        finally:
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main_loop())

