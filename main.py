import time
import yaml
from pathlib import Path
from datetime import datetime

import ccxt
import requests

from typing import Dict

from binance_client import BinanceReadClient


# V2 state: simple PnL tracking (skeleton)
pnl_today_usdt = 0.0
last_reset_date: str | None = None

# V1.5 state: vị thế ước tính (dựa trên tín hiệu)
has_position = False
current_entry_price: float | None = None


def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config {path} not found")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def notify_telegram(bot_token: str, chat_id: str, message: str) -> None:
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message})
    except Exception as e:
        print(f"[agent_eth] Telegram error: {e}")


def compute_change(history, window_min: float):
    """Tính % thay đổi giá trong window_min phút gần nhất."""
    if not history:
        return None
    now = time.time()
    cutoff = now - window_min * 60
    past_points = [p for (t0, p) in history if t0 <= cutoff]
    if not past_points:
        return None
    past_price = past_points[-1]
    last_price = history[-1][1]
    if past_price <= 0:
        return None
    return (last_price - past_price) / past_price * 100.0


def main():
    cfg = load_config()
    scfg = cfg["strategy"]
    rcfg = cfg["risk"]
    tcfg = cfg.get("telegram", {})

    symbol = scfg.get("symbol", "ETH/USDT")
    poll_interval = float(scfg.get("poll_interval_sec", 5))

    capital = float(rcfg.get("capital_usdt", 20))
    position_pct = float(rcfg.get("position_pct", 0.25))
    sl_pct = float(rcfg.get("max_loss_pct_per_trade", 2.0))
    tp_min = float(rcfg.get("target_profit_pct_min", 1.0))
    tp_max = float(rcfg.get("target_profit_pct_max", 2.0))

    tg_token = tcfg.get("bot_token", "")
    tg_chat_id = tcfg.get("chat_id", "")

    # Binance public client (price)
    ex = ccxt.binance({"enableRateLimit": True})

    # Binance read client (balances, trades) - V2 skeleton
    bread = BinanceReadClient(
        api_key=cfg["binance_read"]["apiKey"],
        secret=cfg["binance_read"]["secret"],
    )

    startup = (
        "[agent_eth] Bot V1.5/V2 khởi động\n"
        f"Sàn: Binance, cặp: {symbol}\n"
        f"Chu kỳ quét: {poll_interval:.0f}s/lần\n"
        f"Rule: vốn {capital:.2f} USDT, {capital*position_pct:.2f} USDT/lệnh, "
        f"TP {tp_min:.1f}-{tp_max:.1f}%, SL {sl_pct:.1f}%, giới hạn ±{rcfg.get('daily_pnl_limit_pct', 3.0):.1f}%/ngày"
    )
    print(startup)
    notify_telegram(tg_token, tg_chat_id, startup)

    history: list[tuple[float, float]] = []

    global pnl_today_usdt, last_reset_date, has_position, current_entry_price

    while True:
        try:
            # Lấy giá hiện tại
            t: Dict = ex.fetch_ticker(symbol)
            price = float(t["last"])
            ts = time.time()
            history.append((ts, price))
            cutoff = ts - 30 * 60
            history = [(t0, p0) for (t0, p0) in history if t0 >= cutoff]

            # DEBUG: in mỗi lần quét giá
            print(f"[agent_eth] price={price:.2f}")

            # Reset P&L theo ngày (skeleton)
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            if last_reset_date is None or last_reset_date != today_str:
                last_reset_date = today_str
                pnl_today_usdt = 0.0
                has_position = False
                current_entry_price = None

            # Đọc balance spot (V2 skeleton)
            bal = bread.get_spot_balance()
            eth_bal = bal["ETH"]
            usdt_bal = bal["USDT"]

            # Tính % thay đổi 5 phút và 15 phút
            change_short = compute_change(history, 5)
            change_long = compute_change(history, 15)

            # Nếu đã đủ dữ liệu
            if change_short is not None and change_long is not None:
                # --- Nhánh MUA: chỉ khi CHƯA có vị thế ước tính ---
                if (not has_position) and change_short <= -0.3:
                    size_usdt = capital * position_pct
                    size_eth = size_usdt / price
                    entry = price
                    tp1 = entry * (1 + tp_min / 100.0)
                    tp2 = entry * (1 + tp_max / 100.0)
                    sl = entry * (1 - sl_pct / 100.0)

                    msg = (
                        "[agent_eth – TÍN HIỆU MUA]\n"
                        f"Giá hiện tại: {price:.2f} USDT\n"
                        f"Thay đổi 5p: {change_short:.2f}% | 15p: {change_long:.2f}%\n\n"
                        f"Vốn tham chiếu: {capital:.2f} USDT\n"
                        f"Size gợi ý (25% vốn): {size_usdt:.2f} USDT (~{size_eth:.6f} ETH)\n\n"
                        "Kế hoạch giá (V1.5):\n"
                        f"- Entry quanh: {entry:.2f} USDT\n"
                        f"- TP1 (+{tp_min:.0f}%): {tp1:.2f} USDT\n"
                        f"- TP2 (+{tp_max:.0f}%): {tp2:.2f} USDT\n"
                        f"- SL (-{sl_pct:.0f}%): {sl:.2f} USDT\n"
                        f"- Balance hiện tại: {eth_bal:.6f} ETH, {usdt_bal:.2f} USDT\n"
                        f"- PnL hôm nay (ước tính): {pnl_today_usdt:.4f} USDT\n"
                    )
                    print(msg)
                    notify_telegram(tg_token, tg_chat_id, msg)

                    # Cập nhật trạng thái vị thế ước tính
                    has_position = True
                    current_entry_price = entry

                # --- Nhánh CHỐT LỜI: chỉ khi đang có vị thế ước tính ---
                if has_position and current_entry_price is not None:
                    entry = current_entry_price
                    pnl_pct = (price - entry) / entry * 100.0

                    # Nếu đạt TP1 (>= tp_min)
                    if pnl_pct >= tp_min:
                        tp_msg = (
                            "[agent_eth – CÂN NHẮC CHỐT LỜI]\n"
                            f"Entry ước tính: {entry:.2f} USDT\n"
                            f"Giá hiện tại: {price:.2f} USDT\n"
                            f"Lãi ước tính: {pnl_pct:.2f}%\n\n"
                            f"Kế hoạch ban đầu: TP1 {tp_min:.0f}–TP2 {tp_max:.0f}%\n"
                            f"Gợi ý: cân nhắc đặt lệnh bán một phần/toàn bộ quanh vùng hiện tại.\n"
                        )
                        print(tp_msg)
                        notify_telegram(tg_token, tg_chat_id, tp_msg)
                        # Lưu ý: V1.5 chỉ nhắc, không reset has_position; bạn tự quyết định khi nào thoát

        except KeyboardInterrupt:
            print("\n[agent_eth] Stopped by user.")
            break
        except Exception as e:
            print(f"[agent_eth] Error: {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
