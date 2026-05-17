from __future__ import annotations

import asyncio

import httpx

from app.services.onchain import rpc_call


def test_rpc_call_retries_transient_timeout() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x89"})

    async def run() -> str:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await rpc_call(client, "https://rpc.example", "eth_chainId", [])

    assert asyncio.run(run()) == "0x89"
    assert calls == 2


def test_rpc_call_reports_empty_timeout_class_name() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await rpc_call(client, "https://rpc.example", "eth_call", [])

    try:
        asyncio.run(run())
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected RuntimeError")

    assert "eth_call" in message
    assert "ReadTimeout" in message
