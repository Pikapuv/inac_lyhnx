from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Dict

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from agent_eth_settings import Settings
from agent_eth_global_state import GlobalState, GLOBAL_STATE_PATH
from agent_eth_strategy import BuyProposal


logger = logging.getLogger(__name__)


PENDING_PROPOSALS: Dict[str, BuyProposal] = {}


def expire_pending_proposal(proposal_id: str) -> None:
    """Remove a pending BUY proposal so user cannot click it later."""
    PENDING_PROPOSALS.pop(proposal_id, None)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = Settings.load()
    now_utc = datetime.now(timezone.utc)
    st = GlobalState.load(settings)
    st.auto_trade_enabled = True
    st.save(GLOBAL_STATE_PATH)

    text = (
        "[agent_eth] Dashboard\n"
        f"Thời gian UTC: {now_utc:%Y-%m-%d %H:%M:%S}\n"
        f"Symbols: {', '.join(settings.effective_symbols())}\n"
        f"C0: {settings.initial_capital_usdt:.2f} USDT | Daily limit: ±{settings.daily_limit_usdt:.2f}\n"
        f"Entry: {'ON' if st.auto_trade_enabled else 'OFF'} | Pos: {'YES' if st.has_position else 'NO'}"
    )
    keyboard = [
        [
            InlineKeyboardButton(
                "⏸️ Pause entry",
                callback_data="ENTRY:OFF",
            ),
            InlineKeyboardButton(
                "▶️ Resume entry",
                callback_data="ENTRY:ON",
            ),
        ]
    ]
    await update.message.reply_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "[agent_eth] Commands\n"
        "/start: Resume entry (bật tạo tín hiệu vào lệnh)\n"
        "/stopentry: Pause entry (tạm ngừng tạo tín hiệu vào lệnh)\n"
        "/status: Xem trạng thái bot + P&L ngày\n"
        "/settings: Chỉnh TP/SL/dump threshold/max trades/C0\n"
        "/config: Xem cấu hình hiện tại (chi tiết)\n"
    )
    await update.message.reply_text(text)


