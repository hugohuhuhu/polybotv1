from __future__ import annotations

import asyncio
from collections.abc import Iterable
from datetime import datetime, timezone

import httpx

from app.models.core import OrderBookSnapshot
from app.utils.math_utils import normalise_book_levels, safe_float
from app.utils.time_utils import parse_datetime


class ClobClient:
    """Read-only client for public CLOB endpoints."""

    def __init__(self, base_url: str, timeout: float = 10.0, concurrency: int = 20) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout)
        self._semaphore = asyncio.Semaphore(concurrency)

    async def close(self) -> None:
        await self._client.aclose()

    async def get_order_book(self, token_id: str) -> OrderBookSnapshot | None:
        async with self._semaphore:
            retries = 3
            backoff = 1.0
            for attempt in range(retries):
                try:
                    response = await self._client.get("/book", params={"token_id": token_id})
                    if response.status_code == 404:
                        return None
                    response.raise_for_status()
                    payload = response.json()
                    return OrderBookSnapshot(
                        token_id=str(payload.get("asset_id", token_id)),
                        market_id=payload.get("market"),
                        bids=normalise_book_levels(payload.get("bids", []), "bid"),
                        asks=normalise_book_levels(payload.get("asks", []), "ask"),
                        last_trade_price=safe_float(payload.get("last_trade_price")),
                        tick_size=safe_float(payload.get("tick_size")),
                        min_order_size=safe_float(payload.get("min_order_size")),
                        updated_at=parse_datetime(payload.get("timestamp")) or datetime.now(timezone.utc),
                        source="rest",
                    )
                except httpx.HTTPError:
                    if attempt == retries - 1:
                        return None
                    await asyncio.sleep(backoff)
                    backoff *= 2
        return None

    async def get_order_books(self, token_ids: Iterable[str]) -> dict[str, OrderBookSnapshot]:
        tasks = [self.get_order_book(token_id) for token_id in token_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        books: dict[str, OrderBookSnapshot] = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            if result is not None:
                books[result.token_id] = result
        return books
