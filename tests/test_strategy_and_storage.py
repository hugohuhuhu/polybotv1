from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time
from zoneinfo import ZoneInfo

from app.storage.backups import consolidate_sqlite_backups
from app.models.core import (
    BookLevel,
    EventRecord,
    LiveExecutionLegResult,
    LiveExecutionResult,
    MarketRecord,
    Opportunity,
    OrderBookSnapshot,
    PaperTradeResult,
    SignalDirection,
    StrategyType,
)
from app.models.runtime import TradingControls
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.strategy.execution_planner import ExecutionPlanner, PaperTradeSimulator
from app.strategy.opportunity_ranker import OpportunityRanker


def make_opportunity(opportunity_id: str, *, net_edge: float, liquidity: float) -> Opportunity:
    return Opportunity(
        opportunity_id=opportunity_id,
        strategy_type=StrategyType.BINARY_SUM,
        direction=SignalDirection.BUY_BASKET,
        title="Opportunity",
        summary="Summary",
        market_slugs=["market-a"],
        market_ids=["m1"],
        token_ids=["yes", "no"],
        prices={"yes_ask": 0.48, "no_ask": 0.49},
        gross_edge=net_edge + 0.002,
        estimated_fees=0.0,
        slippage_estimate=0.002,
        net_edge=net_edge,
        max_safe_size=liquidity,
        available_liquidity=liquidity,
        confidence_score=0.8,
        suggested_action="Buy basket",
        details={"locked_profit_per_share": net_edge},
    )


def make_books() -> dict[str, OrderBookSnapshot]:
    return {
        "yes": OrderBookSnapshot(
            token_id="yes",
            bids=[BookLevel(price=0.47, size=500)],
            asks=[BookLevel(price=0.48, size=500)],
            updated_at=datetime.now(timezone.utc),
        ),
        "no": OrderBookSnapshot(
            token_id="no",
            bids=[BookLevel(price=0.48, size=500)],
            asks=[BookLevel(price=0.49, size=500)],
            updated_at=datetime.now(timezone.utc),
        ),
    }


def test_ranker_puts_higher_score_first() -> None:
    ranker = OpportunityRanker()
    ranked = ranker.rank(
        [
            make_opportunity("low", net_edge=0.01, liquidity=100),
            make_opportunity("high", net_edge=0.03, liquidity=300),
        ]
    )
    assert ranked[0].opportunity_id == "high"


def test_orderbook_snapshots_dedupe_by_token_and_minute(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "dedupe.db"))
    captured_at = datetime(2026, 5, 10, 1, 2, 10, tzinfo=timezone.utc)
    first = OrderBookSnapshot(
        token_id="token-a",
        market_id="market-a",
        bids=[BookLevel(price=0.47, size=100)],
        asks=[BookLevel(price=0.49, size=100)],
        updated_at=captured_at,
    )
    second = first.model_copy(
        update={
            "bids": [BookLevel(price=0.48, size=100)],
            "asks": [BookLevel(price=0.50, size=100)],
            "updated_at": captured_at + timedelta(seconds=25),
        }
    )

    repository.save_orderbooks([first, second])

    row = repository.connection.fetchone(
        "SELECT COUNT(*) AS count, MAX(best_bid) AS best_bid FROM orderbook_snapshots WHERE token_id = ?",
        ("token-a",),
    )
    repository.connection.close()
    assert row["count"] == 1
    assert row["best_bid"] == 0.48


