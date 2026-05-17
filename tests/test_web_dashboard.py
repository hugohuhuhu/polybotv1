from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import Settings
from app.models.core import EventRecord, MarketRecord, Opportunity, OrderBookSnapshot, SignalDirection, StrategyType
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.web import create_app


class FakePreflightReport:
    def __init__(self, *, ready: bool = True, reasons: list[str] | None = None, address: str | None = None) -> None:
        self.ready = ready
        self.blocking_reasons = reasons or []
        self.address = address or "0x1111111111111111111111111111111111111111"

    def as_payload(self) -> dict:
        return {
            "ready": self.ready,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "address": self.address,
            "funder_address": self.address,
            "collateral_symbol": "USDC.e",
            "blocking_reasons": self.blocking_reasons,
            "checks": [],
        }


def seed_dashboard_data(sqlite_path: Path) -> None:
    connection = connect_db(sqlite_path)
    repository = ScannerRepository(connection)
    repository.save_markets(
        [EventRecord(event_id="e1", title="Event 1", active=True, closed=False)],
        [
            MarketRecord(
                market_id="m1",
                event_id="e1",
                question="Will something happen?",
                slug="will-something-happen",
                outcome_labels=["Yes", "No"],
                token_ids=["yes", "no"],
                liquidity=2500,
                active=True,
                closed=False,
                end_date=datetime.now(timezone.utc) + timedelta(hours=4),
            )
        ],
    )
    repository.save_opportunities(
        [
            Opportunity(
                opportunity_id="o1",
                strategy_type=StrategyType.LATE_RESOLUTION,
                direction=SignalDirection.BUY_BASKET,
                title="Near-close maker",
                summary="Near-close maker paper signal",
                market_slugs=["will-something-happen"],
                market_ids=["m1"],
                token_ids=["yes"],
                prices={"entry_bid": 0.97, "entry_ask": 0.986},
                gross_edge=0.03,
                estimated_fees=0.0,
                slippage_estimate=0.002,
                net_edge=0.028,
                max_safe_size=1.0,
                available_liquidity=80.0,
                confidence_score=0.88,
                suggested_action="Paper observe near-close bid",
                details={
                    "strategy_variant": "near_close_maker",
                    "summary": "Near-close maker paper signal",
                    "suggested_action": "Paper observe near-close bid",
                },
            )
        ]
    )
    repository.save_scan_cycle(
        executed_at=datetime.now(timezone.utc),
        discovered_market_count=120,
        monitored_market_count=25,
        book_count=50,
        opportunity_count=1,
        actionable_count=1,
        candidate_count=0,
    )
    now = datetime.now(timezone.utc)
    repository.save_watch_heartbeat(
        source="watch",
        state="delay",
        latest_scan_at=now,
        message="watch scan completed; delaying before next scan",
        details={
            "phase": "delay",
            "scan_started_at": (now - timedelta(seconds=5)).isoformat(),
            "delay_started_at": now.isoformat(),
            "delay_until": (now + timedelta(seconds=30)).isoformat(),
            "delay_sec": 30,
            "scan_timeout_sec": 60,
        },
    )
    repository.save_alert("o1", "console", "Binary underround alert")
    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id="o1",
        status="submitted",
        message="live ok",
        details={"legs": 2},
        claim_key="live:o1:test",
    )
    connection.close()


def seed_live_trade(sqlite_path: Path) -> None:
    connection = connect_db(sqlite_path)
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "live-cache-test",
                1,
                "BUY",
                "yes",
                "will-something-happen",
                "Yes",
                0.89,
                5.0,
                "0xlivecache",
                "settled_lost",
                "{}",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    connection.close()


