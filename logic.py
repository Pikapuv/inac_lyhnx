from dataclasses import dataclass
from typing import List, Optional
import time


@dataclass
class PricePoint:
    ts: float
    price: float


@dataclass
class Signal:
    kind: str  # "buy"
    message: str


def compute_change(history: List[PricePoint], window_min: float) -> Optional[float]:
    if not history:
        return None
    now = time.time()
    cutoff = now - window_min * 60
    past = [p for p in history if p.ts <= cutoff]
    if not past:
        return None
    past_price = past[-1].price
    last_price = history[-1].price
    if past_price <= 0:
        return None
    return (last_price - past_price) / past_price * 100.0


def build_buy_signal(
    price_now: float,
    change_short: float,
    change_long: float,
    capital_usdt: float,
    position_pct: float,
    tp_min: float,
    tp_max: float,
    sl_pct: float,
) -> Signal:
    size_usdt = capital_usdt * position_pct
    size_eth = size_usdt / price_now
    entry_avg = price_now
    tp1 = entry_avg * (1 + tp_min / 100.0)
    tp2 = entry_avg * (1 + tp_max / 100.0)
    sl = entry_avg * (1 - sl_pct / 100.0)

    msg = (
        f"[lynhx_bot – TÍN HIỆU MUA]\\n"
        f"Giá hiện tại: {price_now:.2f} USDT\\n"
        f"Thay đổi ngắn: {change_short:.2f}% | dài: {change_long:.2f}%\\n\\n"
        f"Vốn tham chiếu: {capital_usdt:.2f} USDT\\n"
        f"Size gợi ý (25% vốn): {size_usdt:.2f} USDT (~{size_eth:.6f} ETH)\\n\\n"
        f"Kế hoạch giá (ước tính):\\n"
        f"- Entry quanh: {entry_avg:.2f} USDT\\n"
        f"- TP1 (+{tp_min:.0f}%): {tp1:.2f} USDT\\n"
        f"- TP2 (+{tp_max:.0f}%): {tp2:.2f} USDT\\n"
        f"- SL (-{sl_pct:.0f}%): {sl:.2f} USDT\\n"
    )
    return Signal(kind="buy", message=msg)
