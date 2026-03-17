import ccxt
from typing import Dict, List
from datetime import datetime, timezone


class BinanceReadClient:
    def __init__(self, api_key: str, secret: str):
        self.ex = ccxt.binance({
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
        })

    def get_spot_balance(self) -> Dict[str, float]:
        """Return free spot balances for ETH and USDT."""
        bal = self.ex.fetch_balance()
        eth = float(bal.get("free", {}).get("ETH", 0.0))
        usdt = float(bal.get("free", {}).get("USDT", 0.0))
        return {"ETH": eth, "USDT": usdt}

    def get_today_trades_ethusdt(self) -> List[Dict]:
        """Return today's trades for ETH/USDT (UTC-based)."""
        symbol = "ETH/USDT"
        now = datetime.now(timezone.utc)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        since_ms = int(start_of_day.timestamp() * 1000)
        trades = self.ex.fetch_my_trades(symbol, since=since_ms)
        return trades