def test_dashboard_routes_render_and_serve_data(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": False,
            "address": None,
            "status": "missing_private_key",
            "message": "尚未輸入私鑰",
            "balances": [],
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=False, reasons=["尚未輸入私鑰"], address=None)

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    sqlite_path = tmp_path / "dashboard.db"
    seed_dashboard_data(sqlite_path)
    app = create_app(
        Settings(
            SQLITE_PATH=str(sqlite_path),
            DASHBOARD_REFRESH_SEC=12,
            DASHBOARD_PAGE_SIZE=10,
            MAX_NOTIONAL_PER_PLAN=150.0,
            POLYMARKET_PRIVATE_KEY="",
        )
    )
    client = TestClient(app)

    html_response = client.get("/")
    assert html_response.status_code == 200
    assert "Polymarket" in html_response.text

    api_response = client.get("/api/dashboard")
    payload = api_response.json()
    assert api_response.status_code == 200
    assert payload["summary"]["open_markets"] == 1
    assert payload["summary"]["latest_monitored_markets"] == 25
    assert len(payload["opportunities"]) == 1
    assert payload["alerts"][0]["channel"] == "console"
    assert payload["execution_events"][0]["status"] == "submitted"
    assert payload["wallet"]["message"] == "尚未輸入私鑰"
    assert payload["trading"]["live_trading_enabled"] is False
    assert payload["preflight"]["ready"] is False
    assert payload["risk"]["max_notional_per_plan"] == 150.0
    assert payload["persistence"]["backend"] == "sqlite"
    assert payload["watch_heartbeats"][0]["state"] == "delay"
    assert payload["watch"]["phase"] == "delay"
    assert payload["watch"]["watch_scan_timeout_sec"] == 60.0
    assert payload["watch"]["watch_delay_sec"] == 30.0