async def cmd_stopentry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = Settings.load()
    st = GlobalState.load(settings)
    st.auto_trade_enabled = False
    st.pending_proposal_id = None
    st.pending_proposal_symbol = None
    st.pending_proposal_ts = None
    st.save(GLOBAL_STATE_PATH)

    now_utc = datetime.now(timezone.utc)
    await update.message.reply_text(
        f"[agent_eth] Đã pause entry.\nUTC: {now_utc:%Y-%m-%d %H:%M:%S}"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = Settings.load()
    st = GlobalState.load(settings)
    now_utc = datetime.now(timezone.utc)
    entry_mode = "ON" if st.auto_trade_enabled else "OFF"
    active = st.active_symbol or "n/a"
    pending = st.pending_proposal_symbol or "n/a"
    text = (
        "[agent_eth – STATUS]\n"
        f"UTC now: {now_utc:%Y-%m-%d %H:%M:%S}\n"
        f"Symbols: {', '.join(settings.effective_symbols())}\n"
        f"Entry mode: {entry_mode}\n"
        f"Active position: {'YES' if st.has_position else 'NO'} ({active})\n"
        f"Pending proposal: {'YES' if st.pending_proposal_id else 'NO'} ({pending})\n"
        f"Daily P&L: {st.pnl_day_usdt:.4f} USDT (limit ±{st.daily_limit_usdt:.4f})\n"
        f"Trades opened/closed: {st.trades_opened}/{st.trades_closed}\n"
    )

    await update.message.reply_text(text)


async def handle_entry_toggle(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    data = query.data or ""
    await query.answer()

    _, _, value = data.partition(":")
    settings = Settings.load()
    st = GlobalState.load(settings)

    if value == "ON":
        msg = "Đã resume entry (bật tạo tín hiệu BUY)."
    else:
        msg = "Đã pause entry (tắt tạo tín hiệu BUY)."

    st.auto_trade_enabled = (value == "ON")
    if not st.auto_trade_enabled:
        st.pending_proposal_id = None
        st.pending_proposal_symbol = None
        st.pending_proposal_ts = None
        PENDING_PROPOSALS.clear()
    st.save(GLOBAL_STATE_PATH)
    await query.edit_message_text(
        f"[agent_eth] {msg}", reply_markup=None
    )


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = Settings.load()
    now_utc = datetime.now(timezone.utc)

    text = (
        "[agent_eth – CONFIG]\n"
        f"UTC now: {now_utc:%Y-%m-%d %H:%M:%S}\n"
        f"Symbols: {', '.join(settings.effective_symbols())}\n"
        f"Vốn ban đầu (C0): {settings.initial_capital_usdt:.2f} USDT\n"
        f"Giới hạn ngày: ±{settings.daily_limit_pct:.1f}% "
        f"(= ±{settings.daily_limit_usdt:.2f} USDT)\n"
        f"Max lệnh/ngày: {settings.max_trades_per_day}\n"
        f"TP: {settings.tp_pct_min:.1f}–{settings.tp_pct_max:.1f}% | "
        f"SL: {settings.sl_pct:.1f}%\n"
        f"Dump threshold 5m: {settings.dump_threshold_pct:.2f}%\n"
    )

    await update.message.reply_text(text)


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
        f"Dump threshold 5m: {settings.dump_threshold_pct:.1f}%\n"
        f"Dump threshold 1h: {settings.dump_threshold_1h_pct:.1f}%\n"
    )

    keyboard = [
        [
            InlineKeyboardButton("⚙ Vốn ban đầu", callback_data="SET:C0"),
            InlineKeyboardButton("⚙ Giới hạn ngày", callback_data="SET:DAILY"),
        ],
        [
            InlineKeyboardButton("⚙ TP/SL", callback_data="SET:TPSL"),
            InlineKeyboardButton("⚙ Dump threshold 5m", callback_data="SET:DUMP"),
            InlineKeyboardButton("⚙ Dump threshold 1h", callback_data="SET:DUMP1H"),
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

    _, _, key = (query.data or "").partition(":")
    context.user_data["await_setting"] = key

    if key == "C0":
        prompt = "Nhập **vốn ban đầu mới (USDT)**, ví dụ: `25`."
    elif key == "DAILY":
        prompt = (
            "Nhập **giới hạn P&L/ngày (%)** mới, ví dụ: `3` cho ±3%."
        )
    elif key == "TPSL":
        prompt = (
            "Nhập `TP_min TP_max SL` (đơn vị %), ví dụ: `1 2 2`."
        )
    elif key == "DUMP":
        prompt = (
            "Nhập **dump threshold 5m (%)** mới (âm), ví dụ: `-0.3`."
        )
    elif key == "DUMP1H":
        prompt = (
            "Nhập **dump threshold 1h (%)** mới (âm), ví dụ: `-1.0`."
        )
    elif key == "MAXTRADES":
        prompt = (
            "Nhập **max lệnh/ngày** mới (số nguyên), ví dụ: `3`."
        )
    else:
        prompt = "Không nhận diện được tuỳ chọn, hãy chọn lại trong /settings."

    await query.edit_message_text(prompt)


async def handle_setting_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    key = context.user_data.get("await_setting")
    if not key:
        return

    text = (update.message.text or "").strip()
    settings = Settings.load()
    msg_ok = ""

    try:
        if key == "C0":
            value = float(text)
            settings.initial_capital_usdt = value
            msg_ok = f"Đã cập nhật **vốn ban đầu** = {value:.2f} USDT."
        elif key == "DAILY":
            value = float(text)
            settings.daily_limit_pct = value
            msg_ok = f"Đã cập nhật **giới hạn ngày** = ±{value:.1f}%."
        elif key == "TPSL":
            parts = text.replace(",", " ").split()
            if len(parts) != 3:
                raise ValueError("Cần đúng 3 số: TP_min TP_max SL.")
            tp_min, tp_max, sl = map(float, parts)
            settings.tp_pct_min = tp_min
            settings.tp_pct_max = tp_max
            settings.sl_pct = sl
            msg_ok = (
                f"Đã cập nhật **TP/SL**: TP = {tp_min:.1f}–{tp_max:.1f}%, "
                f"SL = {sl:.1f}%."
            )
        elif key == "DUMP":
            value = float(text)
            settings.dump_threshold_pct = value
            msg_ok = f"Đã cập nhật **dump threshold 5m** = {value:.2f}%."
        elif key == "DUMP1H":
            value = float(text)
            settings.dump_threshold_1h_pct = value
            msg_ok = f"Đã cập nhật **dump threshold 1h** = {value:.2f}%."
        elif key == "MAXTRADES":
            value = int(text)
            settings.max_trades_per_day = value
            msg_ok = f"Đã cập nhật **max lệnh/ngày** = {value}."
        else:
            await update.message.reply_text(
                "Không nhận diện được tuỳ chọn đang chỉnh. Hãy dùng lại /settings."
            )
            context.user_data.pop("await_setting", None)
            return
    except ValueError as e:
        await update.message.reply_text(f"Giá trị không hợp lệ: {e}")
        return

    settings.save()
    context.user_data.pop("await_setting", None)

    await update.message.reply_text(msg_ok)


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

    settings = Settings.load()
    st = GlobalState.load(settings)

    if action == "ENTER":
        if st.has_position:
            await query.edit_message_text("Bot đang có 1 vị thế mở. Không thể ENTER thêm.")
            return
        st.has_position = True
        st.active_symbol = proposal.symbol
        st.entry_price = proposal.price
        st.position_open_time = proposal.ts
        st.size_usdt = proposal.size_usdt
        st.size_coin = proposal.size_coin
        st.tp_alert_sent = False
        st.sl_alert_sent = False
        st.time_stop_alert_sent = False
        st.trades_opened += 1
        # Clear global pending
        st.pending_proposal_id = None
        st.pending_proposal_symbol = None
        st.pending_proposal_ts = None
        st.save(GLOBAL_STATE_PATH)

        await query.edit_message_text(
            proposal.to_message() + "\n\n[ENTER] Đã vào lệnh (thực hiện tay trên Binance)."
        )
    elif action == "SKIP":
        if st.pending_proposal_id == proposal.id:
            st.pending_proposal_id = None
            st.pending_proposal_symbol = None
            st.pending_proposal_ts = None
            st.save(GLOBAL_STATE_PATH)
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

    # data format: ACTION:SYMBOL:VALUE
    parts = data.split(":", 2)
    action = parts[0] if parts else ""
    symbol = parts[1] if len(parts) >= 2 else ""
    value_str = parts[2] if len(parts) >= 3 else ""

    settings = Settings.load()
    st = GlobalState.load(settings)

    if action in {"TP_OK", "SL_OK", "TIME_OK"}:
        if not st.has_position or st.entry_price is None:
            await query.edit_message_text(
                "Hiện tại bot không thấy còn vị thế để đóng (có thể đã được auto-tracking)."
            )
            return
        # Trong chế độ auto-tracking từ Binance:
        # TP_OK/SL_OK/TIME_OK chỉ xác nhận quyết định của bạn, bot sẽ cập nhật PnL khi SELL khớp.
        await query.edit_message_text(
            "Đã ghi nhận quyết định đóng lệnh.\n"
            "Bot sẽ tự theo dõi Binance để cập nhật P&L khi lệnh SELL khớp."
        )
    else:
        await query.edit_message_text("Đã ghi nhận giữ lệnh.")


def build_application() -> Application:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Thiếu biến môi trường TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stopentry", cmd_stopentry))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("config", cmd_config))

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

    app.add_handler(
        CallbackQueryHandler(handle_entry_toggle, pattern=r"^ENTRY:")
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_setting_input,
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
    symbol: str,
    kind: str,
    pnl_pct: float,
) -> None:
    settings = Settings.load()
    if kind == "TP":
        text = f"{symbol} – CÂN NHẮC CHỐT LỜI – PnL: {pnl_pct:.2f}%"
        if settings.close_on_tp_alert:
            # Winrate-first: ưu tiên chốt ngay ở TP, giảm cơ hội để biến thắng thành thua.
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Chốt lời", callback_data=f"TP_OK:{symbol}:{pnl_pct:.4f}"
                    ),
                ]
            ]
        else:
            keyboard = [
                [
                    InlineKeyboardButton(
                        "✅ Chốt lời", callback_data=f"TP_OK:{symbol}:{pnl_pct:.4f}"
                    ),
                    InlineKeyboardButton(
                        "❌ Giữ tiếp",
                        callback_data=f"TP_SKIP:{symbol}:{pnl_pct:.4f}",
                    ),
                ]
            ]
    elif kind == "SL":
        text = f"{symbol} – CÂN NHẮC CẮT LỖ – PnL: {pnl_pct:.2f}%"
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Cắt lỗ", callback_data=f"SL_OK:{symbol}:{pnl_pct:.4f}"
                ),
                InlineKeyboardButton(
                    "❌ Giữ thêm", callback_data=f"SL_SKIP:{symbol}:{pnl_pct:.4f}"
                ),
            ]
        ]
    else:
        text = f"{symbol} – TIME-STOP: Lệnh đã đi ngang quá lâu quanh hòa vốn."
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Đóng lệnh", callback_data=f"TIME_OK:{symbol}:{pnl_pct:.4f}"
                ),
                InlineKeyboardButton(
                    "❌ Bỏ qua", callback_data=f"TIME_SKIP:{symbol}:{pnl_pct:.4f}"
                ),
            ]
        ]

    await app.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )



