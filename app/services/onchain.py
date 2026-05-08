from __future__ import annotations

from typing import Any

import httpx


BALANCE_OF_SELECTOR = "70a08231"
ALLOWANCE_SELECTOR = "dd62ed3e"
TOKEN_DECIMALS = 6
NATIVE_TOKEN_DECIMALS = 18


def encode_address_param(address: str) -> str:
    """Encode an EVM address as a 32-byte ABI parameter."""

    return address.removeprefix("0x").lower().rjust(64, "0")


async def rpc_call(
    client: httpx.AsyncClient,
    rpc_url: str,
    method: str,
    params: list[Any],
) -> Any:
    """Call a Polygon JSON-RPC method and return its result."""

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    }
    response = await client.post(rpc_url, json=payload)
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise ValueError(f"Polygon RPC error: {body['error']}")
    return body.get("result")


async def fetch_chain_id(client: httpx.AsyncClient, rpc_url: str) -> int:
    """Return the Polygon RPC chain ID."""

    result = await rpc_call(client, rpc_url, "eth_chainId", [])
    if not isinstance(result, str):
        raise ValueError("Polygon RPC did not return a hex chain ID.")
    return int(result, 16)


async def fetch_native_balance(
    client: httpx.AsyncClient,
    rpc_url: str,
    wallet_address: str,
) -> int:
    """Return native POL balance in wei."""

    result = await rpc_call(client, rpc_url, "eth_getBalance", [wallet_address, "latest"])
    if not isinstance(result, str):
        raise ValueError("Polygon RPC did not return a hex native balance result.")
    return int(result, 16)


async def fetch_erc20_balance(
    client: httpx.AsyncClient,
    rpc_url: str,
    token_address: str,
    wallet_address: str,
) -> int:
    """Return ERC-20 token balance in base units."""

    token = token_address.strip().lower()
    if not token:
        raise ValueError("ERC-20 token address is empty.")
    wallet = encode_address_param(wallet_address)
    result = await rpc_call(
        client,
        rpc_url,
        "eth_call",
        [
            {
                "to": token,
                "data": f"0x{BALANCE_OF_SELECTOR}{wallet}",
            },
            "latest",
        ],
    )
    if not isinstance(result, str):
        raise ValueError("Polygon RPC did not return a hex balance result.")
    return int(result, 16)


async def fetch_erc20_allowance(
    client: httpx.AsyncClient,
    rpc_url: str,
    token_address: str,
    owner_address: str,
    spender_address: str,
) -> int:
    """Return ERC-20 allowance in base units for owner/spender."""

    token = token_address.strip().lower()
    spender = spender_address.strip()
    if not token or not spender:
        raise ValueError("ERC-20 token or spender address is empty.")
    owner = encode_address_param(owner_address)
    encoded_spender = encode_address_param(spender)
    result = await rpc_call(
        client,
        rpc_url,
        "eth_call",
        [
            {
                "to": token,
                "data": f"0x{ALLOWANCE_SELECTOR}{owner}{encoded_spender}",
            },
            "latest",
        ],
    )
    if not isinstance(result, str):
        raise ValueError("Polygon RPC did not return a hex allowance result.")
    return int(result, 16)


def format_units(value: int, decimals: int = TOKEN_DECIMALS) -> float:
    """Convert integer token units into a decimal display value."""

    return value / (10**decimals)
