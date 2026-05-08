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
        results = await asyncio.gather(native_task, *token_tasks, return_exceptions=True)

    balances: list[dict[str, Any]] = []
    error_count = 0

    native_result = results[0]
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

    for (symbol, _), result in zip(token_configs, results[1:], strict=True):
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

    if error_count == len(results):
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
    }