def test_database_maintenance_keeps_daily_summary_and_prunes_raw_rows(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "maintenance.db"))
    now = datetime.now(timezone.utc)
    old_raw = (now - timedelta(days=8)).isoformat()
    old_snapshot = (now - timedelta(days=31)).isoformat()
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO orderbook_snapshots (
                token_id, market_id, captured_minute, best_bid, best_ask, midpoint, spread,
                bids_json, asks_json, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("token-old", "market-old", old_raw[:16], 0.1, 0.2, 0.15, 0.1, "[]", "[]", old_raw),
        )
        repository.connection.execute(
            """
            INSERT INTO scan_cycles (
                executed_at, discovered_market_count, monitored_market_count, book_count,
                opportunity_count, actionable_count, candidate_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (old_snapshot, 12, 3, 2, 1, 1, 0),
        )

    result = repository.run_database_maintenance(force=True, force_vacuum=False)

    raw_count = repository.connection.fetchone("SELECT COUNT(*) AS count FROM orderbook_snapshots")
    scan_count = repository.connection.fetchone("SELECT COUNT(*) AS count FROM scan_cycles")
    summary_count = repository.connection.fetchone("SELECT COUNT(*) AS count FROM daily_summaries")
    repository.connection.close()
    assert result["status"] == "completed"
    assert raw_count["count"] == 0
    assert scan_count["count"] == 0
    assert summary_count["count"] == 1


def test_daily_rollover_maintains_previous_day_before_new_scan(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "rollover.db"))
    now = datetime.now(timezone.utc)
    old_raw = (now - timedelta(days=8)).isoformat()
    old_snapshot = (now - timedelta(days=31)).isoformat()
    yesterday = (now - timedelta(days=1)).date().isoformat()
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO maintenance_state (key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            ("active_scan_date", yesterday, old_raw),
        )
        repository.connection.execute(
            """
            INSERT INTO orderbook_snapshots (
                token_id, market_id, captured_minute, best_bid, best_ask, midpoint, spread,
                bids_json, asks_json, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("token-old", "market-old", old_raw[:16], 0.1, 0.2, 0.15, 0.1, "[]", "[]", old_raw),
        )
        repository.connection.execute(
            """
            INSERT INTO scan_cycles (
                executed_at, discovered_market_count, monitored_market_count, book_count,
                opportunity_count, actionable_count, candidate_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (old_snapshot, 12, 3, 2, 1, 1, 0),
        )

    result = repository.finalize_previous_scan_day_if_needed(
        executed_at=now,
        raw_retention_days=7,
        snapshot_retention_days=30,
    )

    raw_count = repository.connection.fetchone("SELECT COUNT(*) AS count FROM orderbook_snapshots")
    scan_count = repository.connection.fetchone("SELECT COUNT(*) AS count FROM scan_cycles")
    summary_count = repository.connection.fetchone("SELECT COUNT(*) AS count FROM daily_summaries")
    state = repository.connection.fetchone("SELECT value FROM maintenance_state WHERE key = ?", ("active_scan_date",))
    repository.connection.close()
    assert result["status"] == "rolled_over"
    assert raw_count["count"] == 0
    assert scan_count["count"] == 0
    assert summary_count["count"] == 1
    assert state["value"] == now.date().isoformat()


def test_consolidate_sqlite_backups_keeps_one_file_per_day(tmp_path) -> None:
    first = tmp_path / "polymarket_scanner.finish.20260510-150452.db"
    second = tmp_path / "polymarket_scanner.finish.20260510-171429.db"
    latest = tmp_path / "polymarket_scanner.latest.db"
    active = tmp_path / "polymarket_scanner.db"
    for path in (first, second, latest, active):
        path.write_text(path.name, encoding="utf-8")
    first.touch()
    second.touch()

    result = consolidate_sqlite_backups(tmp_path)

    assert result["status"] == "completed"
    assert (tmp_path / "polymarket_scanner.20260510.db").read_text(encoding="utf-8") == second.name
    assert not first.exists()
    assert not second.exists()
    assert not latest.exists()
    assert active.exists()


def test_execution_planner_and_paper_simulator_fill_buy_basket() -> None:
    opportunity = make_opportunity("fill-me", net_edge=0.03, liquidity=100)
    plan = ExecutionPlanner().build_plan(opportunity)
    result = PaperTradeSimulator(fees_bps=25).simulate(plan, make_books(), opportunity.net_edge)
    assert result.filled is True
    assert result.expected_pnl is not None
    assert result.gross_notional > 0
    assert result.estimated_fees_paid > 0


def test_execution_planner_handles_late_resolution_single_leg() -> None:
    opportunity = Opportunity(
        opportunity_id="late-fill",
        strategy_type=StrategyType.LATE_RESOLUTION,
        direction=SignalDirection.BUY_BASKET,
        title="Late resolution",
        summary="Late resolution summary",
        market_slugs=["market-a"],
        market_ids=["m1"],
        token_ids=["yes"],
        prices={"entry_bid": 0.97, "entry_ask": 0.986, "current_bid": 0.969, "target_exit_price": 1.0},
        gross_edge=0.03,
        estimated_fees=0.0,
        slippage_estimate=0.001,
        net_edge=0.018,
        max_safe_size=50,
        available_liquidity=50,
        confidence_score=0.7,
        suggested_action="Buy then rest a maker exit",
        details={
            "strategy_variant": "near_close_maker",
            "tradable_live": False,
            "requires_exit_order": False,
            "post_only": True,
            "order_type": "GTD",
            "expiration_sec": 1800,
        },
    )
    plan = ExecutionPlanner().build_plan(opportunity)
    assert len(plan.legs) == 1
    assert plan.legs[0].action == "BUY"
    assert plan.legs[0].target_price == 0.97
    assert plan.legs[0].post_only is True
    assert plan.legs[0].order_type == "GTD"
    assert plan.legs[0].expiration_sec == 1800
    assert plan.live_trading_allowed is False


def test_repository_runtime_controls_claims_and_reporting(tmp_path) -> None:
    connection = connect_db(tmp_path / "scanner.db")
    repository = ScannerRepository(connection)
    opportunity = make_opportunity("persist-me", net_edge=0.02, liquidity=120)
    opportunity.details.update(
        {
            "qualification_tier": "actionable",
            "qualification_label": "可直接警示",
            "alert_eligible": True,
            "ranking_score": 9.9,
        }
    )
    repository.save_opportunities([opportunity])
    repository.save_scan_cycle(
        executed_at=datetime.now(timezone.utc),
        discovered_market_count=125,
        monitored_market_count=40,
        book_count=80,
        opportunity_count=5,
        actionable_count=2,
        candidate_count=3,
        watch_bucket_counts={"general": 20, "event_cluster": 10},
        shortlist_reason_counts={"tight_spread": 12, "event_cluster": 10},
        shortlisted_markets=[
            {
                "question": "Market A",
                "slug": "market-a",
                "liquidity": 5000,
                "watch_score": 0.91,
                "bucket": "general",
                "family_key": "event-a",
                "reasons": ["tight_spread", "recent_activity"],
                "discovered_at": datetime.now(timezone.utc).isoformat(),
            }
        ],
        excluded_long_tail_count=7,
        excluded_family_cap_count=3,
        positive_edge_candidates_24h=4,
        near_close_funnel=[
            {
                "label": "探索到的市場",
                "count": 125,
                "description": "Gamma discovery 本輪回傳的 open market universe。",
            },
            {
                "label": "進入監看 shortlist",
                "count": 40,
                "description": "依策略排序後進入低頻監看池。",
            },
        ],
    )
    repository.save_alert(opportunity.opportunity_id, "console", "message")
    repository.save_paper_trade(
        PaperTradeResult(
            opportunity_id=opportunity.opportunity_id,
            filled=True,
            average_entry_price=0.485,
            filled_size=100.0,
            gross_notional=97.0,
            estimated_fees_paid=0.24,
            expected_pnl=2.0,
            notes="ok",
        )
    )

    defaults = TradingControls(False, False, False)
    assert repository.get_trading_controls(defaults) == defaults

    updated_controls = repository.save_trading_controls(TradingControls(True, False, False))
    assert updated_controls.live_trading_enabled is True
    assert repository.get_trading_controls(defaults).live_trading_enabled is True

    assert repository.claim_execution(
        claim_key="live:persist-me:test",
        opportunity_id=opportunity.opportunity_id,
        source="watch",
        mode="live",
        message="claim",
    ) is True
    assert repository.claim_execution(
        claim_key="live:persist-me:test",
        opportunity_id=opportunity.opportunity_id,
        source="dashboard",
        mode="live",
        message="duplicate",
    ) is False

    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id=opportunity.opportunity_id,
        status="submitted",
        message="ok",
        details={"legs": 2},
        claim_key="live:persist-me:test",
    )
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="live-buy",
            status="submitted",
            message="ok",
            order_type="FOK",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="market-a",
                    outcome_label="Yes",
                    target_price=0.48,
                    requested_size=10.0,
                    order_id="buy-1",
                    status="submitted",
                )
            ],
        )
    )
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="live-sell",
            status="submitted",
            message="ok",
            order_type="FOK",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="SELL",
                    token_id="yes",
                    market_slug="market-a",
                    outcome_label="Yes",
                    target_price=0.51,
                    requested_size=10.0,
                    order_id="sell-1",
                    status="submitted",
                )
            ],
        )
    )

    assert repository.was_alerted_recently(opportunity.opportunity_id, 3600) is True
    assert repository.top_opportunities_today(limit=5)
    assert repository.strategy_hit_rate()
    assert repository.average_realized_pnl() == 2.0
    assert repository.recent_execution_events(limit=5)[0]["status"] == "submitted"
    assert repository.recent_live_positions(limit=5) == []
    assert repository.save_clob_fills(
        [
            {
                "id": "fill-buy-1",
                "taker_order_id": "buy-fill-1",
                "asset_id": "yes",
                "side": "BUY",
                "size": "10",
                "price": "0.48",
                "status": "CONFIRMED",
                "outcome": "Yes",
                "match_time": str(int(datetime.now(timezone.utc).timestamp())),
            },
            {
                "id": "fill-sell-1",
                "taker_order_id": "sell-fill-1",
                "asset_id": "yes",
                "side": "SELL",
                "size": "10",
                "price": "0.51",
                "status": "CONFIRMED",
                "outcome": "Yes",
                "match_time": str(int(datetime.now(timezone.utc).timestamp())),
            },
        ]
    ) == 2
    assert repository.save_clob_fills(
        [
            {
                "id": "fill-maker-1",
                "taker_order_id": "counterparty-taker-1",
                "asset_id": "other-token",
                "side": "SELL",
                "size": "5",
                "price": "0.09",
                "status": "CONFIRMED",
                "outcome": "Up",
                "maker_orders": [
                    {
                        "order_id": "our-maker-1",
                        "maker_address": "0xabc",
                        "asset_id": "yes",
                        "side": "BUY",
                        "matched_amount": "5",
                        "price": "0.91",
                        "outcome": "Yes",
                    }
                ],
                "match_time": str(int(datetime.now(timezone.utc).timestamp())),
            }
        ],
        wallet_address="0xABC",
    ) == 1
    assert len(repository.recent_live_positions(limit=5)) == 3

    dashboard = repository.dashboard_summary()
    assert dashboard["latest_monitored_markets"] == 40
    assert dashboard["latest_candidate_count"] == 3
    assert dashboard["near_close_funnel"][0]["label"] == "探索到的市場"
    assert dashboard["near_close_funnel"][1]["count"] == 40
    assert dashboard["paper_notional_today"] == 97.0
    assert dashboard["watch_bucket_counts"]["general"] == 20
    assert dashboard["excluded_long_tail_count"] == 7
    assert dashboard["positive_edge_candidates_24h"] == 4

    top_markets = repository.top_markets(limit=5)
    assert top_markets[0]["bucket"] == "general"
    assert top_markets[0]["slug"] == "market-a"

    trade_journal = repository.live_trade_journal_summary()
    assert round(float(trade_journal["estimated_realized_pnl_total"]), 2) == 0.30
    assert trade_journal["trade_count_total"] == 3
    assert round(float(trade_journal["open_size_total"]), 2) == 5.0

    created_at = datetime(2026, 5, 11, 22, 22, 35, tzinfo=timezone.utc)
    repository.connection.execute(
        """
        INSERT INTO live_trades (
            opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
            target_price, requested_size, order_id, status, response_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "local-opportunity",
            1,
            "BUY",
            "activity-token",
            "doge-updown",
            "Down",
            0.94,
            5.0,
            "0xlocalorder",
            "cancel_unconfirmed",
            "{}",
            created_at.isoformat(),
        ),
    )
    assert repository.save_polymarket_activity_trades(
        [
            {
                "proxyWallet": "0xabc",
                "timestamp": int((created_at + timedelta(seconds=18)).timestamp()),
                "type": "TRADE",
                "size": 5,
                "usdcSize": 4.7,
                "transactionHash": "0xtx",
                "price": 0.94,
                "asset": "activity-token",
                "side": "BUY",
                "slug": "doge-updown",
                "outcome": "Down",
            }
        ],
        wallet_address="0xABC",
    ) == 1
    row = repository.connection.fetchone("SELECT status, response_json FROM live_trades WHERE order_id = ?", ("0xlocalorder",))
    assert row["status"] == "CONFIRMED"
    assert "0xtx" in row["response_json"]
    activity_order = next(order for order in repository.recent_live_orders(limit=10) if order["order_id"] == "0xlocalorder")
    assert activity_order["transaction_hash"] == "0xtx"

    latest = repository.latest_opportunities(limit=5)
    assert latest[0]["qualification_tier"] == "actionable"
    assert latest[0]["title"].startswith("[可直接警示]")

    near_close = opportunity.model_copy(
        update={
            "opportunity_id": "near-close",
            "strategy_type": StrategyType.LATE_RESOLUTION,
            "details": {"strategy_variant": "near_close_maker"},
        }
    )
    repository.save_opportunities([near_close])
    near_latest = repository.latest_opportunities(limit=5, strategy_variant="near_close_maker")
    near_summary = repository.strategy_summary(strategy_variant="near_close_maker")
    assert [item["opportunity_id"] for item in near_latest] == ["near-close"]
    assert near_summary[0]["strategy_type"] == "late_resolution"


def test_near_close_live_exposure_ignores_expired_gtd_orders(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "expired-near-close.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="expired-near-close",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="expired-market",
                    outcome_label="Yes",
                    target_price=0.97,
                    requested_size=5.0,
                    order_id="expired-1",
                    status="submitted",
                    response={
                        "strategy_variant": "near_close_maker",
                        "expiration": int(time.time()) - 60,
                    },
                )
            ],
        )
    )

    exposure = repository.near_close_live_exposure()

    assert exposure["total"] == 0.0
    assert exposure["active_orders"] == 0


