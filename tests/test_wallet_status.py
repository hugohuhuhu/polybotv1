from __future__ import annotations

import asyncio

from eth_account import Account

from app.config import Settings
from app.services.wallet_status import fetch_polymarket_portfolio_value, fetch_polymarket_positions, load_wallet_status


class FakePortfolioResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict[str, float | str]]:
        return [{"user": "0xabc", "value": 24.995}]


class FakePortfolioClient:
    async def get(self, _url, *, params):
        return FakePortfolioResponse()


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

    async def fake_portfolio_value(_client, _base_url, _wallet_address):
        return 24.995

    async def fake_positions(_client, _base_url, _wallet_address):
        return [{"asset": "yes", "currentValue": 5.0, "cashPnl": 0.2}]

    monkeypatch.setattr("app.services.wallet_status.fetch_erc20_balance", fake_fetch)
    monkeypatch.setattr("app.services.wallet_status.fetch_native_balance", fake_fetch_native)
    monkeypatch.setattr("app.services.wallet_status.fetch_polymarket_portfolio_value", fake_portfolio_value)
    monkeypatch.setattr("app.services.wallet_status.fetch_polymarket_positions", fake_positions)

    private_key = "0x" + "1" * 64
    status = asyncio.run(load_wallet_status(Settings(POLYMARKET_PRIVATE_KEY=private_key)))

    assert status["configured"] is True
    assert status["address"] == Account.from_key(private_key).address
    balance_map = {item["symbol"]: item["amount"] for item in status["balances"]}
    assert balance_map["POL"] == 1.5
    assert balance_map["USDC"] == 12.5
    assert balance_map["pUSD"] == 3.25
    assert status["portfolio"]["position_value"] == 24.995
    assert status["portfolio"]["source"] == "polymarket_data_api"
    assert status["portfolio"]["positions"][0]["cashPnl"] == 0.2


def test_portfolio_value_accepts_data_api_list_payload() -> None:
    value = asyncio.run(fetch_polymarket_portfolio_value(FakePortfolioClient(), "https://data-api.polymarket.com", "0xabc"))

    assert value == 24.995


def test_portfolio_positions_accepts_data_api_list_payload() -> None:
    positions = asyncio.run(fetch_polymarket_positions(FakePortfolioClient(), "https://data-api.polymarket.com", "0xabc"))

    assert positions == [{"user": "0xabc", "value": 24.995}]
