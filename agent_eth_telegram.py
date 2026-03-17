from __future__ import annotations

import logging
import os
from typing import Dict

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from agent_eth_settings import Settings
from agent_eth_state import State
from agent_eth_strategy import BuyProposal


logger = logging.getLogger(__name__)


PENDING_PROPOSALS: Dict[str, BuyProposal] = {}


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("agent_eth V3-light sẵn sàng.")


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = Settings.load()
    text = (
        "[agent_eth – SETTINGS]\n"
        f"Vốn ban đầu: {settings.initial_capital_usdt:.2f} USDT\n"
        f"Giới hạn ngày: ±{settings.daily_limit_pct:.1f}% "
        f"(= ±{settings.daily_limit_usdt:.2f} USDT)\n"
        f"Max lệnh/ngày: {settings.max_trades_per_day}\n"
        f"TP: {settings.tp_pct_min:.1f}–{settings.tp_pct_max:.1f}%\n"
        f"SL: {settings.sl_pct:.1f}%\n"
        f"Dump threshold: {settings.dump_threshold_pct:.1f}%\n"
    )

    keyboard = [
        [
            InlineKeyboardButton("⚙ Vốn ban đầu", callback_data="SET:C0"),
            InlineKeyboardButton("⚙ Giới hạn ngày", callback_data="SET:DAILY"),
        ],
        [
            InlineKeyboardButton("⚙ TP/SL", callback_data="SET:TPSL"),
            InlineKeyboardButton("⚙ Dump threshold", callback_data="SET:DUMP"),
        ],
        [
            InlineKeyboardButton("⚙ Max lệnh/ngày", callback_data="SET:MAXTRADES"),
        ],
    ]

    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def handle_settings_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "UI chỉnh settings chi tiết sẽ được bổ sung sau (V3-full)."
    )


async def handle_buy_proposal_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    data = query.data or ""
    await query.answer()

    action, _, proposal_id = data.partition(":")
    proposal = PENDING_PROPOSALS.get(proposal_id)

    if not proposal:
        await query.edit_message_text("Proposal đã hết hiệu lực.")
        return

    state = State.load(Settings.load())

    if action == "ENTER":
        state.has_position = True
        state.entry_price = proposal.price
        state.position_open_time = proposal.ts
        state.size_usdt = proposal.size_usdt
        state.size_coin = proposal.size_coin
        state.tp_alert_sent = False
        state.sl_alert_sent = False
        state.trades_opened += 1
        state.save()

        await query.edit_message_text(
            proposal.to_message() + "\n\n[ENTER] Đã vào lệnh (thực hiện tay trên Binance)."
        )
    elif action == "SKIP":
        await query.edit_message_text(
            proposal.to_message() + "\n\n[SKIP] Đã bỏ qua cơ hội này."
        )

    PENDING_PROPOSALS.pop(proposal_id, None)


async def handle_position_decision_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    data = query.data or ""
    await query.answer()

    action, _, _rest = data.partition(":")

    state = State.load(Settings.load())

    if action in {"TP_OK", "SL_OK", "TIME_OK"}:
        state.has_position = False
        state.entry_price = None
        state.position_open_time = None
        state.size_usdt = None
        state.size_coin = None
        state.tp_alert_sent = False
        state.sl_alert_sent = False
        state.trades_closed += 1
        state.save()
        await query.edit_message_text(
            "Đã ghi nhận đóng lệnh (bạn tự thực hiện trên Binance)."
        )
    else:
        await query.edit_message_text("Đã ghi nhận giữ lệnh.")


def build_application() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))

    app.add_handler(
        CallbackQueryHandler(handle_settings_callback, pattern=r"^SET:")
    )
    app.add_handler(
        CallbackQueryHandler(handle_buy_proposal_callback, pattern=r"^(ENTER|SKIP):")
    )
    app.add_handler(
        CallbackQueryHandler(
            handle_position_decision_callback, pattern=r"^(TP_|SL_|TIME_)"
        )
    )

    return app


async def send_buy_proposal_message(
    app: Application, chat_id: int, proposal: BuyProposal
) -> None:
    PENDING_PROPOSALS[proposal.id] = proposal

    keyboard = [
        [
            InlineKeyboardButton("✅ Vào lệnh", callback_data=f"ENTER:{proposal.id}"),
            InlineKeyboardButton("❌ Bỏ qua", callback_data=f"SKIP:{proposal.id}"),
        ]
    ]

    await app.bot.send_message(
        chat_id=chat_id,
        text=proposal.to_message(),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def send_tp_sl_time_stop_message(
    app: Application,
    chat_id: int,
    kind: str,
    pnl_pct: float,
) -> None:
    if kind == "TP":
        text = f"CÂN NHẮC CHỐT LỜI – PnL: {pnl_pct:.2f}%"
        keyboard = [
            [
                InlineKeyboardButton("✅ Chốt lời", callback_data="TP_OK:1"),
                InlineKeyboardButton("❌ Giữ tiếp", callback_data="TP_SKIP:1"),
            ]
        ]
    elif kind == "SL":
        text = f"CÂN NHẮC CẮT LỖ – PnL: {pnl_pct:.2f}%"
        keyboard = [
            [
                InlineKeyboardButton("✅ Cắt lỗ", callback_data="SL_OK:1"),
                InlineKeyboardButton("❌ Giữ thêm", callback_data="SL_SKIP:1"),
            ]
        ]
    else:
        text = "TIME-STOP: Lệnh đã đi ngang quá lâu quanh hòa vốn."
        keyboard = [
            [
                InlineKeyboardButton("✅ Đóng lệnh", callback_data="TIME_OK:1"),
                InlineKeyboardButton("❌ Bỏ qua", callback_data="TIME_SKIP:1"),
            ]
        ]

    await app.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

