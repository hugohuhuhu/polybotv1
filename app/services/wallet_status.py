from __future__ import annotations

import asyncio
from typing import Any

import httpx
from eth_account import Account

from app.config import Settings
from app.services.onchain import (
    NATIVE_TOKEN_DECIMALS,
    TOKEN_DECIMALS,
    fetch_erc20_balance,
    fetch_native_balance,
    format_units,
)


def _empty_balance(symbol: str, *, status: str, note: str | None = None) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "amount": None,
        "status": status,
        "note": note,
    }


def _default_empty_balances(status: str) -> list[dict[str, Any]]:
    return [
        _empty_balance("POL", status=status),
        _empty_balance("USDC", status=status),
        _empty_balance("pUSD", status=status),
    ]


async def fetch_polymarket_portfolio_value(client: httpx.AsyncClient, base_url: str, address: str) -> float:
    response = await client.get(f"{base_url.rstrip('/')}/value", params={"user": address})
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return float(payload.get("value") or 0.0)


async def fetch_polymarket_positions(client: httpx.AsyncClient, base_url: str, address: str) -> list[dict[str, Any]]:
    response = await client.get(
        f"{base_url.rstrip('/')}/positions",
        params={"user": address, "limit": 500},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


async def load_wallet_status(settings: Settings) -> dict[str, Any]:
    """Return private-key-derived wallet address and read-only token balances."""

    private_key = (settings.polymarket_private_key or "").strip()
    if not private_key:
        return {
            "configured": False,
            "address": None,
            "status": "missing_private_key",
            "message": "尚未輸入私鑰",
            "balances": _default_empty_balances("missing_private_key"),
        }

    if not private_key.startswith("0x"):
        private_key = f"0x{private_key}"

    try:
        address = Account.from_key(private_key).address
    except Exception:
        return {
            "configured": True,
            "address": None,
            "status": "invalid_private_key",
            "message": "私鑰格式無法解析",
            "balances": _default_empty_balances("invalid_private_key"),
        }

    rpc_url = settings.polygon_rpc_url.strip()
    if not rpc_url:
        return {
            "configured": True,
            "address": address,
            "status": "missing_rpc",
            "message": "已讀取錢包地址，但尚未設定 Polygon RPC",
            "balances": _default_empty_balances("missing_rpc"),
        }

    token_configs = [
        ("USDC", settings.polygon_usdc_token_address),
        ("pUSD", settings.polygon_pusd_token_address),
    ]
    async with httpx.AsyncClient(timeout=10.0) as client:
        native_task = fetch_native_balance(client, rpc_url, address)
        token_tasks = [
            fetch_erc20_balance(client, rpc_url, token_address, address)
            for _, token_address in token_configs
        ]
        portfolio_value_task = fetch_polymarket_portfolio_value(client, settings.polymarket_data_api_base_url, address)
        portfolio_positions_task = fetch_polymarket_positions(client, settings.polymarket_data_api_base_url, address)
        results = await asyncio.gather(
            native_task,
            *token_tasks,
            portfolio_value_task,
            portfolio_positions_task,
            return_exceptions=True,
        )

    balances: list[dict[str, Any]] = []
    error_count = 0
    balance_results = results[: 1 + len(token_configs)]
    portfolio_value_result = results[-2]
    portfolio_positions_result = results[-1]

    native_result = balance_results[0]
    if isinstance(native_result, Exception):
        error_count += 1
        balances.append(_empty_balance("POL", status="rpc_error", note="Polygon RPC 讀取失敗"))
    else:
        balances.append(
            {
                "symbol": "POL",
                "amount": format_units(native_result, NATIVE_TOKEN_DECIMALS),
                "status": "ok",
                "note": None,
            }
        )

    for (symbol, _), result in zip(token_configs, balance_results[1:], strict=True):
        if isinstance(result, Exception):
            error_count += 1
            balances.append(_empty_balance(symbol, status="rpc_error", note="Polygon RPC 讀取失敗"))
            continue
        balances.append(
            {
                "symbol": symbol,
                "amount": format_units(result, TOKEN_DECIMALS),
                "status": "ok",
                "note": None,
            }
        )

    portfolio = {
        "position_value": None,
        "status": "ok",
        "source": "polymarket_data_api",
        "note": None,
    }
    if isinstance(portfolio_value_result, Exception):
        portfolio["status"] = "api_error"
        portfolio["note"] = "Polymarket portfolio value read failed."
    else:
        portfolio["position_value"] = portfolio_value_result
    if isinstance(portfolio_positions_result, Exception):
        portfolio["positions"] = []
        portfolio["positions_status"] = "api_error"
    else:
        portfolio["positions"] = portfolio_positions_result
        portfolio["positions_status"] = "ok"

    if error_count == len(balance_results):
        message = "已讀取錢包地址，但 Polygon RPC 暫時讀取失敗"
        status = "rpc_error"
    elif error_count:
        message = "已讀取錢包地址，但部分餘額暫時讀取失敗"
        status = "partial_error"
    else:
        message = "已讀取錢包地址與代幣餘額"
        status = "ok"

    return {
        "configured": True,
        "address": address,
        "status": status,
        "message": message,
        "balances": balances,
        "portfolio": portfolio,
    }
