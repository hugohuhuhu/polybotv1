from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from threading import Lock
from typing import Any

from eth_account import Account

try:
    from py_clob_client_v2 import (
        BalanceAllowanceParams,
        ClobClient,
        MarketOrderArgs,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
        Side,
    )

    _V2_SDK_IMPORT_ERROR: ImportError | None = None
except ImportError as exc:
    _V2_SDK_IMPORT_ERROR = exc

    class ClobClient:  # type: ignore[no-redef]
        pass

    class OrderType:  # type: ignore[no-redef]
        GTC = "GTC"
        FOK = "FOK"
        GTD = "GTD"
        FAK = "FAK"

    class Side:  # type: ignore[no-redef]
        BUY = "BUY"
        SELL = "SELL"

    @dataclass(slots=True)
    class OrderArgs:  # type: ignore[no-redef]
        token_id: str
        price: float
        size: float
        side: str
        expiration: int = 0

    @dataclass(slots=True)
    class MarketOrderArgs:  # type: ignore[no-redef]
        token_id: str
        amount: float
        side: str
        price: float = 0.0
        order_type: str = OrderType.FOK
        user_usdc_balance: float = 0.0

    @dataclass(slots=True)
    class PartialCreateOrderOptions:  # type: ignore[no-redef]
        tick_size: str | None = None
        neg_risk: bool | None = None

    @dataclass(slots=True)
    class BalanceAllowanceParams:  # type: ignore[no-redef]
        asset_type: str | None = None
        token_id: str | None = None
        signature_type: int = -1

from app.config import Settings
from app.models.core import ExecutionPlan, LiveExecutionLegResult, LiveExecutionResult
from app.services.onchain import TOKEN_DECIMALS, format_units
from app.strategy.execution_planner import LiveTradingAdapter


class LiveTradingError(RuntimeError):
    """Raised when a live-trading precondition is not satisfied."""


@dataclass(slots=True)
class AllowanceSnapshot:
    """Normalized balance/allowance payload returned by the CLOB client."""

    balance: float
    allowances: dict[str, float]
    raw: dict[str, Any]

    def allowance_for(self, address: str) -> float:
        return self.allowances.get(address.lower(), 0.0)


def is_clob_v2_sdk_available() -> bool:
    return _V2_SDK_IMPORT_ERROR is None


def normalize_private_key(private_key: str) -> str:
    normalized = private_key.strip()
    return normalized if normalized.startswith("0x") else f"0x{normalized}"


def resolve_funder_address(settings: Settings, private_key: str) -> str | None:
    funder = (settings.polymarket_funder_address or "").strip() or None
    if settings.polymarket_signature_type == 0:
        return funder or Account.from_key(private_key).address
    return funder


def create_authenticated_clob_v2_client(settings: Settings) -> ClobClient:
    if _V2_SDK_IMPORT_ERROR is not None:
        raise LiveTradingError(
            "py-clob-client-v2 is not installed. Install project dependencies before enabling live trading."
        ) from _V2_SDK_IMPORT_ERROR

    private_key = normalize_private_key(settings.polymarket_private_key or "")
    funder = resolve_funder_address(settings, private_key)
    if settings.polymarket_signature_type != 0 and funder is None:
        raise LiveTradingError("POLYMARKET_FUNDER_ADDRESS is required for non-EOA signature types.")

    base_client = ClobClient(
        host=settings.clob_base_url,
        chain_id=settings.polymarket_chain_id,
        key=private_key,
        signature_type=settings.polymarket_signature_type,
        funder=funder,
    )
    api_creds = base_client.create_or_derive_api_key()
    return ClobClient(
        host=settings.clob_base_url,
        chain_id=settings.polymarket_chain_id,
        key=private_key,
        creds=api_creds,
        signature_type=settings.polymarket_signature_type,
        funder=funder,
        retry_on_error=True,
    )


def normalize_limit_order_size(action: str, shares: float) -> float:
    normalized_action = action.upper()
    if normalized_action in {"BUY", "SELL"}:
        return round(max(shares, 0.0), 6)
    raise ValueError(f"Unsupported action: {action}")


