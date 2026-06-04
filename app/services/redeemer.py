from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
from typing import Any

import httpx
from eth_account import Account
from eth_utils import keccak

from app.config import Settings
from app.storage.repositories import ScannerRepository


TOKEN_DECIMALS = 6
ZERO_COLLECTION_ID = "0x" + ("0" * 64)


def _loss_autopsy_risk_settings(settings: Settings) -> dict[str, Any]:
    return {
        "taker_exit_price": settings.near_close_taker_exit_price,
        "hard_stop_offset": settings.near_close_hard_stop_offset,
        "emergency_slippage": settings.near_close_emergency_slippage,
        "hedge_enabled": settings.near_close_post_fill_hedge_enabled,
        "hedge_shadow_enabled": settings.near_close_post_fill_hedge_shadow_enabled,
        "hedge_default_price": settings.near_close_hedge_default_price,
        "hedge_min_locked_profit": settings.near_close_hedge_min_locked_profit,
    }


@dataclass
class RedeemResult:
    token_id: str
    market_slug: str
    outcome_label: str
    redeemed_size: float
    redeem_tx: str | None = None
    wrap_tx: str | None = None
    approve_tx: str | None = None
    status: str = "skipped"
    message: str = ""
    trade_ids: list[int] = field(default_factory=list)


def _selector(signature: str) -> str:
    return keccak(text=signature)[:4].hex()


def _pad_address(address: str) -> str:
    normalized = address.lower().removeprefix("0x")
    if len(normalized) != 40:
        raise ValueError(f"invalid address: {address}")
    return normalized.rjust(64, "0")


def _pad_uint(value: int) -> str:
    if value < 0:
        raise ValueError("uint cannot be negative")
    return hex(value)[2:].rjust(64, "0")


def _pad_bytes32(value: str) -> str:
    normalized = value.lower().removeprefix("0x")
    if len(normalized) != 64:
        raise ValueError(f"invalid bytes32: {value}")
    return normalized


def _encode_call(signature: str, args: list[tuple[str, Any]]) -> str:
    data = _selector(signature)
    dynamic_parts: list[str] = []
    head_slots: list[str] = []
    for arg_type, value in args:
        if arg_type == "address":
            head_slots.append(_pad_address(str(value)))
        elif arg_type == "bytes32":
            head_slots.append(_pad_bytes32(str(value)))
        elif arg_type == "uint256":
            head_slots.append(_pad_uint(int(value)))
        elif arg_type == "uint256[]":
            values = [int(item) for item in value]
            offset = 32 * len(args) + 32 * sum(len(part) // 64 for part in dynamic_parts)
            head_slots.append(_pad_uint(offset))
            dynamic_parts.append(_pad_uint(len(values)) + "".join(_pad_uint(item) for item in values))
        else:
            raise ValueError(f"unsupported ABI type: {arg_type}")
    return "0x" + data + "".join(head_slots) + "".join(dynamic_parts)


def _rpc(client: httpx.Client, rpc_url: str, method: str, params: list[Any]) -> Any:
    response = client.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"{method} failed: {payload['error']}")
    return payload["result"]


def _call_uint(client: httpx.Client, rpc_url: str, to: str, data: str) -> int:
    return int(_rpc(client, rpc_url, "eth_call", [{"to": to, "data": data}, "latest"]), 16)


