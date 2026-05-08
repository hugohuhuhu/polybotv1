from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.models.core import ExecutionLeg, ExecutionPlan
from app.services.preflight import PreflightCheck, load_preflight_report
from app.strategy.polymarket_live_trading import (
    AllowanceSnapshot,
    PolymarketLiveTradingAdapter,
    normalize_limit_order_size,
    normalize_market_order_amount,
)


class _FakeBook:
    tick_size = "0.01"
    neg_risk = False


class _FakeClient:
    def __init__(self) -> None:
        self.limit_orders: list[object] = []
        self.market_orders: list[object] = []

    def get_order_book(self, _token_id: str) -> _FakeBook:
        return _FakeBook()

    def get_balance_allowance(self, _params=None) -> dict[str, object]:
        return {
            "balance": str(100 * 10**6),
            "allowances": {
                "0xe111180000d2663c0091e4f400237545b87b996b": str(100 * 10**6),
            },
        }

    def create_order(self, order_args, _options):
        self.limit_orders.append(order_args)
        shares = float(order_args.size)
        collateral = round(float(order_args.price) * shares, 6)
        if _is_buy(order_args.side):
            return SimpleNamespace(makerAmount=str(int(collateral * 10**6)), takerAmount=str(int(shares * 10**6)))
        return SimpleNamespace(makerAmount=str(int(shares * 10**6)), takerAmount=str(int(collateral * 10**6)))

    def create_market_order(self, order_args, _options):
        self.market_orders.append(order_args)
        amount = float(order_args.amount)
        if _is_buy(order_args.side):
            shares = round(amount / float(order_args.price), 6)
            return SimpleNamespace(makerAmount=str(int(amount * 10**6)), takerAmount=str(int(shares * 10**6)))
        collateral = round(amount * float(order_args.price), 6)
        return SimpleNamespace(makerAmount=str(int(amount * 10**6)), takerAmount=str(int(collateral * 10**6)))

    def post_order(self, _order, order_type: str, post_only: bool = False):
        return {"orderID": "order-1", "status": "matched", "orderType": order_type}

    def cancel_orders(self, _order_ids: list[str]) -> None:
        return None


def _is_buy(side: object) -> bool:
    return side == "BUY" or getattr(side, "name", None) == "BUY" or getattr(side, "value", None) == "BUY"


def _make_plan(action: str) -> ExecutionPlan:
    return ExecutionPlan(
        opportunity_id="opp-1",
        summary="test",
        legs=[
            ExecutionLeg(
                action=action,
                token_id="token-1",
                market_slug="market-a",
                outcome_label="Yes",
                target_price=0.42,
                size=5.0,
            )
        ],
        max_slippage_bps=10.0,
        cancel_conditions=[],
        live_trading_allowed=True,
    )


def test_normalize_v2_order_amounts_match_client_expectations() -> None:
    assert normalize_limit_order_size("BUY", 5.0) == pytest.approx(5.0)
    assert normalize_limit_order_size("SELL", 5.0) == pytest.approx(5.0)
    assert normalize_market_order_amount("BUY", 5.0, 0.42) == pytest.approx(2.10)
    assert normalize_market_order_amount("SELL", 5.0, 0.42) == pytest.approx(5.0)


def test_live_adapter_submits_fok_buy_as_market_pusd_amount(monkeypatch) -> None:
    fake_client = _FakeClient()
    adapter = PolymarketLiveTradingAdapter(
        Settings(
            ENABLE_LIVE_TRADING=True,
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            LIVE_ORDER_TYPE="FOK",
            LIVE_MAX_ORDER_SIZE=25.0,
        )
    )
    monkeypatch.setattr(adapter, "_get_authenticated_client", lambda: fake_client)
    monkeypatch.setattr(
        "app.strategy.polymarket_live_trading.read_clob_collateral_status",
        lambda *_args, **_kwargs: AllowanceSnapshot(
            balance=100.0,
            allowances={"0xe111180000d2663c0091e4f400237545b87b996b": 100.0},
            raw={},
        ),
    )

    result = adapter._execute_sync(_make_plan("BUY"))

    assert result.status == "submitted"
    assert len(fake_client.market_orders) == 1
    assert fake_client.market_orders[0].amount == pytest.approx(2.1)
    assert result.leg_results[0].requested_size == pytest.approx(5.0)
    assert result.leg_results[0].response["submitted_size"] == pytest.approx(2.1)
    assert result.leg_results[0].response["required_collateral"] == pytest.approx(2.1)
    assert result.leg_results[0].response["expected_shares"] == pytest.approx(5.0)
    assert result.leg_results[0].response["collateral_symbol"] == "pUSD"
    assert result.leg_results[0].response["submission_kind"] == "market"


def test_live_adapter_submits_gtc_buy_as_limit_shares(monkeypatch) -> None:
    fake_client = _FakeClient()
    adapter = PolymarketLiveTradingAdapter(
        Settings(
            ENABLE_LIVE_TRADING=True,
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            LIVE_ORDER_TYPE="GTC",
            LIVE_MAX_ORDER_SIZE=25.0,
        )
    )
    monkeypatch.setattr(adapter, "_get_authenticated_client", lambda: fake_client)
    monkeypatch.setattr(
        "app.strategy.polymarket_live_trading.read_clob_collateral_status",
        lambda *_args, **_kwargs: AllowanceSnapshot(
            balance=100.0,
            allowances={"0xe111180000d2663c0091e4f400237545b87b996b": 100.0},
            raw={},
        ),
    )

    result = adapter._execute_sync(_make_plan("BUY"))

    assert result.status == "submitted"
    assert len(fake_client.limit_orders) == 1
    assert fake_client.limit_orders[0].size == pytest.approx(5.0)
    assert result.leg_results[0].response["submitted_size"] == pytest.approx(5.0)
    assert result.leg_results[0].response["required_collateral"] == pytest.approx(2.1)
    assert result.leg_results[0].response["expected_shares"] == pytest.approx(5.0)
    assert result.leg_results[0].response["submission_kind"] == "limit"


