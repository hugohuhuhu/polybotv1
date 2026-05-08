from __future__ import annotations

from typing import Any

import httpx


_BINANCE_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "eth": "ETHUSDT",
    "solana": "SOLUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "dogecoin": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
}


def binance_symbol_for_asset(asset: str) -> str | None:
    return _BINANCE_SYMBOLS.get(asset.lower())


class CryptoPriceClient:
    """Small public spot-price client used only for crypto near-close guards."""

    def __init__(self, base_url: str = "https://api.binance.com", timeout: float = 8.0) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_prices(self, symbols: set[str]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol in sorted(symbols):
            response = await self._client.get("/api/v3/ticker/price", params={"symbol": symbol})
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            prices[symbol] = float(payload["price"])
        return prices

    async def get_open_prices_at(self, symbols_by_start_ms: dict[str, int]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for symbol, start_ms in sorted(symbols_by_start_ms.items()):
            response = await self._client.get(
                "/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": start_ms,
                    "limit": 1,
                },
            )
            response.raise_for_status()
            payload: list[list[Any]] = response.json()
            if not payload:
                continue
            prices[symbol] = float(payload[0][1])
        return prices

    async def get_open_prices_for_requests(self, requests: dict[str, tuple[str, int]]) -> dict[str, float]:
        prices: dict[str, float] = {}
        for request_id, (symbol, start_ms) in sorted(requests.items()):
            response = await self._client.get(
                "/api/v3/klines",
                params={
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": start_ms,
                    "limit": 1,
                },
            )
            response.raise_for_status()
            payload: list[list[Any]] = response.json()
            if not payload:
                continue
            prices[request_id] = float(payload[0][1])
        return prices
