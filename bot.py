import time
import yaml
from pathlib import Path

import requests

from exchange import PublicClient
from logic import PricePoint, compute_change, build_buy_signal
from notifier import notify_stdout, notify_telegram


def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Config file {path} not found. Copy config.example.yaml to config.yaml and edit it."
        )
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def main():
    cfg = load_config()

    scfg = cfg["strategy"]
    rcfg = cfg["risk"]
    tcfg = cfg.get("telegram", {})

    symbol = scfg.get("symbol", "ETH/USDT")
    poll_interval = float(scfg.get("poll_interval_sec", 60))
    window_short = float(scfg.get("window_short_min", 5))
    window_long = float(scfg.get("window_long_min", 15))
    dump_th = float(scfg.get("dump_threshold_pct", -1.5))

    capital = float(rcfg.get("capital_usdt", 20))
    position_pct = float(rcfg.get("position_pct", 0.25))
    sl_pct = float(rcfg.get("max_loss_pct_per_trade", 2.0))
    tp_min = float(rcfg.get("target_profit_pct_min", 1.0))
    tp_max = float(rcfg.get("target_profit_pct_max", 2.0))

    tg_enabled = bool(tcfg.get("enabled", False))
    tg_token = tcfg.get("bot_token", "")
    tg_chat_id = tcfg.get("chat_id", "")

    client = PublicClient()

    history: list[PricePoint] = []
    print("[agent_eth] Started (ETH price via CoinGecko). Ctrl+C to stop.")

    # Startup notification
    startup_msg = (
        "[agent_eth] Bot V1 đã khởi động\n"
        "Nguồn giá: CoinGecko (ETH/USD ≈ ETH/USDT)\n"
        f"Chu kỳ quét: {poll_interval:.0f}s/lần\n"
        f"Rule: vốn {capital:.2f} USDT, {capital*position_pct:.2f} USDT/lệnh, "
        f"TP {tp_min:.1f}-{tp_max:.1f}%, SL {sl_pct:.1f}%, giới hạn ±{rcfg.get('daily_pnl_limit_pct', 3.0):.1f}%/ngày"
    )
    notify_stdout(startup_msg)
    if tg_enabled:
        notify_telegram(tg_token, tg_chat_id, startup_msg)

    # Simple backoff for rate limits
    backoff_until = 0.0

    while True:
        try:
            now = time.time()
            if now < backoff_until:
                time.sleep(max(1.0, backoff_until - now))
                continue

            try:
                ticker = client.get_ticker(symbol)
            except requests.HTTPError as http_err:
                if http_err.response is not None and http_err.response.status_code == 429:
                    # Rate limited by CoinGecko
                    msg = (
                        "[agent_eth] Rate limit CoinGecko (429). "
                        "Tạm dừng 120s rồi thử lại."
                    )
                    notify_stdout(msg)
                    if tg_enabled:
                        notify_telegram(tg_token, tg_chat_id, msg)
                    backoff_until = time.time() + 120
                    continue
                else:
                    raise

            price = ticker["last"]
            ts = time.time()
            history.append(PricePoint(ts=ts, price=price))
            cutoff = ts - 30 * 60
            history[:] = [p for p in history if p.ts >= cutoff]

            change_short = compute_change(history, window_short)
            change_long = compute_change(history, window_long)

            if change_short is not None and change_long is not None:
                if change_short <= dump_th:
                    sig = build_buy_signal(
                        price_now=price,
                        change_short=change_short,
                        change_long=change_long,
                        capital_usdt=capital,
                        position_pct=position_pct,
                        tp_min=tp_min,
                        tp_max=tp_max,
                        sl_pct=sl_pct,
                    )
                    notify_stdout(sig.message)
                    if tg_enabled:
                        notify_telegram(tg_token, tg_chat_id, sig.message)

        except KeyboardInterrupt:
            print("\n[agent_eth] Stopped by user.")
            break
        except Exception as e:
            print(f"[agent_eth] Error: {e}")

        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
