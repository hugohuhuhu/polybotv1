from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.config import Settings
from app.services.preflight import PreflightCheck, load_preflight_report


def test_preflight_blocks_missing_private_key() -> None:
    report = asyncio.run(
        load_preflight_report(
            Settings(POLYMARKET_PRIVATE_KEY=""),
            now=datetime(2026, 4, 29, tzinfo=timezone.utc),
        )
    )

    assert report.ready is False
    assert "尚未載入私鑰。" in report.blocking_reasons


def test_preflight_passes_when_required_v2_readiness_is_present(monkeypatch) -> None:
    async def fake_chain_id(_client, _rpc_url):
        return 137

    async def fake_native(_client, _rpc_url, _wallet_address):
        return 1_000_000_000_000_000_000

    async def fake_balance(_client, _rpc_url, _token_address, _wallet_address):
        return 100_000_000

    async def fake_allowance(_client, _rpc_url, _token_address, _wallet_address, _spender_address):
        return 100_000_000

    async def fake_clock(_client, _settings, _now):
        return PreflightCheck(
            check_id="clock_drift",
            label="系統時間",
            status="ok",
            message="時間正常",
            required=False,
        )

    monkeypatch.setattr("app.services.preflight.fetch_chain_id", fake_chain_id)
    monkeypatch.setattr("app.services.preflight.fetch_native_balance", fake_native)
    monkeypatch.setattr("app.services.preflight.fetch_erc20_balance", fake_balance)
    monkeypatch.setattr("app.services.preflight.fetch_erc20_allowance", fake_allowance)
    monkeypatch.setattr("app.services.preflight._check_clob_clock", fake_clock)
    monkeypatch.setattr("app.services.preflight._clob_v2_sdk_error", lambda: None)

    report = asyncio.run(
        load_preflight_report(
            Settings(POLYMARKET_PRIVATE_KEY="0x" + "1" * 64),
            now=datetime(2026, 4, 29, tzinfo=timezone.utc),
        )
    )

    assert report.ready is True
    assert report.address is not None
    assert report.collateral_symbol == "pUSD"


def test_preflight_blocks_missing_v2_sdk_after_cutover(monkeypatch) -> None:
    async def fake_chain_id(_client, _rpc_url):
        return 137

    async def fake_native(_client, _rpc_url, _wallet_address):
        return 1_000_000_000_000_000_000

    async def fake_balance(_client, _rpc_url, _token_address, _wallet_address):
        return 100_000_000

    async def fake_allowance(_client, _rpc_url, _token_address, _wallet_address, _spender_address):
        return 100_000_000

    async def fake_clock(_client, _settings, _now):
        return PreflightCheck(
            check_id="clock_drift",
            label="系統時間",
            status="ok",
            message="時間正常",
            required=False,
        )

    monkeypatch.setattr("app.services.preflight.fetch_chain_id", fake_chain_id)
    monkeypatch.setattr("app.services.preflight.fetch_native_balance", fake_native)
    monkeypatch.setattr("app.services.preflight.fetch_erc20_balance", fake_balance)
    monkeypatch.setattr("app.services.preflight.fetch_erc20_allowance", fake_allowance)
    monkeypatch.setattr("app.services.preflight._check_clob_clock", fake_clock)
    monkeypatch.setattr("app.services.preflight._clob_v2_sdk_error", lambda: "No module named py_clob_client_v2")

    report = asyncio.run(
        load_preflight_report(
            Settings(POLYMARKET_PRIVATE_KEY="0x" + "1" * 64),
            now=datetime(2026, 4, 29, tzinfo=timezone.utc),
        )
    )

    assert report.ready is False
    assert any("py-clob-client-v2" in reason for reason in report.blocking_reasons)
