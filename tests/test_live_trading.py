from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from eth_account import Account

from app.config import Settings
from app.models.core import ExecutionLeg, ExecutionPlan
from app.strategy.polymarket_live_trading import LiveTradingError, PolymarketLiveTradingAdapter


def make_plan(*, live_trading_allowed: bool = True) -> ExecutionPlan:
    return ExecutionPlan(
        opportunity_id="opp-1",
        summary="Test plan",
        legs=[
            ExecutionLeg(
                action="BUY",
                token_id="yes-token",
                market_slug="test-market",
                outcome_label="Yes",
                target_price=0.49,
                size=50,
            ),
            ExecutionLeg(
                action="BUY",
                token_id="no-token",
                market_slug="test-market",
                outcome_label="No",
                target_price=0.50,
                size=50,
            ),
        ],
        max_slippage_bps=10,
        cancel_conditions=[],
        requires_manual_approval=True,
        live_trading_allowed=live_trading_allowed,
    )


def _allowance_payload(balance: float = 100.0) -> dict[str, object]:
    return {
        "balance": str(int(balance * 10**6)),
        "allowances": {
            "0xe111180000d2663c0091e4f400237545b87b996b": str(int(balance * 10**6)),
        },
    }


def test_live_trader_derives_eoa_funder_reuses_client_and_submits_orders(monkeypatch) -> None:
    private_key = "0x" + "1" * 64
    created_clients: list[FakeClobClient] = []

    class FakeClobClient:
        def __init__(self, host, chain_id=None, key=None, creds=None, signature_type=None, funder=None, **kwargs):
            self.host = host
            self.chain_id = chain_id
            self.key = key
            self.creds = creds
            self.signature_type = signature_type
            self.funder = funder
            self.kwargs = kwargs
            self.orders: list[dict] = []
            created_clients.append(self)

        def create_or_derive_api_key(self):
            return SimpleNamespace(api_key="api-key", api_secret="secret", api_passphrase="passphrase")

        def get_order_book(self, token_id):
            return SimpleNamespace(tick_size="0.01", neg_risk=False)

        def get_balance_allowance(self, _params=None):
            return _allowance_payload()

        def create_order(self, order_args, options=None):
            return SimpleNamespace(
                token_id=order_args.token_id,
                price=order_args.price,
                size=order_args.size,
                makerAmount=str(int(order_args.size * order_args.price * 10**6)),
                takerAmount=str(int(order_args.size * 10**6)),
            )

        def create_market_order(self, order_args, options=None):
            return SimpleNamespace(
                token_id=order_args.token_id,
                price=order_args.price,
                amount=order_args.amount,
                makerAmount=str(int(order_args.amount * 10**6)),
                takerAmount=str(int((order_args.amount / order_args.price) * 10**6)),
            )

        def post_order(self, order, order_type="FOK", post_only=False):
            self.orders.append({"order": order, "order_type": order_type, "post_only": post_only})
            return {"orderID": f"oid-{len(self.orders)}"}

    monkeypatch.setattr("app.strategy.polymarket_live_trading._V2_SDK_IMPORT_ERROR", None)
    monkeypatch.setattr("app.strategy.polymarket_live_trading.ClobClient", FakeClobClient)
    adapter = PolymarketLiveTradingAdapter(
        Settings(
            ENABLE_LIVE_TRADING=True,
            POLYMARKET_PRIVATE_KEY=private_key,
            POLYMARKET_SIGNATURE_TYPE=0,
            LIVE_ORDER_TYPE="FOK",
            LIVE_MAX_ORDER_SIZE=25,
        )
    )

    first = asyncio.run(adapter.execute(make_plan()))
    second = asyncio.run(adapter.execute(make_plan()))

    assert first.status == "submitted"
    assert second.status == "submitted"
    assert len(first.leg_results) == 2
    assert created_clients[0].creds is None
    assert created_clients[1].funder == Account.from_key(private_key).address
    assert len(created_clients) == 2
    assert all(leg.requested_size == 25 for leg in first.leg_results)
    assert created_clients[1].orders[0]["order"].amount == pytest.approx(12.25)
    assert created_clients[1].orders[1]["order"].amount == pytest.approx(12.5)
    assert first.leg_results[0].response["stack"] == "clob_v2_pusd"


