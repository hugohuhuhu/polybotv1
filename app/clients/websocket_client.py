from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from app.models.core import OrderBookSnapshot
from app.utils.math_utils import normalise_book_levels, safe_float

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]


class MarketWebSocketClient:
    """Market WebSocket client for best-bid-ask and book updates."""

    def __init__(self, url: str, message_handler: MessageHandler) -> None:
        self._url = url
        self._message_handler = message_handler
        self._stop_event = asyncio.Event()
        self._subscribed_assets: list[str] = []

    async def stop(self) -> None:
        self._stop_event.set()

    async def subscribe_forever(self, asset_ids: list[str]) -> None:
        self._subscribed_assets = list(asset_ids)
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self._url, ping_interval=None, open_timeout=15) as websocket:
                    await self._send_subscribe(websocket, asset_ids)
                    backoff = 1.0
                    await self._listen(websocket)
            except Exception:
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(min(backoff, 30))
                backoff *= 2

    async def _send_subscribe(self, websocket: WebSocketClientProtocol, asset_ids: list[str]) -> None:
        payload = {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": True,
            "initial_dump": True,
        }
        await websocket.send(json.dumps(payload))

    async def _listen(self, websocket: WebSocketClientProtocol) -> None:
        async for raw in websocket:
            if self._stop_event.is_set():
                return
            if raw == "{}":
                await websocket.send("{}")
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, list):
                for item in payload:
                    if isinstance(item, dict):
                        await self._message_handler(item)
                continue
            if isinstance(payload, dict):
                await self._message_handler(payload)


class OrderBookState:
    """In-memory order book store kept current via WebSocket events."""

    def __init__(self) -> None:
        self.books: dict[str, OrderBookSnapshot] = {}

    async def handle_message(self, payload: dict[str, Any]) -> None:
        event_type = payload.get("event_type")
        if event_type == "book":
            self._apply_book(payload)
        elif event_type == "best_bid_ask":
            self._apply_best_bid_ask(payload)
        elif event_type == "price_change":
            self._apply_price_change(payload)
        elif event_type == "last_trade_price":
            self._apply_last_trade_price(payload)

    def upsert_snapshot(self, snapshot: OrderBookSnapshot) -> None:
        self.books[snapshot.token_id] = snapshot

    def _apply_book(self, payload: dict[str, Any]) -> None:
        token_id = str(payload.get("asset_id"))
        current = self.books.get(token_id)
        snapshot = OrderBookSnapshot(
            token_id=token_id,
            market_id=payload.get("market"),
            bids=normalise_book_levels(payload.get("bids", []), "bid"),
            asks=normalise_book_levels(payload.get("asks", []), "ask"),
            last_trade_price=current.last_trade_price if current else None,
            updated_at=datetime.now(timezone.utc),
            source="ws",
        )
        self.books[token_id] = snapshot

    def _apply_best_bid_ask(self, payload: dict[str, Any]) -> None:
        token_id = str(payload.get("asset_id"))
        current = self.books.get(token_id) or OrderBookSnapshot(token_id=token_id, market_id=payload.get("market"))
        best_bid = safe_float(payload.get("best_bid"))
        best_ask = safe_float(payload.get("best_ask"))
        if best_bid is not None:
            current.bids = normalise_book_levels(
                [{"price": best_bid, "size": current.bids[0].size if current.bids else 0.0}],
                "bid",
            )
        if best_ask is not None:
            current.asks = normalise_book_levels(
                [{"price": best_ask, "size": current.asks[0].size if current.asks else 0.0}],
                "ask",
            )
        current.market_id = payload.get("market") or current.market_id
        current.updated_at = datetime.now(timezone.utc)
        current.source = "ws"
        self.books[token_id] = current

    def _apply_price_change(self, payload: dict[str, Any]) -> None:
        for change in payload.get("price_changes", []):
            token_id = str(change.get("asset_id"))
            current = self.books.get(token_id) or OrderBookSnapshot(token_id=token_id, market_id=payload.get("market"))
            side = str(change.get("side", "")).upper()
            price = safe_float(change.get("price"))
            size = safe_float(change.get("size"), 0.0) or 0.0
            if price is not None:
                levels = current.bids if side == "BUY" else current.asks
                filtered = [level for level in levels if level.price != price]
                if size > 0:
                    filtered.append({"price": price, "size": size})
                normalised = normalise_book_levels(filtered, "bid" if side == "BUY" else "ask")
                if side == "BUY":
                    current.bids = normalised
                else:
                    current.asks = normalised
            best_bid = safe_float(change.get("best_bid"))
            best_ask = safe_float(change.get("best_ask"))
            if best_bid is not None and all(level.price != best_bid for level in current.bids):
                current.bids = normalise_book_levels(
                    [{"price": best_bid, "size": current.bids[0].size if current.bids else size}] + current.bids,
                    "bid",
                )
            if best_ask is not None and all(level.price != best_ask for level in current.asks):
                current.asks = normalise_book_levels(
                    [{"price": best_ask, "size": current.asks[0].size if current.asks else size}] + current.asks,
                    "ask",
                )
            current.market_id = payload.get("market") or current.market_id
            current.updated_at = datetime.now(timezone.utc)
            current.source = "ws"
            self.books[token_id] = current

    def _apply_last_trade_price(self, payload: dict[str, Any]) -> None:
        token_id = str(payload.get("asset_id"))
        current = self.books.get(token_id) or OrderBookSnapshot(token_id=token_id, market_id=payload.get("market"))
        current.last_trade_price = safe_float(payload.get("price"))
        current.updated_at = datetime.now(timezone.utc)
        current.source = "ws"
        self.books[token_id] = current
