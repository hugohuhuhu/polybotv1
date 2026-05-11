from __future__ import annotations

from datetime import datetime, timedelta, timezone
import time

from app.models.core import (
    BookLevel,
    LiveExecutionLegResult,
    LiveExecutionResult,
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