def test_near_close_active_orders_for_market_returns_unexpired_orders(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "active-near-close.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="active-near-close",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="btc-updown",
                    outcome_label="Yes",
                    target_price=0.987,
                    requested_size=5.0,
                    order_id="active-1",
                    status="submitted",
                    response={
                        "strategy_variant": "near_close_maker",
                        "expiration": int(time.time()) + 600,
                    },
                )
            ],
        )
    )

    active = repository.near_close_active_orders_for_market(market_slug="btc-updown", token_id="yes")

    assert len(active) == 1
    assert active[0]["order_id"] == "active-1"
    assert active[0]["target_price"] == 0.987


def test_expire_open_orders_for_ended_markets_marks_stale_orders_cancelled(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-market-orders.db"))
    repository.save_markets(
        [EventRecord(event_id="event-ended", title="Ended", active=False, closed=True)],
        [
            MarketRecord(
                market_id="market-ended",
                event_id="event-ended",
                question="Ended market?",
                slug="ended-updown",
                outcome_labels=["Up", "Down"],
                token_ids=["ended-up", "ended-down"],
                active=False,
                closed=True,
                end_date=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        ],
    )
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="ended-live",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="ended-up",
                    market_slug="ended-updown",
                    outcome_label="Up",
                    target_price=0.97,
                    requested_size=5.0,
                    order_id="ended-open-1",
                    status="submitted",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )

    assert repository.expire_open_orders_for_ended_markets() == 1
    orders = repository.recent_live_orders(limit=5)

    assert orders[0]["raw_status"] == "expired"
    assert orders[0]["status"] == "cancelled"


def test_expire_open_orders_for_ended_timestamp_slug_without_market_row(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-slug-orders.db"))
    ended_slug = f"doge-updown-5m-{int(time.time()) - 60}"
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="ended-slug-live",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="ended-up",
                    market_slug=ended_slug,
                    outcome_label="Up",
                    target_price=0.97,
                    requested_size=5.0,
                    order_id="ended-slug-open-1",
                    status="submitted",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )

    assert repository.expire_open_orders_for_ended_markets() == 1
    assert repository.recent_live_orders(limit=5)[0]["status"] == "cancelled"


def test_expire_open_orders_for_ended_et_slug_without_market_row(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-et-slug-orders.db"))
    ended_at = datetime.now(ZoneInfo("America/New_York")) - timedelta(hours=1)
    hour_12 = ended_at.hour % 12 or 12
    meridiem = "pm" if ended_at.hour >= 12 else "am"
    ended_slug = f"bitcoin-up-or-down-{ended_at.strftime('%b').lower()}-{ended_at.day}-{ended_at.year}-{hour_12}{meridiem}-et"
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="ended-et-slug-live",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="ended-up",
                    market_slug=ended_slug,
                    outcome_label="Up",
                    target_price=0.97,
                    requested_size=5.0,
                    order_id="ended-et-slug-open-1",
                    status="submitted",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )

    assert repository.expire_open_orders_for_ended_markets() == 1
    assert repository.recent_live_orders(limit=5)[0]["status"] == "cancelled"


def test_recent_live_orders_use_settlement_value_after_market_end(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-confirmed-order.db"))
    token_id = "btc-up"
    slug = f"btc-updown-15m-{int(time.time()) - 60}"
    repository.save_markets(
        [EventRecord(event_id="event-ended", title="Ended", active=True, closed=False)],
        [
            MarketRecord(
                market_id="market-ended",
                event_id="event-ended",
                question="BTC ended?",
                slug=slug,
                outcome_labels=["Up", "Down"],
                token_ids=[token_id, "btc-down"],
                active=True,
                closed=False,
                end_date=datetime.now(timezone.utc) - timedelta(minutes=1),
                raw={"near_close_crypto_winning_outcome": "Up"},
            )
        ],
    )
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="ended-confirmed",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id=token_id,
                    market_slug=slug,
                    outcome_label="Up",
                    target_price=0.97,
                    requested_size=5.0,
                    order_id="confirmed-ended-1",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )
    repository.save_orderbooks(
        [
            OrderBookSnapshot(
                token_id=token_id,
                bids=[BookLevel(price=0.76, size=100)],
                asks=[BookLevel(price=0.85, size=100)],
                updated_at=datetime.now(timezone.utc),
            )
        ]
    )

    order = repository.recent_live_orders(limit=5)[0]

    assert order["status"] == "settlement_pending"
    assert order["market_ended"] is True
    assert order["current_price"] == 1.0
    assert order["current_price_source"] == "settlement_outcome"
    assert order["current_value"] == 5.0
    assert round(order["pnl"], 2) == 0.15

    group = repository.live_trade_groups(limit=5)[0]
    assert group["latest_status"] == "settlement_pending"
    assert group["market_ended"] is True
    assert group["current_price"] == 1.0
    assert group["current_price_source"] == "settlement_outcome"
    assert group["current_value"] == 5.0
    assert round(group["total_pnl"], 2) == 0.15


def test_recent_live_orders_use_response_winner_and_market_link_after_market_end(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-response-winner-order.db"))
    token_id = "eth-up"
    slug = f"eth-updown-15m-{int(time.time()) - 60}"
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="ended-response-winner",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id=token_id,
                    market_slug=slug,
                    outcome_label="Up",
                    target_price=0.97,
                    requested_size=5.0,
                    order_id="confirmed-ended-response-1",
                    status="CONFIRMED",
                    response={
                        "strategy_variant": "near_close_maker",
                        "crypto_winning_outcome": "Up",
                    },
                )
            ],
        )
    )

    order = repository.recent_live_orders(limit=5)[0]

    assert order["status"] == "settlement_pending"
    assert order["market_url"] == f"https://polymarket.com/market/{slug}"
    assert order["current_price"] == 1.0
    assert order["current_value"] == 5.0
    assert round(order["pnl"], 2) == 0.15


