from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Dict

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


async def mock_fetch_market_snapshot(settings: Settings) -> MarketSnapshot:
    ts = time.time()
    base_price = 2300.0
    price = base_price * (1 + random.uniform(-0.01, 0.01))
    prices: Dict[str, float] = {
        settings.symbol.replace("/", ""): price,
        "BTCUSDT": 70000.0,
        "SOLBTC": 0.0005,
    }

    change_5m = random.uniform(-1.5, 1.5)
    changes_5m = {
        settings.symbol.replace("/", ""): change_5m,
        "BTCUSDT": random.uniform(-0.5, 0.5),
        "SOLBTC": random.uniform(-0.5, 0.5),
    }
    changes_15m = {k: v * 1.5 for k, v in changes_5m.items()}

    volumes = {
        settings.symbol.replace("/", ""): random.uniform(10000, 50000),
        f"{settings.symbol.replace('/', '')}_AVG1H": 15000.0,
    }
    atrs = {
        settings.symbol.replace("/", ""): random.uniform(0.5, 5.0),
    }

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

        settings = Settings.load()
        state = State.load(settings)

        logger.info("agent_eth V3-light loop started.")

        try:
            while True:
                settings = Settings.load()
                state = State.load(settings)

                mkt = await mock_fetch_market_snapshot(settings)

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

                await asyncio.sleep(30)
        finally:
            await app.stop()


if __name__ == "__main__":
    asyncio.run(main_loop())