def test_live_adapter_keeps_fok_sell_in_shares(monkeypatch) -> None:
    fake_client = _FakeClient()
    adapter = PolymarketLiveTradingAdapter(
        Settings(
            ENABLE_LIVE_TRADING=True,
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            LIVE_ORDER_TYPE="FOK",
            LIVE_MAX_ORDER_SIZE=25.0,
        )
    )
    monkeypatch.setattr(adapter, "_get_authenticated_client", lambda: fake_client)
    monkeypatch.setattr(
        "app.strategy.polymarket_live_trading.read_clob_conditional_status",
        lambda *_args, **_kwargs: AllowanceSnapshot(
            balance=100.0,
            allowances={"0xe111180000d2663c0091e4f400237545b87b996b": 100.0},
            raw={},
        ),
    )

    result = adapter._execute_sync(_make_plan("SELL"))

    assert result.status == "submitted"
    assert len(fake_client.market_orders) == 1
    assert fake_client.market_orders[0].amount == pytest.approx(5.0)
    assert result.leg_results[0].response["submitted_size"] == pytest.approx(5.0)
    assert result.leg_results[0].response["expected_shares"] == pytest.approx(5.0)


def test_preflight_reports_legacy_stack_before_cutover(monkeypatch) -> None:
    async def fake_fetch_chain_id(*_args, **_kwargs) -> int:
        return 137

    async def fake_fetch_native_balance(*_args, **_kwargs) -> int:
        return 10**18

    async def fake_fetch_erc20_balance(*_args, **_kwargs) -> int:
        return 25 * 10**6

    async def fake_fetch_erc20_allowance(*_args, **_kwargs) -> int:
        return 100 * 10**6

    async def fake_check_clock(*_args, **_kwargs) -> PreflightCheck:
        return PreflightCheck("clock_drift", "系統時間", "ok", "本機時間偏差 0 秒。", required=False)

    monkeypatch.setattr("app.services.preflight.fetch_chain_id", fake_fetch_chain_id)
    monkeypatch.setattr("app.services.preflight.fetch_native_balance", fake_fetch_native_balance)
    monkeypatch.setattr("app.services.preflight.fetch_erc20_balance", fake_fetch_erc20_balance)
    monkeypatch.setattr("app.services.preflight.fetch_erc20_allowance", fake_fetch_erc20_allowance)
    monkeypatch.setattr("app.services.preflight._check_clob_clock", fake_check_clock)
    report = asyncio.run(
        load_preflight_report(
            Settings(
                POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
                MIN_TRADING_COLLATERAL=1.0,
                MIN_EXCHANGE_ALLOWANCE=25.0,
            ),
            now=datetime(2026, 4, 24, 9, 0, tzinfo=timezone.utc),
        )
    )

    checks = {check.check_id: check for check in report.checks}
    assert report.collateral_symbol == "USDC.e"
    assert checks["live_stack"].status == "warning"
    assert checks["exchange_allowance"].status == "warning"
    assert report.ready is True


def test_preflight_accepts_v2_stack_after_cutover(monkeypatch) -> None:
    async def fake_fetch_chain_id(*_args, **_kwargs) -> int:
        return 137

    async def fake_fetch_native_balance(*_args, **_kwargs) -> int:
        return 10**18

    async def fake_fetch_erc20_balance(*_args, **_kwargs) -> int:
        return 25 * 10**6

    async def fake_fetch_erc20_allowance(*_args, **_kwargs) -> int:
        return 100 * 10**6

    async def fake_check_clock(*_args, **_kwargs) -> PreflightCheck:
        return PreflightCheck("clock_drift", "系統時間", "ok", "本機時間偏差 0 秒。", required=False)

    monkeypatch.setattr("app.services.preflight.fetch_chain_id", fake_fetch_chain_id)
    monkeypatch.setattr("app.services.preflight.fetch_native_balance", fake_fetch_native_balance)
    monkeypatch.setattr("app.services.preflight.fetch_erc20_balance", fake_fetch_erc20_balance)
    monkeypatch.setattr("app.services.preflight.fetch_erc20_allowance", fake_fetch_erc20_allowance)
    monkeypatch.setattr("app.services.preflight._check_clob_clock", fake_check_clock)
    monkeypatch.setattr("app.services.preflight._clob_v2_sdk_error", lambda: None)

    report = asyncio.run(
        load_preflight_report(
            Settings(
                POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
                MIN_TRADING_COLLATERAL=1.0,
                MIN_EXCHANGE_ALLOWANCE=25.0,
            ),
            now=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
        )
    )

    checks = {check.check_id: check for check in report.checks}
    assert report.collateral_symbol == "pUSD"
    assert checks["live_stack"].status == "ok"
    assert report.ready is True
