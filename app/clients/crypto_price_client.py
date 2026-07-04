from __future__ import annotations

import asyncio
from dataclasses import dataclass
import time
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


_CHAINLINK_POLYGON_FEEDS = {
    "BTCUSDT": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETHUSDT": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "SOLUSDT": "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC",
    "BNBUSDT": "0x82a6c4AF830caa6c97bb504425f6A66165C2c26e",
}

_CHAINLINK_DECIMALS_SELECTOR = "0x313ce567"
_CHAINLINK_LATEST_ROUND_SELECTOR = "0xfeaf968c"
_CHAINLINK_GET_ROUND_SELECTOR = "0x9a6fc8f5"


def binance_symbol_for_asset(asset: str) -> str | None:
    return _BINANCE_SYMBOLS.get(asset.lower())


@dataclass(frozen=True, slots=True)
class ChainlinkRoundData:
    round_id: int
    answer: float
    updated_at: int


@dataclass(frozen=True, slots=True)
class CryptoPriceObservation:
    price: float
    updated_at: int


@dataclass(frozen=True, slots=True)
class CryptoRangeObservation:
    range_bps: float
    sample_count: int
    window_start_ms: int
    window_end_ms: int
    latest_close: float | None = None
    short_change_bps: float | None = None
    long_change_bps: float | None = None


def _parse_chainlink_feeds(value: str | dict[str, str] | None) -> dict[str, str]:
    feeds = dict(_CHAINLINK_POLYGON_FEEDS)
    if isinstance(value, dict):
        items = value.items()
    else:
        raw = str(value or "").strip()
        if not raw:
            return feeds
        items = []
        for chunk in raw.split(","):
            if ":" not in chunk:
                continue
            symbol, address = chunk.split(":", 1)
            items.append((symbol, address))
    for symbol, address in items:
        clean_symbol = str(symbol or "").strip().upper()
        clean_address = str(address or "").strip()
        if clean_symbol and clean_address.startswith("0x") and len(clean_address) == 42:
            feeds[clean_symbol] = clean_address
    return feeds


def crypto_price_client_from_settings(settings: Any) -> "CryptoPriceClient":
    return CryptoPriceClient(
        timeout=float(getattr(settings, "crypto_price_timeout_sec", 8.0)),
        source=str(getattr(settings, "crypto_price_source", "chainlink")),
        rpc_url=str(getattr(settings, "polygon_rpc_url", "")),
        chainlink_feeds=getattr(settings, "chainlink_price_feed_addresses", None),
        chainlink_stale_after_sec=float(getattr(settings, "chainlink_price_stale_sec", 3600.0)),
    )


