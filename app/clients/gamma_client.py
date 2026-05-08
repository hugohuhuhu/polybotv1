from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

import httpx

from app.models.core import EventRecord, MarketRecord
from app.utils.math_utils import parse_jsonish_list, safe_float
from app.utils.time_utils import parse_datetime


class GammaClient:
    """Client for market discovery via the public Gamma API."""

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        retries = 3
        backoff = 1.0
        for attempt in range(retries):
            try:
                response = await self._client.get(path, params=params)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError:
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(backoff)
                backoff *= 2
        raise RuntimeError("Unreachable retry loop")

    async def iter_active_events(
        self,
        *,
        limit: int = 100,
        page_size: int = 50,
    ) -> AsyncIterator[dict[str, Any]]:
        yielded = 0
        cursor: str | None = None
        while yielded < limit:
            remaining = limit - yielded
            params = {
                "active": "true",
                "closed": "false",
                "limit": min(page_size, remaining),
            }
            if cursor:
                params["after_cursor"] = cursor
            payload = await self._get("/events/keyset", params=params)
            events = payload.get("events", [])
            if not events:
                break
            for event in events:
                yield event
                yielded += 1
                if yielded >= limit:
                    break
            cursor = payload.get("next_cursor")
            if not cursor:
                break

    def normalise_event(self, payload: dict[str, Any]) -> EventRecord:
        tags = [tag.get("label") for tag in payload.get("tags", []) if tag.get("label")]
        return EventRecord(
            event_id=str(payload.get("id")),
            slug=payload.get("slug"),
            title=payload.get("title") or payload.get("ticker") or "未命名事件",
            category=payload.get("category"),
            tags=tags,
            active=bool(payload.get("active", True)),
            closed=bool(payload.get("closed", False)),
            start_date=parse_datetime(payload.get("startDate")),
            end_date=parse_datetime(payload.get("endDate")),
            liquidity=safe_float(payload.get("liquidity")),
            volume=safe_float(payload.get("volume")),
            raw=payload,
        )

    def normalise_market(
        self,
        payload: dict[str, Any],
        event: EventRecord | None = None,
    ) -> MarketRecord:
        outcome_labels = [str(value) for value in parse_jsonish_list(payload.get("outcomes"))]
        outcome_prices = [float(value) for value in parse_jsonish_list(payload.get("outcomePrices")) if safe_float(value) is not None]
        token_ids = [str(value) for value in parse_jsonish_list(payload.get("clobTokenIds"))]
        tags = event.tags if event else [tag.get("label") for tag in payload.get("tags", []) if tag.get("label")]
        return MarketRecord(
            market_id=str(payload.get("id")),
            event_id=event.event_id if event else None,
            question=payload.get("question") or payload.get("description") or "未命名市場",
            slug=payload.get("slug") or str(payload.get("id")),
            condition_id=payload.get("conditionId"),
            resolution_source=payload.get("resolutionSource") or (event.raw.get("resolutionSource") if event else None),
            end_date=parse_datetime(payload.get("endDate")),
            start_date=parse_datetime(payload.get("startDate")),
            outcome_labels=outcome_labels,
            outcome_prices=outcome_prices,
            token_ids=token_ids,
            category=payload.get("category") or (event.category if event else None),
            tags=tags,
            active=bool(payload.get("active", True)),
            closed=bool(payload.get("closed", False)),
            restricted=bool(payload.get("restricted", False)),
            liquidity=safe_float(payload.get("liquidity")) or safe_float(payload.get("liquidityClob")),
            volume=safe_float(payload.get("volume")) or safe_float(payload.get("volumeClob")),
            spread=safe_float(payload.get("spread")),
            best_bid=safe_float(payload.get("bestBid")),
            best_ask=safe_float(payload.get("bestAsk")),
            last_trade_price=safe_float(payload.get("lastTradePrice")),
            event_title=event.title if event else None,
            event_slug=event.slug if event else None,
            fees_enabled=bool(payload.get("feesEnabled", False)),
            raw=payload,
        )

    async def discover_active_markets(self, *, limit: int = 100) -> tuple[list[EventRecord], list[MarketRecord]]:
        events: list[EventRecord] = []
        markets: list[MarketRecord] = []
        async for event_payload in self.iter_active_events(limit=limit):
            event = self.normalise_event(event_payload)
            events.append(event)
            for market_payload in event_payload.get("markets", []):
                market = self.normalise_market(market_payload, event=event)
                if market.active and not market.closed:
                    markets.append(market)
        return events, markets

    async def discover_markets_by_end_date(
        self,
        *,
        end_date_min: datetime,
        end_date_max: datetime,
        limit: int = 500,
    ) -> tuple[list[EventRecord], list[MarketRecord]]:
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "end_date_min": end_date_min.isoformat().replace("+00:00", "Z"),
            "end_date_max": end_date_max.isoformat().replace("+00:00", "Z"),
            "order": "endDate",
            "ascending": "true",
        }
        payload = await self._get("/markets", params=params)
        events_by_id: dict[str, EventRecord] = {}
        markets: list[MarketRecord] = []
        for market_payload in payload if isinstance(payload, list) else []:
            event = None
            event_payloads = market_payload.get("events") or []
            if event_payloads:
                event = self.normalise_event(event_payloads[0])
                events_by_id[event.event_id] = event
            market = self.normalise_market(market_payload, event=event)
            if market.active and not market.closed:
                markets.append(market)
        return list(events_by_id.values()), markets