def test_recent_live_orders_fall_back_to_book_value_for_unresolved_matched_order(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-unresolved-order.db"))
    token_id = "sol-up"
    slug = f"sol-updown-15m-{int(time.time()) - 60}"
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="ended-unresolved",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id=token_id,
                    market_slug=slug,
                    outcome_label="Up",
                    target_price=0.95,
                    requested_size=2.0,
                    order_id="confirmed-unresolved-1",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )
    repository.save_orderbooks(
        [
            OrderBookSnapshot(
                token_id=token_id,
                bids=[BookLevel(price=0.82, size=100)],
                asks=[BookLevel(price=0.9, size=100)],
                updated_at=datetime.now(timezone.utc),
            )
        ]
    )

    order = repository.recent_live_orders(limit=5)[0]

    assert order["status"] == "settlement_pending"
    assert order["current_price"] == 0.82
    assert order["current_value"] == 1.64
    assert round(order["pnl"], 2) == -0.26


def test_near_close_live_exposure_counts_open_confirmed_positions(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "open-near-close.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="open-near-close",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="eth-updown",
                    outcome_label="Yes",
                    target_price=0.97,
                    requested_size=5.0,
                    order_id="confirmed-1",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )

    exposure = repository.near_close_live_exposure()

    assert exposure["total"] == 4.85
    assert exposure["by_market"]["eth-updown"] == 4.85
    assert exposure["by_position"]["eth-updown:yes:Yes"]["open_size"] == 5.0


def test_near_close_live_exposure_ignores_strategy_cancelled_orders(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "cancelled-near-close.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="cancelled-near-close",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="yes",
                    market_slug="eth-updown",
                    outcome_label="Yes",
                    target_price=0.988,
                    requested_size=5.0,
                    order_id="reprice-1",
                    status="submitted",
                    response={
                        "strategy_variant": "near_close_maker",
                        "expiration": int(time.time()) + 600,
                    },
                ),
                LiveExecutionLegResult(
                    leg_index=2,
                    action="BUY",
                    token_id="no",
                    market_slug="sol-updown",
                    outcome_label="No",
                    target_price=0.982,
                    requested_size=5.0,
                    order_id="qualification-1",
                    status="submitted",
                    response={
                        "strategy_variant": "near_close_maker",
                        "expiration": int(time.time()) + 600,
                    },
                ),
            ],
        )
    )

    repository.mark_live_orders_cancelled(["reprice-1"], status="reprice_cancelled")
    repository.mark_live_orders_cancelled(["qualification-1"], status="qualification_cancelled")

    assert repository.near_close_active_orders_for_market() == []
    exposure = repository.near_close_live_exposure()
    assert exposure["total"] == 0.0
    assert exposure["active_orders"] == 0
