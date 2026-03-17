import requests
from typing import Dict

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"


class PublicClient:
    def __init__(self):
        # No API key needed for basic CoinGecko simple price endpoint
        pass

    def get_ticker(self, symbol: str) -> Dict[str, float]:
        """Fetch ETH price in USDT-equivalent via CoinGecko.

        We ignore symbol here and always fetch Ethereum in USD,
        treating 1 USD ≈ 1 USDT for spot purposes.
        """
        params = {
            "ids": "ethereum",
            "vs_currencies": "usd",
        }
        resp = requests.get(COINGECKO_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        price = float(data["ethereum"]["usd"])
        # CoinGecko does not provide bid/ask here; use last for all
        return {"last": price, "bid": price, "ask": price}