def test_dashboard_timeout_reuses_last_successful_payload(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "ok",
            "balances": [],
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    sqlite_path = tmp_path / "dashboard-cache.db"
    seed_dashboard_data(sqlite_path)
    seed_live_trade(sqlite_path)
    app = create_app(Settings(SQLITE_PATH=str(sqlite_path), POLYMARKET_PRIVATE_KEY=""))
    client = TestClient(app)

    first_payload = client.get("/api/dashboard").json()
    assert first_payload["trade_journal"]["trade_count_total"] == 1
    assert len(first_payload["trade_groups"]) == 1

    def slow_refresh_open_position_orderbooks(_repo, _settings):
        time.sleep(0.1)
        return 0

    monkeypatch.setattr("app.web.DASHBOARD_DB_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr("app.web.refresh_open_position_orderbooks", slow_refresh_open_position_orderbooks)

    second_payload = client.get("/api/dashboard").json()

    assert second_payload["data_stale"] is True
    assert second_payload["trade_journal"]["trade_count_total"] == 1
    assert len(second_payload["trade_groups"]) == 1
    assert second_payload["trade_groups"][0]["market_slug"] == "will-something-happen"


def test_dashboard_throttles_open_position_orderbook_refresh(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "ok",
            "balances": [],
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    refresh_calls = 0

    def fake_refresh_open_position_orderbooks(_repo, _settings):
        nonlocal refresh_calls
        refresh_calls += 1
        return 0

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    monkeypatch.setattr("app.web.refresh_open_position_orderbooks", fake_refresh_open_position_orderbooks)
    sqlite_path = tmp_path / "dashboard-refresh-throttle.db"
    seed_dashboard_data(sqlite_path)
    app = create_app(Settings(SQLITE_PATH=str(sqlite_path), POLYMARKET_PRIVATE_KEY=""))
    client = TestClient(app)

    assert client.get("/api/dashboard").status_code == 200
    assert client.get("/api/dashboard").status_code == 200
    assert refresh_calls == 1


def test_dashboard_equity_uses_wallet_portfolio_value(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "ok",
            "balances": [
                {"symbol": "POL", "amount": 1.0, "status": "ok", "note": None},
                {"symbol": "USDC", "amount": 0.0, "status": "ok", "note": None},
                {"symbol": "pUSD", "amount": 15.920263, "status": "ok", "note": None},
            ],
            "portfolio": {
                "position_value": 24.995,
                "status": "ok",
                "source": "polymarket_data_api",
                "note": None,
                "positions": [],
            },
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    sqlite_path = tmp_path / "dashboard-equity.db"
    seed_dashboard_data(sqlite_path)
    app = create_app(
        Settings(
            SQLITE_PATH=str(sqlite_path),
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            PUSD_PNL_BASELINE=20.55,
        )
    )
    client = TestClient(app)

    payload = client.get("/api/dashboard").json()
    journal = payload["trade_journal"]

    assert journal["open_market_value"] == 24.995
    assert journal["open_market_value_source"] == "polymarket_data_api"
    assert round(journal["account_equity_estimate"], 6) == 40.915263
    assert round(journal["account_equity_delta"], 6) == 20.365263


def test_dashboard_trade_group_prefers_local_orderbook_over_portfolio_position_pnl(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "ok",
            "balances": [
                {"symbol": "pUSD", "amount": 15.0, "status": "ok", "note": None},
            ],
            "portfolio": {
                "position_value": 5.0,
                "status": "ok",
                "source": "polymarket_data_api",
                "note": None,
                "positions": [
                    {
                        "asset": "yes",
                        "currentValue": 5.0,
                        "cashPnl": 0.2,
                        "curPrice": 1.0,
                    }
                ],
            },
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    sqlite_path = tmp_path / "dashboard-position-pnl.db"
    seed_dashboard_data(sqlite_path)
    connection = connect_db(sqlite_path)
    repository = ScannerRepository(connection)
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "portfolio-pnl-test",
                1,
                "BUY",
                "yes",
                "will-something-happen",
                "Yes",
                0.96,
                5.0,
                "0xportfolio",
                "CONFIRMED",
                "{}",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    repository.save_orderbooks(
        [
            OrderBookSnapshot(
                token_id="yes",
                market_id="m1",
                bids=[{"price": 0.954, "size": 20.0}],
                asks=[{"price": 0.99, "size": 20.0}],
                updated_at=datetime.now(timezone.utc),
            )
        ]
    )
    connection.close()
    app = create_app(Settings(SQLITE_PATH=str(sqlite_path), POLYMARKET_PRIVATE_KEY="0x" + "1" * 64))
    client = TestClient(app)

    payload = client.get("/api/dashboard").json()
    group = payload["trade_groups"][0]

    assert group["current_value"] == 4.77
    assert round(group["total_pnl"], 2) == -0.03
    assert group["current_price"] == 0.954
    assert group["current_price_source"] == "best_bid"


def test_dashboard_includes_wallet_only_unredeemed_position(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "ok",
            "balances": [
                {"symbol": "pUSD", "amount": 31.5, "status": "ok", "note": None},
            ],
            "portfolio": {
                "position_value": 5.0,
                "status": "ok",
                "source": "polymarket_data_api",
                "note": None,
                "positions": [
                    {
                        "asset": "0xwalletonly",
                        "title": "Ethereum Up or Down - May 13, 8PM ET",
                        "outcome": "Up",
                        "size": 5.0,
                        "avgPrice": 0.97,
                        "currentValue": 5.0,
                        "cashPnl": 0.15,
                        "curPrice": 1.0,
                        "redeemable": True,
                    }
                ],
            },
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    sqlite_path = tmp_path / "dashboard-wallet-only.db"
    seed_dashboard_data(sqlite_path)
    app = create_app(Settings(SQLITE_PATH=str(sqlite_path), POLYMARKET_PRIVATE_KEY="0x" + "1" * 64))
    client = TestClient(app)

    payload = client.get("/api/dashboard").json()
    wallet_group = next(group for group in payload["trade_groups"] if group["token_id"] == "0xwalletonly")

    assert wallet_group["market_slug"] == "Ethereum Up or Down - May 13, 8PM ET"
    assert wallet_group["outcome_label"] == "Up"
    assert wallet_group["latest_status"] == "redeemable"
    assert wallet_group["open_size"] == 5.0
    assert wallet_group["current_value"] == 5.0
    assert wallet_group["current_price"] == 1.0
    assert wallet_group["total_pnl"] == 0.15


def test_dashboard_payload_degrades_when_preflight_and_wallet_timeout(tmp_path, monkeypatch) -> None:
    async def slow_wallet_status(_settings):
        await asyncio.sleep(1)
        return {"configured": True, "address": "0x1111111111111111111111111111111111111111", "balances": []}

    async def slow_preflight(_settings, *, verify_clob_credentials=True):
        await asyncio.sleep(1)
        return FakePreflightReport(ready=True)

    monkeypatch.setattr("app.web.DASHBOARD_COMPONENT_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr("app.web.load_wallet_status", slow_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", slow_preflight)
    sqlite_path = tmp_path / "dashboard-timeout.db"
    seed_dashboard_data(sqlite_path)
    app = create_app(Settings(SQLITE_PATH=str(sqlite_path), POLYMARKET_PRIVATE_KEY="0x" + "1" * 64))
    client = TestClient(app)

    response = client.get("/api/dashboard")
    payload = response.json()

    assert response.status_code == 200
    assert payload["summary"]["open_markets"] == 1
    assert payload["preflight"]["warning"] == "dashboard_preflight_timeout"
    assert payload["wallet"]["warning"] == "dashboard_wallet_timeout"
    assert payload["trading"]["auto_execute_enabled"] is False


def test_health_endpoint_reports_ok(tmp_path) -> None:
    sqlite_path = tmp_path / "health.db"
    app = create_app(Settings(SQLITE_PATH=str(sqlite_path)))
    client = TestClient(app)

    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["persistence_backend"] == "sqlite"


def test_trading_toggle_routes_persist_runtime_controls(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "已讀取錢包地址與代幣餘額",
            "balances": [
                {"symbol": "USDC", "amount": 125.0, "status": "ok", "note": None},
                {"symbol": "USDC.e", "amount": 80.0, "status": "ok", "note": None},
            ],
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    sqlite_path = tmp_path / "toggle.db"

    first_app = create_app(
        Settings(
            SQLITE_PATH=str(sqlite_path),
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            ENABLE_LIVE_TRADING=False,
            LIVE_AUTO_EXECUTE=False,
        )
    )
    first_client = TestClient(first_app)
    assert first_client.post("/api/actions/trading/live").status_code == 200
    auto_response = first_client.post("/api/actions/trading/auto")
    assert auto_response.status_code == 200
    assert auto_response.json()["payload"]["trading"]["armed"] is True

    second_app = create_app(
        Settings(
            SQLITE_PATH=str(sqlite_path),
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            ENABLE_LIVE_TRADING=False,
            LIVE_AUTO_EXECUTE=False,
        )
    )
    second_client = TestClient(second_app)
    payload = second_client.get("/api/dashboard").json()
    assert payload["trading"]["live_trading_enabled"] is True
    assert payload["trading"]["auto_execute_enabled"] is True
    assert payload["trading"]["armed"] is True


def test_kill_switch_route_disarms_runtime_controls(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "已讀取錢包地址與代幣餘額",
            "balances": [],
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    sqlite_path = tmp_path / "kill-switch.db"
    app = create_app(
        Settings(
            SQLITE_PATH=str(sqlite_path),
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            ENABLE_LIVE_TRADING=False,
            LIVE_AUTO_EXECUTE=False,
        )
    )
    client = TestClient(app)

    assert client.post("/api/actions/trading/live").status_code == 200
    assert client.post("/api/actions/trading/auto").status_code == 200

    kill_response = client.post("/api/actions/risk/kill-switch")
    assert kill_response.status_code == 200
    trading_payload = kill_response.json()["payload"]["trading"]
    assert trading_payload["kill_switch_enabled"] is True
    assert trading_payload["live_trading_enabled"] is False
    assert trading_payload["auto_execute_enabled"] is False

    blocked_live = client.post("/api/actions/trading/live")
    assert blocked_live.status_code == 409
    assert blocked_live.json()["detail"] == "kill_switch_enabled"


def test_finish_work_disarms_and_cancels_open_orders(tmp_path, monkeypatch) -> None:
    class FakeLiveTrader:
        def __init__(self) -> None:
            self.cancelled: list[str] = []

        async def get_open_orders(self) -> list[dict]:
            return [{"id": "0xremote"}]

        async def cancel_orders(self, order_ids: list[str]) -> dict:
            self.cancelled = order_ids
            return {"cancelled": len(order_ids)}

    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "ok",
            "balances": [],
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    stop_watch_called = False

    def fake_stop_watch_process() -> bool:
        nonlocal stop_watch_called
        stop_watch_called = True
        return True

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    monkeypatch.setattr("app.web._stop_watch_process", fake_stop_watch_process)
    sqlite_path = tmp_path / "finish-work.db"
    app = create_app(
        Settings(
            SQLITE_PATH=str(sqlite_path),
            SQLITE_BACKUP_DIR=str(tmp_path / "backups"),
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            ENABLE_LIVE_TRADING=True,
            LIVE_AUTO_EXECUTE=True,
        )
    )
    fake_trader = FakeLiveTrader()
    app.state.live_trader = fake_trader
    connection = connect_db(sqlite_path)
    with connection.transaction():
        connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "finish-test",
                1,
                "BUY",
                "token",
                "market",
                "Up",
                0.98,
                5.0,
                "0xlocal",
                "submitted",
                "{}",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    connection.close()
    client = TestClient(app)

    response = client.post("/api/actions/trading/finish")

    assert response.status_code == 200
    payload = response.json()["payload"]
    assert payload["trading"]["live_trading_enabled"] is False
    assert payload["trading"]["auto_execute_enabled"] is False
    assert stop_watch_called is True
    assert set(fake_trader.cancelled) == {"0xlocal", "0xremote"}
    connection = connect_db(sqlite_path)
    row = connection.fetchone("SELECT status FROM live_trades WHERE order_id = ?", ("0xlocal",))
    audit = connection.fetchone(
        """
        SELECT details_json
        FROM execution_audit_log
        WHERE status = ?
        ORDER BY id DESC
        """,
        ("finish_completed",),
    )
    connection.close()
    assert row["status"] == "cancelled"
    assert audit is not None
    assert "backup" not in json.loads(audit["details_json"])
    assert not any((tmp_path / "backups").glob("*.db"))


def test_scan_leaves_auto_redeem_to_background_loop(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "ok",
            "balances": [],
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=True)

    async def fake_scan_cycle(_settings, *, limit=None, repository=None, previous_midpoints=None):
        return SimpleNamespace(
            events=[],
            markets=[],
            books={},
            opportunities=[],
            executed_at=datetime.now(timezone.utc),
        )

    class FakeRedeemResult:
        status = "redeemed"
        market_slug = "solana-up-or-down"
        outcome_label = "Down"
        redeemed_size = 5.0
        message = "Redeemed and wrapped to pUSD."

    redeem_calls = 0

    def fake_redeem(_settings, _repo):
        nonlocal redeem_calls
        redeem_calls += 1
        return [FakeRedeemResult()]

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    monkeypatch.setattr("app.web.execute_scan_cycle", fake_scan_cycle)
    monkeypatch.setattr("app.web.persist_scan_cycle", lambda _repo, _result, _settings=None: None)
    monkeypatch.setattr("app.web.run_auto_redeem_once", fake_redeem)
    sqlite_path = tmp_path / "scan-redeem.db"
    app = create_app(Settings(SQLITE_PATH=str(sqlite_path), POLYMARKET_PRIVATE_KEY="0x" + "1" * 64))
    client = TestClient(app)

    response = client.post("/api/actions/scan")

    assert response.status_code == 200
    assert redeem_calls == 0
    assert response.json()["redeem_summary"]["status"] == "background_loop"


def test_auto_redeem_runs_once_on_dashboard_startup(tmp_path, monkeypatch) -> None:
    redeem_calls = 0

    def fake_redeem(_settings, _repo):
        nonlocal redeem_calls
        redeem_calls += 1
        return []

    monkeypatch.setattr("app.web.run_auto_redeem_once", fake_redeem)
    sqlite_path = tmp_path / "startup-redeem.db"
    app = create_app(
        Settings(
            SQLITE_PATH=str(sqlite_path),
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            AUTO_REDEEM_REFRESH_SEC=999,
        )
    )

    with TestClient(app):
        for _ in range(20):
            if redeem_calls:
                break
            time.sleep(0.05)

    assert redeem_calls == 1


def test_trading_toggle_blocks_when_preflight_fails(tmp_path, monkeypatch) -> None:
    async def fake_wallet_status(_settings):
        return {
            "configured": True,
            "address": "0x1111111111111111111111111111111111111111",
            "status": "ok",
            "message": "已讀取錢包地址與代幣餘額",
            "balances": [],
        }

    async def fake_preflight(_settings, *, verify_clob_credentials=True):
        return FakePreflightReport(ready=False, reasons=["POL 餘額不足"])

    monkeypatch.setattr("app.web.load_wallet_status", fake_wallet_status)
    monkeypatch.setattr("app.web.load_preflight_report", fake_preflight)
    sqlite_path = tmp_path / "preflight-block.db"
    app = create_app(
        Settings(
            SQLITE_PATH=str(sqlite_path),
            POLYMARKET_PRIVATE_KEY="0x" + "1" * 64,
            REQUIRE_LIVE_PREFLIGHT=True,
        )
    )
    client = TestClient(app)

    live_response = client.post("/api/actions/trading/live")

    assert live_response.status_code == 409
    assert live_response.json()["detail"]["code"] == "preflight_failed"
