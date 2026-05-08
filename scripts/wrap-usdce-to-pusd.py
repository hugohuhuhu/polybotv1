from __future__ import annotations

import argparse
import sys
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any

import httpx
from eth_account import Account
from eth_utils import keccak

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings


TOKEN_DECIMALS = 6
CONFIRM_WORD = "WRAP"


def selector(signature: str) -> str:
    return keccak(text=signature)[:4].hex()


def pad_address(address: str) -> str:
    normalized = address.lower().removeprefix("0x")
    if len(normalized) != 40:
        raise ValueError(f"invalid address: {address}")
    return normalized.rjust(64, "0")


def pad_uint(value: int) -> str:
    if value < 0:
        raise ValueError("uint cannot be negative")
    return hex(value)[2:].rjust(64, "0")


def encode_call(signature: str, args: list[tuple[str, Any]]) -> str:
    data = selector(signature)
    for arg_type, value in args:
        if arg_type == "address":
            data += pad_address(str(value))
        elif arg_type == "uint256":
            data += pad_uint(int(value))
        else:
            raise ValueError(f"unsupported ABI type: {arg_type}")
    return "0x" + data


def rpc(client: httpx.Client, rpc_url: str, method: str, params: list[Any]) -> Any:
    response = client.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"{method} failed: {payload['error']}")
    return payload["result"]


def to_base_units(amount: str) -> int:
    try:
        value = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError(f"invalid amount: {amount}") from exc
    if value <= 0:
        raise ValueError("amount must be greater than zero")
    quantized = value.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return int(quantized * (10**TOKEN_DECIMALS))


def from_base_units(value: int) -> Decimal:
    return Decimal(value) / Decimal(10**TOKEN_DECIMALS)


def call_uint(client: httpx.Client, rpc_url: str, to: str, data: str) -> int:
    result = rpc(client, rpc_url, "eth_call", [{"to": to, "data": data}, "latest"])
    return int(result, 16)


