import datetime as _dt
import requests
from typing import Optional


def now_str() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")


def notify_stdout(message: str) -> None:
    print("==== ETH SIGNAL ====")
    print(f"[{now_str()}]")
    print(message)
    print("====================")


def notify_telegram(bot_token: str, chat_id: str, message: str) -> Optional[bool]:
    if not bot_token or not chat_id:
        return None
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message})
        return True
    except Exception as e:
        print(f"[TELEGRAM ERROR] {e}")
        return False
