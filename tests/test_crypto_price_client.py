from __future__ import annotations

import asyncio
import json
import time

import httpx

from app.clients.crypto_price_client import CryptoPriceClient
from app.config import Settings


def _slot(value: int) -> str:
    if value < 0:
        value += 1 << 256
    return f"{value:064x}"


def _round_response(*, round_id: int, answer: int, updated_at: int) -> str:
    return "0x" + "".join(
        [
            _slot(round_id),
            _slot(answer),
            _slot(updated_at),
            _slot(updated_at),
            _slot(round_id),
        ]
    )


def test_settings_use_chainlink_price_source_by_default() -> None:
    settings = Settings()

    assert settings.crypto_price_source == "chainlink"
    assert "BTCUSDT:0xc907E116054Ad103354f2D350FD2514433D57F6f" in settings.chainlink_price_feed_addresses
    assert "BNBUSDT:0x82a6c4AF830caa6c97bb504425f6A66165C2c26e" in settings.chainlink_price_feed_addresses


def test_chainlink_latest_price_decodes_rpc_round_data() -> None:
    feed = "0x0000000000000000000000000000000000000001"
    updated_at = int(time.time())

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        data = payload["params"][0]["data"]
        if data == "0x313ce567":
            result = "0x" + _slot(8)
        elif data == "0xfeaf968c":
            result = _round_response(round_id=(1 << 64) + 7, answer=105_250_000_000, updated_at=updated_at)
        else:
            raise AssertionError(f"unexpected eth_call data {data}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    client = CryptoPriceClient(
        source="chainlink",
        rpc_url="https://rpc.example",
        chainlink_feeds={"BTCUSDT": feed},
        transport=httpx.MockTransport(handler),
    )
    try:
        observations = asyncio.run(client.get_price_observations({"BTCUSDT"}))
    finally:
        asyncio.run(client.close())

    assert observations["BTCUSDT"].price == 1052.5
    assert observations["BTCUSDT"].updated_at == updated_at


def test_chainlink_open_price_uses_round_at_or_before_start_time() -> None:
    feed = "0x0000000000000000000000000000000000000001"
    phase = 1 << 64
    rounds = {
        phase + 1: _round_response(round_id=phase + 1, answer=100_000_000_000, updated_at=100),
        phase + 2: _round_response(round_id=phase + 2, answer=101_000_000_000, updated_at=200),
        phase + 3: _round_response(round_id=phase + 3, answer=102_000_000_000, updated_at=300),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        data = payload["params"][0]["data"]
        if data == "0x313ce567":
            result = "0x" + _slot(8)
        elif data == "0xfeaf968c":
            result = rounds[phase + 3]
        elif data.startswith("0x9a6fc8f5"):
            round_id = int(data.removeprefix("0x9a6fc8f5"), 16)
            result = rounds[round_id]
        else:
            raise AssertionError(f"unexpected eth_call data {data}")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    client = CryptoPriceClient(
        source="chainlink",
        rpc_url="https://rpc.example",
        chainlink_feeds={"BTCUSDT": feed},
        transport=httpx.MockTransport(handler),
    )
    try:
        prices = asyncio.run(client.get_open_prices_for_requests({"market-1": ("BTCUSDT", 250_000)}))
    finally:
        asyncio.run(client.close())

    assert prices == {"market-1": 1010.0}


def test_recent_range_observation_uses_binance_one_second_klines() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/klines"
        payload = [
            [1_000, "100.0", "100.2", "99.9", "100.1"],
            [2_000, "100.1", "100.5", "100.0", "100.4"],
            [3_000, "100.4", "100.4", "99.8", "99.9"],
        ]
        return httpx.Response(200, json=payload)

    client = CryptoPriceClient(source="chainlink", transport=httpx.MockTransport(handler))
    try:
        observations = asyncio.run(
            client.get_recent_range_observations(
                {"market-1": ("BTCUSDT", 3_000)},
                window_sec=3,
                min_samples=3,
            )
        )
    finally:
        asyncio.run(client.close())

    assert observations["market-1"].sample_count == 3
    assert round(observations["market-1"].range_bps, 6) == 70.0
    assert observations["market-1"].latest_close == 99.9
    assert round(observations["market-1"].short_change_bps or 0.0, 6) == -19.98002
    assert round(observations["market-1"].long_change_bps or 0.0, 6) == -19.98002