def normalize_market_order_amount(action: str, shares: float, price: float) -> float:
    """Normalize a V2 market-order amount.

    BUY market orders are funded in pUSD collateral with 2 decimal places.
    SELL market orders are sized in ConditionalToken shares with 2 decimal places.
    """

    normalized_action = action.upper()
    if normalized_action == "BUY":
        return float(
            (Decimal(str(max(shares, 0.0))) * Decimal(str(max(price, 0.0)))).quantize(
                Decimal("0.01"),
                rounding=ROUND_DOWN,
            )
        )
    if normalized_action == "SELL":
        return float(Decimal(str(max(shares, 0.0))).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    raise ValueError(f"Unsupported action: {action}")


def normalize_limit_order_price(action: str, price: float, tick_size: str | float | None) -> float:
    normalized_action = action.upper()
    if normalized_action not in {"BUY", "SELL"}:
        raise ValueError(f"Unsupported action: {action}")
    tick = Decimal(str(tick_size or "0.01"))
    if tick <= 0:
        tick = Decimal("0.01")
    value = Decimal(str(max(price, 0.0)))
    rounding = ROUND_DOWN if normalized_action == "BUY" else ROUND_UP
    units = (value / tick).to_integral_value(rounding=rounding)
    normalized = units * tick
    return float(normalized.quantize(tick))


def _normalize_balance_allowance_payload(payload: dict[str, Any]) -> AllowanceSnapshot:
    raw_allowances = payload.get("allowances") or {}
    allowances = {
        str(address).lower(): format_units(int(raw_value), TOKEN_DECIMALS)
        for address, raw_value in raw_allowances.items()
    }
    balance = format_units(int(payload.get("balance", "0")), TOKEN_DECIMALS)
    return AllowanceSnapshot(balance=balance, allowances=allowances, raw=payload)


def read_clob_collateral_status(client: ClobClient, *, signature_type: int) -> AllowanceSnapshot:
    payload = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type="COLLATERAL",
            signature_type=signature_type,
        )
    )
    return _normalize_balance_allowance_payload(payload)


def read_clob_conditional_status(
    client: ClobClient,
    *,
    token_id: str,
    signature_type: int,
) -> AllowanceSnapshot:
    payload = client.get_balance_allowance(
        BalanceAllowanceParams(
            asset_type="CONDITIONAL",
            token_id=token_id,
            signature_type=signature_type,
        )
    )
    return _normalize_balance_allowance_payload(payload)