class CryptoPriceClient:
    """Small public spot-price client used only for crypto near-close guards."""

    def __init__(
        self,
        base_url: str = "https://api.binance.com",
        timeout: float = 8.0,
        *,
        source: str = "chainlink",
        rpc_url: str | None = None,
        chainlink_feeds: str | dict[str, str] | None = None,
        chainlink_stale_after_sec: float = 3600.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.source = source.strip().lower() or "chainlink"
        if self.source not in {"chainlink", "binance"}:
            self.source = "chainlink"
        self._rpc_url = rpc_url or "https://polygon-bor-rpc.publicnode.com"
        self._chainlink_feeds = _parse_chainlink_feeds(chainlink_feeds)
        self._chainlink_stale_after_sec = max(float(chainlink_stale_after_sec), 0.0)
        self._decimals_cache: dict[str, int] = {}
        if self.source == "binance":
            self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        else:
            self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_prices(self, symbols: set[str]) -> dict[str, float]:
        observations = await self.get_price_observations(symbols)
        return {symbol: observation.price for symbol, observation in observations.items()}

    async def get_price_observations(self, symbols: set[str]) -> dict[str, CryptoPriceObservation]:
        if self.source == "chainlink":
            return await self._get_chainlink_price_observations(symbols)
        prices = await self._get_binance_prices(symbols)
        updated_at = int(time.time())
        return {
            symbol: CryptoPriceObservation(price=price, updated_at=updated_at)
            for symbol, price in prices.items()
        }

    async def _get_binance_prices(self, symbols: set[str]) -> dict[str, float]:
        async def fetch_price(symbol: str) -> tuple[str, float | None]:
            try:
                response = await self._client.get("/api/v3/ticker/price", params={"symbol": symbol})
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
                return symbol, float(payload["price"])
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                return symbol, None

        fetched = await asyncio.gather(*(fetch_price(symbol) for symbol in sorted(symbols)))
        return {symbol: price for symbol, price in fetched if price is not None}

    async def get_open_prices_at(self, symbols_by_start_ms: dict[str, int]) -> dict[str, float]:
        if self.source == "chainlink":
            return await self._get_chainlink_open_prices_at(symbols_by_start_ms)
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
        if self.source == "chainlink":
            return await self._get_chainlink_open_prices_for_requests(requests)
        unique_requests = sorted(set(requests.values()))
        semaphore = asyncio.Semaphore(8)

        async def fetch_open_price(symbol: str, start_ms: int) -> tuple[tuple[str, int], float | None]:
            async with semaphore:
                try:
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
                        return (symbol, start_ms), None
                    return (symbol, start_ms), float(payload[0][1])
                except (httpx.HTTPError, IndexError, TypeError, ValueError):
                    return (symbol, start_ms), None

        fetched = await asyncio.gather(*(fetch_open_price(symbol, start_ms) for symbol, start_ms in unique_requests))
        prices_by_key = {key: price for key, price in fetched if price is not None}
        return {
            request_id: prices_by_key[key]
            for request_id, key in requests.items()
            if key in prices_by_key
        }

    async def get_recent_range_observations(
        self,
        requests: dict[str, tuple[str, int]],
        *,
        window_sec: int = 60,
        min_samples: int = 20,
        timeout_sec: float = 2.0,
    ) -> dict[str, CryptoRangeObservation]:
        window_ms = max(int(window_sec), 1) * 1000
        unique_requests = sorted(set(requests.values()))
        semaphore = asyncio.Semaphore(4)

        async def fetch_range(
            symbol: str,
            end_ms: int,
        ) -> tuple[tuple[str, int], CryptoRangeObservation | None]:
            start_ms = int(end_ms) - window_ms
            async with semaphore:
                try:
                    response = await self._client.get(
                        "https://api.binance.com/api/v3/klines",
                        params={
                            "symbol": symbol,
                            "interval": "1s",
                            "startTime": start_ms,
                            "endTime": int(end_ms),
                            "limit": min(max(int(window_sec) + 2, 20), 1000),
                        },
                        timeout=max(float(timeout_sec), 0.1),
                    )
                    response.raise_for_status()
                    payload: list[list[Any]] = response.json()
                    samples = [row for row in payload if len(row) >= 4 and int(row[0]) >= start_ms]
                    if len(samples) < max(int(min_samples), 1):
                        return (symbol, end_ms), None
                    reference_price = float(samples[0][1])
                    if reference_price <= 0:
                        return (symbol, end_ms), None
                    high = max(float(row[2]) for row in samples)
                    low = min(float(row[3]) for row in samples)
                    closes = [(int(row[0]), float(row[4])) for row in samples if len(row) >= 5]
                    latest_close = closes[-1][1] if closes else None

                    def change_bps(seconds: int) -> float | None:
                        if latest_close is None or latest_close <= 0 or not closes:
                            return None
                        target_ms = int(end_ms) - (seconds * 1000)
                        reference = next(
                            (close for timestamp, close in reversed(closes) if timestamp <= target_ms),
                            closes[0][1],
                        )
                        if reference <= 0:
                            return None
                        return ((latest_close - reference) / reference) * 10_000.0

                    return (symbol, end_ms), CryptoRangeObservation(
                        range_bps=((high - low) / reference_price) * 10_000.0,
                        sample_count=len(samples),
                        window_start_ms=start_ms,
                        window_end_ms=int(end_ms),
                        latest_close=latest_close,
                        short_change_bps=change_bps(3),
                        long_change_bps=change_bps(8),
                    )
                except (httpx.HTTPError, IndexError, TypeError, ValueError):
                    return (symbol, end_ms), None

        fetched = await asyncio.gather(*(fetch_range(symbol, end_ms) for symbol, end_ms in unique_requests))
        observations_by_key = {key: observation for key, observation in fetched if observation is not None}
        return {
            request_id: observations_by_key[key]
            for request_id, key in requests.items()
            if key in observations_by_key
        }

    async def _get_chainlink_price_observations(
        self,
        symbols: set[str],
    ) -> dict[str, CryptoPriceObservation]:
        async def fetch_price(symbol: str) -> tuple[str, CryptoPriceObservation | None]:
            try:
                round_data = await self._latest_chainlink_round(symbol)
                if self._chainlink_round_is_stale(round_data):
                    return symbol, None
                return symbol, CryptoPriceObservation(
                    price=round_data.answer,
                    updated_at=round_data.updated_at,
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                return symbol, None

        fetched = await asyncio.gather(*(fetch_price(symbol) for symbol in sorted(symbols)))
        return {symbol: observation for symbol, observation in fetched if observation is not None}

    async def _get_chainlink_open_prices_at(self, symbols_by_start_ms: dict[str, int]) -> dict[str, float]:
        requests = {symbol: (symbol, start_ms) for symbol, start_ms in symbols_by_start_ms.items()}
        return await self._get_chainlink_open_prices_for_requests(requests)

    async def _get_chainlink_open_prices_for_requests(self, requests: dict[str, tuple[str, int]]) -> dict[str, float]:
        unique_requests = sorted(set(requests.values()))
        semaphore = asyncio.Semaphore(4)

        async def fetch_open_price(symbol: str, start_ms: int) -> tuple[tuple[str, int], float | None]:
            async with semaphore:
                try:
                    round_data = await self._chainlink_round_at_or_before(symbol, int(start_ms / 1000))
                    return (symbol, start_ms), round_data.answer if round_data is not None else None
                except (httpx.HTTPError, KeyError, TypeError, ValueError):
                    return (symbol, start_ms), None

        fetched = await asyncio.gather(*(fetch_open_price(symbol, start_ms) for symbol, start_ms in unique_requests))
        prices_by_key = {key: price for key, price in fetched if price is not None}
        return {
            request_id: prices_by_key[key]
            for request_id, key in requests.items()
            if key in prices_by_key
        }

    def _chainlink_round_is_stale(self, round_data: ChainlinkRoundData) -> bool:
        if self._chainlink_stale_after_sec <= 0:
            return False
        return (time.time() - round_data.updated_at) > self._chainlink_stale_after_sec

    async def _latest_chainlink_round(self, symbol: str) -> ChainlinkRoundData:
        address = self._chainlink_address(symbol)
        decimals = await self._chainlink_decimals(address)
        return self._decode_round_data(
            await self._eth_call(address, _CHAINLINK_LATEST_ROUND_SELECTOR),
            decimals=decimals,
        )

    async def _chainlink_round_at_or_before(self, symbol: str, target_ts: int) -> ChainlinkRoundData | None:
        latest = await self._latest_chainlink_round(symbol)
        if latest.updated_at <= target_ts:
            return latest
        phase = latest.round_id >> 64
        low = (phase << 64) + 1
        high = latest.round_id
        best: ChainlinkRoundData | None = None
        decimals = await self._chainlink_decimals(self._chainlink_address(symbol))
        address = self._chainlink_address(symbol)
        while low <= high:
            mid = (low + high) // 2
            try:
                current = self._decode_round_data(
                    await self._eth_call(address, _CHAINLINK_GET_ROUND_SELECTOR + mid.to_bytes(32, "big").hex()),
                    decimals=decimals,
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                low = mid + 1
                continue
            if current.updated_at <= target_ts:
                best = current
                low = mid + 1
            else:
                high = mid - 1
        return best

    def _chainlink_address(self, symbol: str) -> str:
        address = self._chainlink_feeds.get(symbol.upper())
        if not address:
            raise KeyError(f"missing Chainlink feed for {symbol}")
        return address

    async def _chainlink_decimals(self, address: str) -> int:
        cached = self._decimals_cache.get(address)
        if cached is not None:
            return cached
        result = await self._eth_call(address, _CHAINLINK_DECIMALS_SELECTOR)
        decimals = int(result, 16)
        self._decimals_cache[address] = decimals
        return decimals

    async def _eth_call(self, address: str, data: str) -> str:
        response = await self._client.post(
            self._rpc_url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": address, "data": data}, "latest"],
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        if payload.get("error"):
            raise ValueError(str(payload["error"]))
        result = str(payload.get("result") or "")
        if not result.startswith("0x") or len(result) <= 2:
            raise ValueError("empty Chainlink eth_call result")
        return result

    @staticmethod
    def _decode_round_data(result: str, *, decimals: int) -> ChainlinkRoundData:
        raw = result[2:]
        if len(raw) < 64 * 5:
            raise ValueError("short Chainlink round response")
        slots = [raw[index : index + 64] for index in range(0, 64 * 5, 64)]
        answer_int = int(slots[1], 16)
        if answer_int >= 1 << 255:
            answer_int -= 1 << 256
        updated_at = int(slots[3], 16)
        if answer_int <= 0 or updated_at <= 0:
            raise ValueError("invalid Chainlink round response")
        return ChainlinkRoundData(
            round_id=int(slots[0], 16),
            answer=answer_int / (10**decimals),
            updated_at=updated_at,
        )