def test_live_trader_requests_cleanup_on_partial_failure(monkeypatch) -> None:
    cancelled_orders: list[list[str]] = []

    class FakeClobClient:
        def __init__(self, host, chain_id=None, key=None, creds=None, signature_type=None, funder=None, **kwargs):
            self.orders: list[dict] = []

        def create_or_derive_api_key(self):
            return SimpleNamespace(api_key="api-key", api_secret="secret", api_passphrase="passphrase")

        def get_order_book(self, token_id):
            return SimpleNamespace(tick_size="0.01", neg_risk=False)

        def get_balance_allowance(self, _params=None):
            return _allowance_payload()

        def create_order(self, order_args, options=None):
            return SimpleNamespace(
                token_id=order_args.token_id,
                price=order_args.price,
                size=order_args.size,
                makerAmount=str(int(order_args.size * order_args.price * 10**6)),
                takerAmount=str(int(order_args.size * 10**6)),
            )

        def create_market_order(self, order_args, options=None):
            return SimpleNamespace(
                token_id=order_args.token_id,
                price=order_args.price,
                amount=order_args.amount,
                makerAmount=str(int(order_args.amount * 10**6)),
                takerAmount=str(int((order_args.amount / order_args.price) * 10**6)),
            )

        def post_order(self, order, order_type="FOK", post_only=False):
            self.orders.append(order)
            if len(self.orders) == 1:
                return {"orderID": "oid-1"}
            raise RuntimeError("second leg failed")

        def cancel_orders(self, order_ids):
            cancelled_orders.append(order_ids)
            return {"ok": True}

    monkeypatch.setattr("app.strategy.polymarket_live_trading._V2_SDK_IMPORT_ERROR", None)
    monkeypatch.setattr("app.strategy.polymarket_live_trading.ClobClient", FakeClobClient)
    adapter = PolymarketLiveTradingAdapter(
        Settings(
            ENABLE_LIVE_TRADING=True,
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            POLYMARKET_SIGNATURE_TYPE=0,
        )
    )

    result = asyncio.run(adapter.execute(make_plan()))

    assert result.status == "partial_failure"
    assert cancelled_orders == [["oid-1"]]
    assert result.leg_results[0].status == "cancel_requested"
    assert "Cleanup" in result.message


def test_live_trader_requires_enable_flag() -> None:
    adapter = PolymarketLiveTradingAdapter(Settings())
    with pytest.raises(LiveTradingError):
        asyncio.run(adapter.execute(make_plan()))


def test_live_trader_submits_near_close_gtd_post_only(monkeypatch) -> None:
    submitted: list[dict] = []

    class FakeClobClient:
        def __init__(self, host, chain_id=None, key=None, creds=None, signature_type=None, funder=None, **kwargs):
            pass

        def create_or_derive_api_key(self):
            return SimpleNamespace(api_key="api-key", api_secret="secret", api_passphrase="passphrase")

        def get_order_book(self, token_id):
            return SimpleNamespace(tick_size="0.001", neg_risk=False)

        def get_balance_allowance(self, _params=None):
            return _allowance_payload()

        def create_order(self, order_args, options=None):
            submitted.append({"order_args": order_args})
            return SimpleNamespace(
                token_id=order_args.token_id,
                price=order_args.price,
                size=order_args.size,
                expiration=order_args.expiration,
                makerAmount=str(int(order_args.size * order_args.price * 10**6)),
                takerAmount=str(int(order_args.size * 10**6)),
            )

        def post_order(self, order, order_type="GTC", post_only=False):
            submitted.append({"order": order, "order_type": order_type, "post_only": post_only})
            return {"orderID": "near-close-1"}

    monkeypatch.setattr("app.strategy.polymarket_live_trading._V2_SDK_IMPORT_ERROR", None)
    monkeypatch.setattr("app.strategy.polymarket_live_trading.ClobClient", FakeClobClient)
    plan = ExecutionPlan(
        opportunity_id="near-close",
        summary="near close",
        legs=[
            ExecutionLeg(
                action="BUY",
                token_id="yes-token",
                market_slug="market-a",
                outcome_label="Yes",
                target_price=0.97,
                size=1.0,
                order_type="GTD",
                post_only=True,
                expiration_sec=20,
                metadata={"strategy_variant": "near_close_maker", "gtd_safety_buffer_sec": 60},
            )
        ],
        max_slippage_bps=0,
        cancel_conditions=[],
        live_trading_allowed=True,
        strategy_type="late_resolution",
        metadata={"strategy_variant": "near_close_maker"},
    )
    adapter = PolymarketLiveTradingAdapter(
        Settings(
            ENABLE_LIVE_TRADING=True,
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            POLYMARKET_SIGNATURE_TYPE=0,
        )
    )

    result = asyncio.run(adapter.execute(plan))

    assert result.status == "submitted"
    assert submitted[0]["order_args"].expiration > 0
    assert submitted[1]["order_type"] == "GTD"
    assert submitted[1]["post_only"] is True
    assert result.leg_results[0].response["strategy_variant"] == "near_close_maker"