class PolymarketLiveTradingAdapter(LiveTradingAdapter):
    """Live trading adapter for the Polymarket CLOB V2 / pUSD production stack."""

    SUPPORTED_ORDER_TYPES = {"FAK", "FOK", "GTC", "GTD"}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client: ClobClient | None = None
        self._client_signature: tuple[str, int, int, str | None, str] | None = None
        self._client_lock = Lock()

    async def execute(self, plan: ExecutionPlan) -> LiveExecutionResult:
        return await asyncio.to_thread(self._execute_sync, plan)

    async def post_heartbeat(self) -> object:
        return await asyncio.to_thread(self._post_heartbeat_sync)

    async def cancel_orders(self, order_ids: list[str]) -> object:
        return await asyncio.to_thread(self._cancel_orders_sync, order_ids)

    async def get_open_orders(self) -> object:
        return await asyncio.to_thread(self._get_open_orders_sync)

    async def get_trades(self, **params: Any) -> object:
        return await asyncio.to_thread(self._get_trades_sync, params)

    def _post_heartbeat_sync(self) -> object:
        client = self._get_authenticated_client()
        if not hasattr(client, "post_heartbeat"):
            raise LiveTradingError("CLOB client does not expose post_heartbeat.")
        return client.post_heartbeat()

    def _cancel_orders_sync(self, order_ids: list[str]) -> object:
        client = self._get_authenticated_client()
        if not order_ids:
            return {"cancelled": 0}
        if hasattr(client, "cancel_orders"):
            return client.cancel_orders(order_ids)
        if hasattr(client, "cancel_order"):
            return [client.cancel_order(order_id) for order_id in order_ids]
        raise LiveTradingError("CLOB client does not expose order cancellation.")

    def _get_open_orders_sync(self) -> object:
        client = self._get_authenticated_client()
        if not hasattr(client, "get_open_orders"):
            raise LiveTradingError("CLOB client does not expose get_open_orders.")
        return client.get_open_orders()

    def _get_trades_sync(self, params: dict[str, Any]) -> object:
        client = self._get_authenticated_client()
        if not hasattr(client, "get_trades"):
            raise LiveTradingError("CLOB client does not expose get_trades.")
        return client.get_trades(**params)

    def _execute_sync(self, plan: ExecutionPlan) -> LiveExecutionResult:
        self._validate_plan(plan)
        client = self._get_authenticated_client()
        leg_results: list[LiveExecutionLegResult] = []
        try:
            for index, leg in enumerate(plan.legs, start=1):
                desired_shares = min(leg.size, self.settings.live_max_order_size)
                if desired_shares <= 0:
                    raise LiveTradingError("Requested size is zero after applying LIVE_MAX_ORDER_SIZE.")

                order_book = client.get_order_book(leg.token_id)
                tick_size = self._resolve_tick_size(client, leg.token_id, order_book)
                neg_risk = self._resolve_neg_risk(client, leg.token_id, order_book)
                options = PartialCreateOrderOptions(tick_size=tick_size, neg_risk=neg_risk)
                side = Side.BUY if leg.action.upper() == "BUY" else Side.SELL
                normalized_price = normalize_limit_order_price(leg.action, float(leg.target_price), tick_size)
                exchange_address = self.settings.polymarket_exchange_spender_address

                collateral_status = None
                leg_order_type = self._leg_order_type(leg)
                if self._uses_market_order(leg_order_type):
                    submitted_size = normalize_market_order_amount(
                        leg.action,
                        desired_shares,
                        normalized_price,
                    )
                    if submitted_size <= 0:
                        raise LiveTradingError("Market order amount rounded down to zero.")
                    if leg.action.upper() == "BUY":
                        collateral_status = read_clob_collateral_status(
                            client,
                            signature_type=self.settings.polymarket_signature_type,
                        )
                    order = client.create_market_order(
                        MarketOrderArgs(
                            token_id=leg.token_id,
                            amount=float(submitted_size),
                            side=side,
                            price=normalized_price,
                            order_type=self._order_type_enum(leg_order_type),
                            user_usdc_balance=collateral_status.balance if collateral_status else 0.0,
                        ),
                        options,
                    )
                    submission_kind = "market"
                else:
                    submitted_size = normalize_limit_order_size(
                        leg.action,
                        desired_shares,
                    )
                    order = client.create_order(
                        OrderArgs(
                            token_id=leg.token_id,
                            price=normalized_price,
                            size=float(submitted_size),
                            side=side,
                            expiration=self._expiration_timestamp(leg),
                        ),
                        options,
                    )
                    submission_kind = "limit"
                required_collateral, expected_shares = self._extract_signed_amounts(order, side)
                self._validate_allowance_and_balance(
                    client=client,
                    token_id=leg.token_id,
                    leg_action=leg.action,
                    required_collateral=required_collateral,
                    required_shares=expected_shares,
                    exchange_address=exchange_address,
                    collateral_status=collateral_status,
                )
                response = client.post_order(
                    order,
                    order_type=self._order_type_enum(leg_order_type),
                    post_only=bool(leg.post_only),
                )
                raw_response = response if isinstance(response, dict) else {"response": repr(response)}
                order_id = raw_response.get("orderID") or raw_response.get("orderId") or raw_response.get("id")
                raw_response = {
                    **raw_response,
                    "desired_shares": desired_shares,
                    "submitted_size": submitted_size,
                    "submitted_price": normalized_price,
                    "requested_price": float(leg.target_price),
                    "resolved_tick_size": tick_size,
                    "required_collateral": required_collateral,
                    "expected_shares": expected_shares,
                    "exchange_address": exchange_address,
                    "collateral_symbol": "pUSD",
                    "stack": "clob_v2_pusd",
                    "size_mode": "market_collateral_buy_shares_sell"
                    if submission_kind == "market"
                    else "limit_shares",
                    "submission_kind": submission_kind,
                    "order_type": leg_order_type,
                    "post_only": bool(leg.post_only),
                    "expiration_sec": leg.expiration_sec,
                    "expiration": self._expiration_timestamp(leg),
                    **(leg.metadata if isinstance(leg.metadata, dict) else {}),
                }
                leg_results.append(
                    LiveExecutionLegResult(
                        leg_index=index,
                        action=leg.action,
                        token_id=leg.token_id,
                        market_slug=leg.market_slug,
                        outcome_label=leg.outcome_label,
                        target_price=normalized_price,
                        requested_size=float(desired_shares),
                        order_id=str(order_id) if order_id is not None else None,
                        status="submitted",
                        response=raw_response,
                    )
                )
        except Exception as exc:
            cleanup_message = self._attempt_cleanup(client, leg_results)
            status = "partial_failure" if leg_results else "failed"
            message = str(exc)
            if cleanup_message:
                message = f"{message} Cleanup: {cleanup_message}"
            return LiveExecutionResult(
                opportunity_id=plan.opportunity_id,
                status=status,
                message=message,
                order_type=self._result_order_type(plan),
                leg_results=leg_results,
            )
        return LiveExecutionResult(
            opportunity_id=plan.opportunity_id,
            status="submitted",
            message="Submitted live order legs through the Polymarket CLOB V2 / pUSD stack.",
            order_type=self._result_order_type(plan),
            leg_results=leg_results,
        )

    def _validate_allowance_and_balance(
        self,
        *,
        client: ClobClient,
        token_id: str,
        leg_action: str,
        required_collateral: float,
        required_shares: float,
        exchange_address: str | None,
        collateral_status: AllowanceSnapshot | None = None,
    ) -> None:
        exchange = (exchange_address or "").lower()
        if leg_action.upper() == "BUY":
            collateral_status = collateral_status or read_clob_collateral_status(
                client,
                signature_type=self.settings.polymarket_signature_type,
            )
            if collateral_status.balance < required_collateral:
                raise LiveTradingError(
                    f"pUSD balance {collateral_status.balance:.4f} is below required collateral {required_collateral:.4f}."
                )
            if exchange and collateral_status.allowances and collateral_status.allowance_for(exchange) < required_collateral:
                raise LiveTradingError(
                    f"pUSD allowance for {exchange_address} is below required collateral {required_collateral:.4f}."
                )
            return

        conditional_status = read_clob_conditional_status(
            client,
            token_id=token_id,
            signature_type=self.settings.polymarket_signature_type,
        )
        if conditional_status.balance < required_shares:
            raise LiveTradingError(
                f"Outcome token balance {conditional_status.balance:.4f} is below sell size {required_shares:.4f}."
            )
        if exchange and conditional_status.allowances and conditional_status.allowance_for(exchange) <= 0:
            raise LiveTradingError(f"Outcome token allowance for {exchange_address} is not available.")

    @staticmethod
    def _extract_signed_amounts(order: object, side: object) -> tuple[float, float]:
        if hasattr(order, "dict"):
            payload = order.dict()
        elif isinstance(order, dict):
            payload = order
        else:
            payload = {
                "makerAmount": getattr(order, "makerAmount", 0),
                "takerAmount": getattr(order, "takerAmount", 0),
            }
        maker_amount = format_units(int(payload.get("makerAmount", 0)), TOKEN_DECIMALS)
        taker_amount = format_units(int(payload.get("takerAmount", 0)), TOKEN_DECIMALS)
        if side == Side.BUY or side == "BUY":
            return maker_amount, taker_amount
        return taker_amount, maker_amount

    @staticmethod
    def _book_field(order_book: object, field_name: str) -> Any:
        if isinstance(order_book, dict):
            return order_book.get(field_name)
        return getattr(order_book, field_name, None)

    def _resolve_tick_size(self, client: ClobClient, token_id: str, order_book: object) -> str:
        candidates: list[Decimal] = []
        book_tick = self._book_field(order_book, "tick_size")
        client_tick = None
        if hasattr(client, "get_tick_size"):
            try:
                client_tick = client.get_tick_size(token_id)
            except Exception:
                client_tick = None
        for value in (book_tick, client_tick):
            if value is None:
                continue
            try:
                tick = Decimal(str(value))
            except Exception:
                continue
            if tick > 0:
                candidates.append(tick)
        if not candidates:
            return "0.01"
        return str(max(candidates))

    def _resolve_neg_risk(self, client: ClobClient, token_id: str, order_book: object) -> bool:
        neg_risk = self._book_field(order_book, "neg_risk")
        if neg_risk is not None:
            return bool(neg_risk)
        return bool(client.get_neg_risk(token_id))

    def _uses_market_order(self, order_type: str) -> bool:
        return order_type.upper() in {"FAK", "FOK"}

    def _leg_order_type(self, leg: Any) -> str:
        return str(leg.order_type or self.settings.live_order_type).upper()

    def _order_type_enum(self, order_type: str) -> str:
        return getattr(OrderType, order_type.upper())

    @staticmethod
    def _expiration_timestamp(leg: Any) -> int:
        if str(leg.order_type or "").upper() != "GTD":
            return 0
        expiration_sec = int(leg.expiration_sec or 0)
        if expiration_sec <= 0:
            return 0
        safety_buffer = 60
        if isinstance(leg.metadata, dict):
            safety_buffer = int(leg.metadata.get("gtd_safety_buffer_sec", safety_buffer))
        return int(datetime.now(timezone.utc).timestamp()) + safety_buffer + expiration_sec

    def _result_order_type(self, plan: ExecutionPlan) -> str:
        order_types = {str(leg.order_type or "").upper() for leg in plan.legs if leg.order_type}
        if len(order_types) == 1:
            return next(iter(order_types))
        return "MIXED" if order_types else self.settings.live_order_type.upper()

    def _attempt_cleanup(self, client: ClobClient, leg_results: list[LiveExecutionLegResult]) -> str | None:
        order_ids = [leg.order_id for leg in leg_results if leg.order_id]
        if not order_ids:
            return None
        try:
            if hasattr(client, "cancel_orders"):
                client.cancel_orders(order_ids)
            elif hasattr(client, "cancel"):
                for order_id in order_ids:
                    client.cancel(order_id)
            else:
                raise AttributeError("CLOB client does not expose a cancellation method.")
        except Exception:
            for leg in leg_results:
                if leg.order_id in order_ids:
                    leg.response = {**leg.response, "cleanup": "cancel_failed"}
            return f"{len(order_ids)} submitted order(s) still require manual review."

        for leg in leg_results:
            if leg.order_id in order_ids:
                leg.status = "cancel_requested"
                leg.response = {**leg.response, "cleanup": "cancel_requested"}
        return f"cancel requested for {len(order_ids)} submitted order(s)."

    def _validate_plan(self, plan: ExecutionPlan) -> None:
        if not self.settings.enable_live_trading:
            raise LiveTradingError("ENABLE_LIVE_TRADING is false.")
        if not plan.live_trading_allowed:
            raise LiveTradingError("Execution plan is not marked as live-trading eligible.")
        if self.settings.live_order_type.upper() not in self.SUPPORTED_ORDER_TYPES:
            raise LiveTradingError(f"Unsupported LIVE_ORDER_TYPE: {self.settings.live_order_type}")
        if not plan.legs:
            raise LiveTradingError("Execution plan contains no tradable legs.")
        unsupported_actions = [leg.action for leg in plan.legs if leg.action.upper() not in {"BUY", "SELL"}]
        if unsupported_actions:
            raise LiveTradingError(f"Unsupported live-trading actions: {unsupported_actions}")
        unsupported_order_types = [
            self._leg_order_type(leg)
            for leg in plan.legs
            if self._leg_order_type(leg) not in self.SUPPORTED_ORDER_TYPES
        ]
        if unsupported_order_types:
            raise LiveTradingError(f"Unsupported leg order types: {unsupported_order_types}")
        if any(leg.post_only and self._leg_order_type(leg) not in {"GTC", "GTD"} for leg in plan.legs):
            raise LiveTradingError("Post-only orders must use GTC or GTD.")

    def _get_authenticated_client(self) -> ClobClient:
        private_key = (self.settings.polymarket_private_key or "").strip()
        if not private_key:
            raise LiveTradingError("POLYMARKET_PRIVATE_KEY is not configured.")
        normalized_private_key = normalize_private_key(private_key)
        funder = resolve_funder_address(self.settings, normalized_private_key)
        if self.settings.polymarket_signature_type != 0 and funder is None:
            raise LiveTradingError("POLYMARKET_FUNDER_ADDRESS is required for non-EOA signature types.")

        signature = (
            self.settings.clob_base_url,
            self.settings.polymarket_chain_id,
            self.settings.polymarket_signature_type,
            funder,
            normalized_private_key,
        )
        with self._client_lock:
            if self._client is not None and self._client_signature == signature:
                return self._client
            self._client = create_authenticated_clob_v2_client(self.settings)
            self._client_signature = signature
            return self._client
