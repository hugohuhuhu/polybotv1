from __future__ import annotations

import asyncio

from eth_account import Account

from app.config import Settings
from app.services.wallet_status import load_wallet_status


def test_wallet_status_reports_missing_private_key() -> None:
    status = asyncio.run(load_wallet_status(Settings(POLYMARKET_PRIVATE_KEY="")))
    assert status["configured"] is False
    assert status["message"] == "尚未輸入私鑰"


def test_wallet_status_derives_address_and_balances(monkeypatch) -> None:
    balances = {
        "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359": 12_500_000,
        "0xc011a7e12a19f7b1f670d46f03b03f3342e82dfb": 3_250_000,
    }

    async def fake_fetch(_client, _rpc_url, token_address, _wallet_address):
        return balances[token_address.lower()]

    async def fake_fetch_native(_client, _rpc_url, _wallet_address):
        return 1_500_000_000_000_000_000

    monkeypatch.setattr("app.services.wallet_status.fetch_erc20_balance", fake_fetch)
    monkeypatch.setattr("app.services.wallet_status.fetch_native_balance", fake_fetch_native)

    private_key = "0x" + "1" * 64
    status = asyncio.run(load_wallet_status(Settings(POLYMARKET_PRIVATE_KEY=private_key)))

    assert status["configured"] is True
    assert status["address"] == Account.from_key(private_key).address
    balance_map = {item["symbol"]: item["amount"] for item in status["balances"]}
    assert balance_map["POL"] == 1.5
    assert balance_map["USDC"] == 12.5
    assert balance_map["pUSD"] == 3.25