def _wait_receipt(client: httpx.Client, rpc_url: str, tx_hash: str, *, timeout_sec: int = 180) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        receipt = _rpc(client, rpc_url, "eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            return receipt
        time.sleep(3)
    raise TimeoutError(f"timed out waiting for receipt: {tx_hash}")


def _send_transaction(
    client: httpx.Client,
    settings: Settings,
    *,
    private_key: str,
    from_address: str,
    to: str,
    data: str,
) -> str:
    nonce = int(_rpc(client, settings.polygon_rpc_url, "eth_getTransactionCount", [from_address, "pending"]), 16)
    gas_price = int(_rpc(client, settings.polygon_rpc_url, "eth_gasPrice", []), 16)
    estimated_gas = int(
        _rpc(
            client,
            settings.polygon_rpc_url,
            "eth_estimateGas",
            [{"from": from_address, "to": to, "data": data, "value": "0x0"}],
        ),
        16,
    )
    tx = {
        "chainId": settings.polymarket_chain_id,
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
    return _rpc(client, settings.polygon_rpc_url, "eth_sendRawTransaction", [raw_hex])


def _base_units_to_float(value: int) -> float:
    return value / (10**TOKEN_DECIMALS)


def _decimal_to_base_units(value: float) -> int:
    amount = Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
    return int(amount * (10**TOKEN_DECIMALS))


def _wallet_address(settings: Settings, private_key: str) -> str:
    return settings.polymarket_funder_address or Account.from_key(private_key).address


def _jsonish_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_gamma_market_from_event(payload: Any, market_slug: str) -> dict[str, Any] | None:
    events = payload if isinstance(payload, list) else [payload]
    for event in events:
        if not isinstance(event, dict):
            continue
        markets = event.get("markets")
        if not isinstance(markets, list):
            continue
        for market in markets:
            if not isinstance(market, dict):
                continue
            if market_slug and str(market.get("slug") or "") != market_slug:
                continue
            return market
        if markets and isinstance(markets[0], dict):
            return markets[0]
    return None


def _fetch_latest_market(
    client: httpx.Client,
    settings: Settings,
    market_id: str,
    fallback: dict[str, Any],
    *,
    market_slug: str = "",
) -> dict[str, Any]:
    base_url = settings.gamma_base_url.rstrip("/")
    if market_id:
        response = client.get(f"{base_url}/markets/{market_id}")
        if response.status_code < 400:
            payload = response.json()
            if isinstance(payload, dict):
                return payload
    if market_slug:
        response = client.get(f"{base_url}/events", params={"slug": market_slug})
        if response.status_code < 400:
            market = _extract_gamma_market_from_event(response.json(), market_slug)
            if market is not None:
                return market
        response = client.get(f"{base_url}/markets", params={"slug": market_slug})
        if response.status_code < 400:
            payload = response.json()
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                return payload[0]
            if isinstance(payload, dict):
                return payload
    return fallback


def _fetch_redeemable_wallet_positions(client: httpx.Client, settings: Settings, wallet: str) -> list[dict[str, Any]]:
    response = client.get(
        f"{settings.polymarket_data_api_base_url.rstrip('/')}/positions",
        params={"user": wallet, "limit": 500, "sizeThreshold": 0},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        return []
    positions: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        size = _float_or_none(item.get("size"))
        if item.get("redeemable") is True and size is not None and size > 0:
            positions.append(item)
    return positions


def _wallet_position_to_redeem_candidate(position: dict[str, Any], *, trade_ids: list[int] | None = None) -> dict[str, Any] | None:
    token_id = str(position.get("asset") or position.get("assetId") or "").strip()
    condition_id = str(position.get("conditionId") or "").strip()
    slug = str(position.get("slug") or position.get("eventSlug") or "").strip()
    outcome_label = str(position.get("outcome") or "").strip()
    if not token_id or not condition_id or not slug:
        return None
    try:
        outcome_index = int(position.get("outcomeIndex"))
    except (TypeError, ValueError):
        return None
    opposite_asset = str(position.get("oppositeAsset") or "").strip()
    opposite_outcome = str(position.get("oppositeOutcome") or "").strip()
    token_ids = [token_id]
    labels = [outcome_label]
    if opposite_asset:
        if outcome_index == 0:
            token_ids.append(opposite_asset)
        else:
            token_ids.insert(0, opposite_asset)
    if opposite_outcome:
        if outcome_index == 0:
            labels.append(opposite_outcome)
        else:
            labels.insert(0, opposite_outcome)
    return {
        "id": None,
        "opportunity_id": f"wallet:{token_id}",
        "action": "BUY",
        "token_id": token_id,
        "market_slug": slug,
        "outcome_label": outcome_label,
        "target_price": _float_or_none(position.get("avgPrice")) or 0.0,
        "requested_size": _float_or_none(position.get("size")) or 0.0,
        "order_id": f"wallet:{token_id}",
        "status": "REDEEMABLE",
        "response_json": json.dumps(position),
        "created_at": str(position.get("updatedAt") or position.get("createdAt") or ""),
        "market": {
            "market_id": str(position.get("marketId") or ""),
            "slug": slug,
            "question": str(position.get("title") or slug),
            "end_date": str(position.get("endDate") or ""),
            "token_ids": token_ids,
            "outcome_labels": labels,
            "raw": {"conditionId": condition_id},
        },
        "outcome_index": outcome_index,
        "trade_ids": list(trade_ids or []),
        "wallet_position": position,
    }


def _merge_wallet_redeem_candidates(
    candidates: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    repository: ScannerRepository,
) -> list[dict[str, Any]]:
    merged = list(candidates)
    by_token = {str(candidate.get("token_id") or ""): candidate for candidate in merged}
    for position in positions:
        token_id = str(position.get("asset") or position.get("assetId") or "").strip()
        if not token_id:
            continue
        trade_ids = repository.redeem_candidate_trade_ids_for_token(token_id)
        candidate = _wallet_position_to_redeem_candidate(position, trade_ids=trade_ids)
        if candidate is None:
            continue
        existing = by_token.get(token_id)
        if existing is None:
            by_token[token_id] = candidate
            merged.append(candidate)
            continue
        existing["wallet_position"] = position
        existing["trade_ids"] = sorted({*existing.get("trade_ids", []), *trade_ids})
        market = dict(existing.get("market") or {})
        raw = dict(market.get("raw") or {})
        raw.setdefault("conditionId", position.get("conditionId"))
        market["raw"] = raw
        market.setdefault("slug", candidate["market"]["slug"])
        existing["market"] = market
    return merged


def _outcome_index_for_token(payload: dict[str, Any], token_id: str, fallback: int) -> int:
    token_ids = [str(value) for value in _jsonish_list(payload.get("clobTokenIds"))]
    if token_id in token_ids:
        return token_ids.index(token_id)
    return fallback


def _is_winning_market(payload: dict[str, Any], outcome_index: int) -> bool:
    if not bool(payload.get("closed")):
        return False
    raw_prices = payload.get("outcomePrices") or []
    raw_prices = _jsonish_list(raw_prices)
    try:
        return float(raw_prices[outcome_index]) >= 0.999
    except (IndexError, TypeError, ValueError):
        return False


def _is_closed_losing_market(payload: dict[str, Any], outcome_index: int) -> bool:
    if not bool(payload.get("closed")):
        return False
    raw_prices = payload.get("outcomePrices") or []
    raw_prices = _jsonish_list(raw_prices)
    try:
        return float(raw_prices[outcome_index]) <= 0.001
    except (IndexError, TypeError, ValueError):
        return False


def _ctf_balance_data(wallet: str, token_id: str) -> str:
    return _encode_call("balanceOf(address,uint256)", [("address", wallet), ("uint256", int(token_id))])


def _erc20_balance_data(wallet: str) -> str:
    return _encode_call("balanceOf(address)", [("address", wallet)])


def _erc20_allowance_data(owner: str, spender: str) -> str:
    return _encode_call("allowance(address,address)", [("address", owner), ("address", spender)])


def _wrap_usdce_to_pusd(
    client: httpx.Client,
    settings: Settings,
    *,
    private_key: str,
    wallet: str,
    amount_units: int,
) -> tuple[str | None, str | None]:
    if amount_units <= 0:
        return None, None
    usdce = settings.polygon_usdc_e_token_address
    onramp = settings.polymarket_collateral_onramp_address
    allowance = _call_uint(client, settings.polygon_rpc_url, usdce, _erc20_allowance_data(wallet, onramp))
    approve_tx = None
    if allowance < amount_units:
        allowance_target = max(
            amount_units,
            _decimal_to_base_units(settings.auto_redeem_wrap_allowance_usdce),
        )
        approve_data = _encode_call("approve(address,uint256)", [("address", onramp), ("uint256", allowance_target)])
        approve_tx = _send_transaction(client, settings, private_key=private_key, from_address=wallet, to=usdce, data=approve_data)
        receipt = _wait_receipt(client, settings.polygon_rpc_url, approve_tx)
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise RuntimeError(f"USDC.e approve failed: {approve_tx}")
    wrap_data = _encode_call(
        "wrap(address,address,uint256)",
        [("address", usdce), ("address", wallet), ("uint256", amount_units)],
    )
    wrap_tx = _send_transaction(client, settings, private_key=private_key, from_address=wallet, to=onramp, data=wrap_data)
    receipt = _wait_receipt(client, settings.polygon_rpc_url, wrap_tx)
    if int(receipt.get("status", "0x0"), 16) != 1:
        raise RuntimeError(f"pUSD wrap failed: {wrap_tx}")
    return approve_tx, wrap_tx


def run_auto_redeem_once(
    settings: Settings,
    repository: ScannerRepository,
    *,
    token_ids: set[str] | None = None,
) -> list[RedeemResult]:
    if not settings.auto_redeem_enabled:
        return []
    private_key = (settings.polymarket_private_key or "").strip()
    if not private_key:
        return []
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    wallet = _wallet_address(settings, private_key)
    selected = token_ids or set()
    results: list[RedeemResult] = []
    candidates = repository.redeem_candidate_live_trades(limit=100)
    with httpx.Client(timeout=20.0) as client:
        try:
            wallet_positions = _fetch_redeemable_wallet_positions(client, settings, wallet)
        except Exception as exc:
            wallet_positions = []
            repository.save_execution_event(
                source="auto-redeem",
                mode="live",
                opportunity_id=None,
                status="portfolio_scan_failed",
                message=str(exc),
                details={"stage": "fetch_redeemable_wallet_positions"},
            )
        candidates = _merge_wallet_redeem_candidates(candidates, wallet_positions, repository)
        chain_id = int(_rpc(client, settings.polygon_rpc_url, "eth_chainId", []), 16)
        if chain_id != settings.polymarket_chain_id:
            raise RuntimeError(f"RPC chain id is {chain_id}, expected {settings.polymarket_chain_id}")
        usdce_balance_data = _erc20_balance_data(wallet)
        for candidate in candidates:
            token_id = str(candidate["token_id"])
            if selected and token_id not in selected:
                continue
            market = dict(candidate.get("market") or {})
            market_slug = str(market.get("slug") or candidate.get("market_slug") or "")
            payload = _fetch_latest_market(
                client,
                settings,
                str(market.get("market_id") or ""),
                dict(market.get("raw") or {}),
                market_slug=market_slug,
            )
            outcome_index = _outcome_index_for_token(payload, token_id, int(candidate["outcome_index"]))
            result = RedeemResult(
                token_id=token_id,
                market_slug=market_slug,
                outcome_label=str(candidate.get("outcome_label") or ""),
                redeemed_size=0.0,
                trade_ids=[int(value) for value in candidate.get("trade_ids", [])],
            )
            condition_id = str(payload.get("conditionId") or market.get("raw", {}).get("conditionId") or "")
            if not _is_winning_market(payload, outcome_index):
                if _is_closed_losing_market(payload, outcome_index):
                    settlement_details = {
                        "market_slug": result.market_slug,
                        "outcome_label": result.outcome_label,
                        "token_id": token_id,
                        "outcome_index": outcome_index,
                        "outcome_prices": payload.get("outcomePrices"),
                    }
                    message = "Conditional token expired worthless; no redeemable payout."
                    if condition_id:
                        balance_units = _call_uint(
                            client,
                            settings.polygon_rpc_url,
                            settings.polymarket_ctf_address,
                            _ctf_balance_data(wallet, token_id),
                        )
                        settlement_details["condition_id"] = condition_id
                        settlement_details["ctf_units"] = balance_units
                        if balance_units > 0:
                            usdce_before = _call_uint(
                                client,
                                settings.polygon_rpc_url,
                                settings.polygon_usdc_e_token_address,
                                usdce_balance_data,
                            )
                            index_set = 1 << outcome_index
                            redeem_data = _encode_call(
                                "redeemPositions(address,bytes32,bytes32,uint256[])",
                                [
                                    ("address", settings.polygon_usdc_e_token_address),
                                    ("bytes32", ZERO_COLLECTION_ID),
                                    ("bytes32", condition_id),
                                    ("uint256[]", [index_set]),
                                ],
                            )
                            redeem_tx = _send_transaction(
                                client,
                                settings,
                                private_key=private_key,
                                from_address=wallet,
                                to=settings.polymarket_ctf_address,
                                data=redeem_data,
                            )
                            receipt = _wait_receipt(client, settings.polygon_rpc_url, redeem_tx)
                            if int(receipt.get("status", "0x0"), 16) != 1:
                                raise RuntimeError(f"zero-payout redeem failed: {redeem_tx}")
                            usdce_after = _call_uint(
                                client,
                                settings.polygon_rpc_url,
                                settings.polygon_usdc_e_token_address,
                                usdce_balance_data,
                            )
                            settlement_details.update(
                                {
                                    "index_set": index_set,
                                    "redeem_tx": redeem_tx,
                                    "usdce_delta": _base_units_to_float(max(0, usdce_after - usdce_before)),
                                    "zero_payout_redeem": True,
                                }
                            )
                            result.redeem_tx = redeem_tx
                            result.redeemed_size = _base_units_to_float(balance_units)
                            message = "Redeemed zero-payout losing conditional token; shares were burned."
                    if result.trade_ids:
                        repository.mark_live_trade_ids_status(result.trade_ids, "settled_lost")
                        repository.save_loss_autopsy(
                            result.trade_ids,
                            risk_settings=_loss_autopsy_risk_settings(settings),
                            settlement_details=settlement_details,
                        )
                    repository.save_execution_event(
                        source="auto-redeem",
                        mode="live",
                        opportunity_id=str(candidate.get("opportunity_id") or ""),
                        status="settled_lost",
                        message=message,
                        details=settlement_details,
                    )
                    result.status = "settled_lost"
                    result.message = "Market is closed and this token settled at 0."
                    if result.redeem_tx:
                        result.message += " Zero-payout redeem sent to clear wallet position."
                else:
                    result.message = "Market is not closed with this outcome at 1.00 yet."
                results.append(result)
                continue
            if not condition_id:
                result.message = "Missing conditionId."
                results.append(result)
                continue
            balance_units = _call_uint(
                client,
                settings.polygon_rpc_url,
                settings.polymarket_ctf_address,
                _ctf_balance_data(wallet, token_id),
            )
            if balance_units <= 0:
                repository.mark_live_trade_ids_status(result.trade_ids, "redeemed")
                repository.save_execution_event(
                    source="auto-redeem",
                    mode="live",
                    opportunity_id=str(candidate.get("opportunity_id") or ""),
                    status="redeemed",
                    message="Winning conditional token has no wallet balance; marking local position complete.",
                    details={
                        "market_slug": result.market_slug,
                        "outcome_label": result.outcome_label,
                        "token_id": token_id,
                        "condition_id": condition_id,
                        "ctf_units": balance_units,
                        "reason": "no_conditional_token_balance",
                    },
                )
                result.status = "redeemed"
                result.message = "No conditional token balance to redeem; local position marked complete."
                results.append(result)
                continue
            usdce_before = _call_uint(client, settings.polygon_rpc_url, settings.polygon_usdc_e_token_address, usdce_balance_data)
            index_set = 1 << outcome_index
            redeem_data = _encode_call(
                "redeemPositions(address,bytes32,bytes32,uint256[])",
                [
                    ("address", settings.polygon_usdc_e_token_address),
                    ("bytes32", ZERO_COLLECTION_ID),
                    ("bytes32", condition_id),
                    ("uint256[]", [index_set]),
                ],
            )
            redeem_tx = _send_transaction(
                client,
                settings,
                private_key=private_key,
                from_address=wallet,
                to=settings.polymarket_ctf_address,
                data=redeem_data,
            )
            receipt = _wait_receipt(client, settings.polygon_rpc_url, redeem_tx)
            if int(receipt.get("status", "0x0"), 16) != 1:
                raise RuntimeError(f"redeem failed: {redeem_tx}")
            usdce_after = _call_uint(client, settings.polygon_rpc_url, settings.polygon_usdc_e_token_address, usdce_balance_data)
            usdce_delta = max(0, usdce_after - usdce_before)
            approve_tx = None
            wrap_tx = None
            if usdce_delta >= _decimal_to_base_units(settings.auto_redeem_min_usdce):
                approve_tx, wrap_tx = _wrap_usdce_to_pusd(
                    client,
                    settings,
                    private_key=private_key,
                    wallet=wallet,
                    amount_units=usdce_delta,
                )
            repository.mark_live_trade_ids_status(result.trade_ids, "redeemed")
            repository.save_execution_event(
                source="auto-redeem",
                mode="live",
                opportunity_id=str(candidate.get("opportunity_id") or ""),
                status="redeemed",
                message="Redeemed winning conditional token and wrapped USDC.e to pUSD.",
                details={
                    "market_slug": result.market_slug,
                    "outcome_label": result.outcome_label,
                    "token_id": token_id,
                    "condition_id": condition_id,
                    "index_set": index_set,
                    "ctf_units": balance_units,
                    "redeem_tx": redeem_tx,
                    "approve_tx": approve_tx,
                    "wrap_tx": wrap_tx,
                    "usdce_delta": _base_units_to_float(usdce_delta),
                },
            )
            result.status = "redeemed"
            result.message = "Redeemed and wrapped to pUSD."
            result.redeemed_size = _base_units_to_float(balance_units)
            result.redeem_tx = redeem_tx
            result.approve_tx = approve_tx
            result.wrap_tx = wrap_tx
            results.append(result)
    return results