def wait_receipt(client: httpx.Client, rpc_url: str, tx_hash: str, *, timeout_sec: int = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        receipt = rpc(client, rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            return receipt
        time.sleep(3)
    raise TimeoutError(f"timed out waiting for receipt: {tx_hash}")


def send_transaction(
    client: httpx.Client,
    rpc_url: str,
    *,
    private_key: str,
    from_address: str,
    to: str,
    data: str,
    chain_id: int,
) -> str:
    nonce = int(rpc(client, rpc_url, "eth_getTransactionCount", [from_address, "pending"]), 16)
    gas_price = int(rpc(client, rpc_url, "eth_gasPrice", []), 16)
    estimated_gas = int(
        rpc(
            client,
            rpc_url,
            "eth_estimateGas",
            [{"from": from_address, "to": to, "data": data, "value": "0x0"}],
        ),
        16,
    )
    tx = {
        "chainId": chain_id,
        "nonce": nonce,
        "to": to,
        "value": 0,
        "data": data,
        "gas": int(estimated_gas * 1.25),
        "gasPrice": gas_price,
    }
    signed = Account.sign_transaction(tx, private_key)
    raw_transaction = getattr(signed, "rawTransaction", None) or signed.raw_transaction
    raw_hex = raw_transaction.hex()
    if not raw_hex.startswith("0x"):
        raw_hex = "0x" + raw_hex
    return rpc(client, rpc_url, "eth_sendRawTransaction", [raw_hex])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wrap Polygon USDC.e into Polymarket pUSD through the official CollateralOnramp.",
    )
    parser.add_argument("--amount", required=True, help="USDC.e amount to wrap, e.g. 20.55")
    parser.add_argument("--send", action="store_true", help="Actually send approve/wrap transactions after confirmation.")
    args = parser.parse_args()

    settings = get_settings()
    private_key = (settings.polymarket_private_key or "").strip()
    if not private_key:
        print("POLYMARKET_PRIVATE_KEY is not configured.", file=sys.stderr)
        return 1
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key

    account = Account.from_key(private_key)
    wallet = account.address
    rpc_url = settings.polygon_rpc_url
    usdce = settings.polygon_usdc_e_token_address
    pusd = settings.polygon_pusd_token_address
    onramp = settings.polymarket_collateral_onramp_address
    amount_units = to_base_units(args.amount)
    amount_display = from_base_units(amount_units)

    balance_data = encode_call("balanceOf(address)", [("address", wallet)])
    allowance_data = encode_call("allowance(address,address)", [("address", wallet), ("address", onramp)])
    wrap_data = encode_call(
        "wrap(address,address,uint256)",
        [("address", usdce), ("address", wallet), ("uint256", amount_units)],
    )
    approve_data = encode_call(
        "approve(address,uint256)",
        [("address", onramp), ("uint256", amount_units)],
    )

    with httpx.Client(timeout=20.0) as client:
        chain_id = int(rpc(client, rpc_url, "eth_chainId", []), 16)
        if chain_id != settings.polymarket_chain_id:
            raise RuntimeError(f"RPC chain id is {chain_id}, expected {settings.polymarket_chain_id}")

        balance_units = call_uint(client, rpc_url, usdce, balance_data)
        allowance_units = call_uint(client, rpc_url, usdce, allowance_data)

        print("Official Polymarket pUSD wrap preview")
        print(f"Wallet: {wallet}")
        print(f"Network chain id: {chain_id}")
        print(f"USDC.e token: {usdce}")
        print(f"pUSD token: {pusd}")
        print(f"CollateralOnramp: {onramp}")
        print(f"USDC.e balance: {from_base_units(balance_units)}")
        print(f"Current USDC.e allowance to onramp: {from_base_units(allowance_units)}")
        print(f"Wrap amount: {amount_display}")
        print()

        if amount_units > balance_units:
            print("Amount exceeds USDC.e balance.", file=sys.stderr)
            return 1

        needs_approve = allowance_units < amount_units
        if needs_approve:
            print("Step 1 required: approve CollateralOnramp to spend this USDC.e amount.")
        else:
            print("Step 1 not required: existing allowance is enough.")
        print("Step 2 required: call CollateralOnramp.wrap(USDC.e, wallet, amount).")
        print()

        if not args.send:
            print("Dry run only. No transaction was signed or sent.")
            print("To execute manually, rerun with:")
            print(f"  python scripts\\wrap-usdce-to-pusd.py --amount {amount_display} --send")
            return 0

        print("This will send real Polygon transactions and spend gas.")
        print(f"Type exactly: {CONFIRM_WORD} {amount_display}")
        confirmation = input("> ").strip()
        if confirmation != f"{CONFIRM_WORD} {amount_display}":
            print("Confirmation did not match. Aborted.")
            return 1

        if needs_approve:
            approve_hash = send_transaction(
                client,
                rpc_url,
                private_key=private_key,
                from_address=wallet,
                to=usdce,
                data=approve_data,
                chain_id=chain_id,
            )
            print(f"Approve tx: {approve_hash}")
            approve_receipt = wait_receipt(client, rpc_url, approve_hash)
            if int(approve_receipt.get("status", "0x0"), 16) != 1:
                raise RuntimeError(f"approve failed: {approve_hash}")

        wrap_hash = send_transaction(
            client,
            rpc_url,
            private_key=private_key,
            from_address=wallet,
            to=onramp,
            data=wrap_data,
            chain_id=chain_id,
        )
        print(f"Wrap tx: {wrap_hash}")
        wrap_receipt = wait_receipt(client, rpc_url, wrap_hash)
        if int(wrap_receipt.get("status", "0x0"), 16) != 1:
            raise RuntimeError(f"wrap failed: {wrap_hash}")

        new_pusd_balance = call_uint(client, rpc_url, pusd, balance_data)
        print(f"Done. pUSD balance is now: {from_base_units(new_pusd_balance)}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
