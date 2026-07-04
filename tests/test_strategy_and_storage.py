from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
import time
from zoneinfo import ZoneInfo

from app.storage.backups import consolidate_sqlite_backups
from app.models.core import (
    BookLevel,
    EventRecord,
    ExecutionLeg,
    ExecutionPlan,
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


def test_pending_submission_reconciles_large_fill_price_move(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "pending-reconciliation.db"))
    plan = ExecutionPlan(
        opportunity_id="pending-opportunity",
        summary="near-close taker",
        legs=[
            ExecutionLeg(
                action="BUY",
                token_id="token-down",
                market_slug="btc-updown-5m-123",
                outcome_label="Down",
                target_price=0.90,
                size=5.0,
                order_type="FAK",
                metadata={
                    "strategy_variant": "near_close_maker",
                    "entry_execution_mode": "taker_fallback",
                    "trade_autopsy_id": "ta_pending_reconciliation",
                },
            )
        ],
        max_slippage_bps=10.0,
        cancel_conditions=[],
        live_trading_allowed=True,
    )
    repository.save_live_submission_pending(plan, claim_key="claim-1", source="watch")
    before = repository.connection.fetchone(
        "SELECT id, order_id, status FROM live_trades WHERE opportunity_id = ?",
        (plan.opportunity_id,),
    )

    activities = [
        {
            "type": "TRADE",
            "proxyWallet": "0xabc",
            "asset": "token-down",
            "side": "BUY",
            "transactionHash": "0xreconciled",
            "price": 0.1342,
            "size": 33.52941,
            "timestamp": int(datetime.now(timezone.utc).timestamp()),
            "slug": "btc-updown-5m-123",
            "outcome": "Down",
        }
    ]
    inserted = repository.save_polymarket_activity_trades(
        activities,
        wallet_address="0xabc",
    )
    after = repository.connection.fetchone(
        "SELECT id, opportunity_id, requested_size, status, response_json FROM live_trades WHERE id = ?",
        (before["id"],),
    )

    assert before["order_id"] is None
    assert before["status"] == "submission_pending"
    assert inserted == 1
    assert after["id"] == before["id"]
    assert after["opportunity_id"] == plan.opportunity_id
    assert after["requested_size"] == 33.52941
    assert after["status"] == "CONFIRMED"
    assert json.loads(after["response_json"])["actual_fill_price"] == 0.1342
    assert repository.save_polymarket_activity_trades(activities, wallet_address="0xabc") == 0
    fill_events = repository.connection.fetchone(
        "SELECT COUNT(*) AS count FROM execution_audit_log WHERE status = 'trade_autopsy_fill'"
    )
    assert fill_events["count"] == 1


def test_activity_redeem_zero_sets_all_in_settled_loss(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "activity-settlement.db"))
    wallet = "0xabc"
    slug = "btc-updown-5m-1782963900"
    token_id = "token-down"
    activities = [
        {
            "type": "REDEEM",
            "proxyWallet": wallet,
            "slug": slug,
            "transactionHash": "0xredeem-zero",
            "timestamp": 1782964724,
            "size": 0,
            "usdcSize": 0,
        },
        {
            "type": "TRADE",
            "proxyWallet": wallet,
            "asset": token_id,
            "side": "BUY",
            "transactionHash": "0xbuy-loss",
            "price": 0.1342105036,
            "size": 33.52941,
            "usdcSize": 4.772719,
            "timestamp": 1782964183,
            "slug": slug,
            "outcome": "Down",
        },
    ]

    assert repository.save_polymarket_activity_trades(activities, wallet_address=wallet) == 2
    row = repository.connection.fetchone(
        "SELECT status, response_json FROM live_trades WHERE market_slug = ?",
        (slug,),
    )
    order = next(item for item in repository.recent_live_orders(limit=20) if item["market_slug"] == slug)

    assert row["status"] == "SETTLED_LOST"
    assert json.loads(row["response_json"])["settlement_source"] == "polymarket_activity"
    assert order["status"] == "finished"
    assert order["notional"] == 4.772719
    assert order["current_value"] == 0.0
    assert order["pnl"] == -4.772719
    assert repository.save_polymarket_activity_trades(activities, wallet_address=wallet) == 0


def test_expire_stale_pending_submission_after_market_end(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "stale-pending.db"))
    plan = ExecutionPlan(
        opportunity_id="stale-pending",
        summary="stale",
        legs=[
            ExecutionLeg(
                action="BUY",
                token_id="token-down",
                market_slug="btc-updown-5m-1782969300",
                outcome_label="Down",
                target_price=0.90,
                size=5.0,
                order_type="GTD",
            )
        ],
        max_slippage_bps=10.0,
        cancel_conditions=[],
        live_trading_allowed=True,
    )
    assert repository.claim_execution(
        claim_key="stale-claim",
        opportunity_id=plan.opportunity_id,
        source="watch",
        mode="live",
    )
    repository.save_live_submission_pending(plan, claim_key="stale-claim", source="watch")
    repository.connection.execute(
        "UPDATE live_trades SET created_at = ? WHERE opportunity_id = ?",
        ("2026-07-02T05:19:26+00:00", plan.opportunity_id),
    )

    assert repository.expire_stale_live_submissions(older_than_sec=300.0) == 1
    row = repository.connection.fetchone(
        "SELECT status, response_json FROM live_trades WHERE opportunity_id = ?",
        (plan.opportunity_id,),
    )
    assert row["status"] == "reconciliation_failed"
    assert json.loads(row["response_json"])["submission_state"] == "reconciliation_failed"
    claim = repository.connection.fetchone(
        "SELECT status FROM execution_claims WHERE opportunity_id = ?",
        (plan.opportunity_id,),
    )
    assert claim["status"] == "reconciliation_failed"
    assert repository.near_close_active_orders_for_market() == []

    repository.connection.execute(
        "UPDATE execution_claims SET status = 'claimed' WHERE opportunity_id = ?",
        (plan.opportunity_id,),
    )
    assert repository.expire_stale_live_submissions(older_than_sec=300.0) == 0
    repaired_claim = repository.connection.fetchone(
        "SELECT status FROM execution_claims WHERE opportunity_id = ?",
        (plan.opportunity_id,),
    )
    assert repaired_claim["status"] == "reconciliation_failed"


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


def test_near_close_resolution_bucket_uses_utc_five_minute_cutoffs() -> None:
    assert ScannerRepository.near_close_resolution_bucket_key("btc-updown-5m-1780202700") == "1780203000"
    assert ScannerRepository.near_close_resolution_bucket_key("sol-updown-5m-1780202671") == "1780203000"
    assert ScannerRepository.near_close_resolution_bucket_key("eth-updown-5m-1780202412") == "1780203000"


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


def test_execution_planner_handles_near_close_taker_fallback_leg() -> None:
    opportunity = Opportunity(
        opportunity_id="late-fak",
        strategy_type=StrategyType.LATE_RESOLUTION,
        direction=SignalDirection.BUY_BASKET,
        title="Late resolution taker",
        summary="Late resolution taker summary",
        market_slugs=["market-a"],
        market_ids=["m1"],
        token_ids=["yes"],
        prices={"entry_bid": 0.90, "entry_ask": 0.90, "current_bid": 0.89, "target_exit_price": 1.0},
        gross_edge=0.10,
        estimated_fees=0.0,
        slippage_estimate=0.001,
        net_edge=0.094,
        max_safe_size=5,
        available_liquidity=50,
        confidence_score=0.7,
        suggested_action="Buy through strict FAK fallback",
        details={
            "strategy_variant": "near_close_maker",
            "tradable_live": True,
            "requires_exit_order": False,
            "entry_execution_mode": "taker_fallback",
            "post_only": False,
            "order_type": "FAK",
            "expiration_sec": None,
        },
    )

    plan = ExecutionPlanner().build_plan(opportunity)

    assert len(plan.legs) == 1
    assert plan.legs[0].target_price == 0.90
    assert plan.legs[0].post_only is False
    assert plan.legs[0].order_type == "FAK"
    assert plan.live_trading_allowed is True
    assert plan.legs[0].metadata["entry_execution_mode"] == "taker_fallback"


def test_recent_live_orders_classifies_maker_and_taker_execution_roles(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "execution-role.db"))
    created_at = datetime.now(timezone.utc)
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="maker-entry",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=created_at,
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="maker-token",
                    market_slug="btc-updown-5m-test",
                    outcome_label="Down",
                    target_price=0.90,
                    requested_size=5.0,
                    order_id="0xmaker",
                    status="submitted",
                    response={"order_type": "GTD", "post_only": True, "submission_kind": "limit"},
                )
            ],
        )
    )
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="taker-entry",
            status="submitted",
            message="ok",
            order_type="FAK",
            created_at=created_at + timedelta(seconds=1),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="taker-token",
                    market_slug="eth-updown-5m-test",
                    outcome_label="Up",
                    target_price=0.90,
                    requested_size=5.0,
                    order_id="0xtaker",
                    status="submitted",
                    response={
                        "order_type": "FAK",
                        "post_only": False,
                        "submission_kind": "market",
                        "entry_execution_mode": "taker_fallback",
                    },
                )
            ],
        )
    )

    orders = repository.recent_live_orders(limit=5)
    by_order_id = {order["order_id"]: order for order in orders}

    assert by_order_id["0xmaker"]["execution_role"] == "maker"
    assert by_order_id["0xmaker"]["order_type"] == "GTD"
    assert by_order_id["0xmaker"]["post_only"] is True
    assert by_order_id["0xtaker"]["execution_role"] == "taker"
    assert by_order_id["0xtaker"]["order_type"] == "FAK"
    assert by_order_id["0xtaker"]["post_only"] is False


def test_clob_partial_maker_fills_accumulate_without_double_counting(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "partial-maker-fills.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="partial-maker-entry",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="partial-token",
                    market_slug="sol-updown-5m-partial",
                    outcome_label="Up",
                    target_price=0.9,
                    requested_size=5.0,
                    order_id="0xpartialmaker",
                    status="submitted",
                )
            ],
        )
    )
    fills = [
        {
            "id": "partial-fill-1",
            "transactionHash": "0xpartialtx1",
            "taker_order_id": "0xtaker1",
            "asset_id": "counterparty-token-1",
            "side": "SELL",
            "size": "10",
            "price": "0.1",
            "status": "CONFIRMED",
            "maker_orders": [
                {
                    "order_id": "0xpartialmaker",
                    "maker_address": "0xabc",
                    "asset_id": "partial-token",
                    "side": "BUY",
                    "matched_amount": "2",
                    "price": "0.9",
                    "outcome": "Up",
                }
            ],
        },
        {
            "id": "partial-fill-2",
            "transactionHash": "0xpartialtx2",
            "taker_order_id": "0xtaker2",
            "asset_id": "counterparty-token-2",
            "side": "SELL",
            "size": "9.6",
            "price": "0.13",
            "status": "CONFIRMED",
            "maker_orders": [
                {
                    "order_id": "0xpartialmaker",
                    "maker_address": "0xabc",
                    "asset_id": "partial-token",
                    "side": "BUY",
                    "matched_amount": "3",
                    "price": "0.9",
                    "outcome": "Up",
                }
            ],
        },
    ]

    assert repository.save_clob_fills(fills, wallet_address="0xABC") == 2
    assert repository.save_clob_fills(fills, wallet_address="0xABC") == 2

    row = repository.connection.fetchone(
        "SELECT requested_size, response_json FROM live_trades WHERE order_id = ?",
        ("0xpartialmaker",),
    )
    response = json.loads(row["response_json"])
    order = next(item for item in repository.recent_live_orders(limit=5) if item["order_id"] == "0xpartialmaker")

    assert float(row["requested_size"]) == 5.0
    assert float(response["actual_matched_size"]) == 5.0
    assert float(response["actual_fill_notional"]) == 4.5
    assert float(response["actual_fill_price"]) == 0.9
    assert len(response["matched_fills"]) == 2
    assert order["requested_size"] == 5.0
    assert order["notional"] == 4.5

    assert repository.save_polymarket_activity_trades(
        [
            {
                "type": "TRADE",
                "proxyWallet": "0xabc",
                "asset": "partial-token",
                "side": "BUY",
                "transactionHash": "0xpartialtx2",
                "price": 0.9,
                "size": 3.0,
                "usdcSize": 2.7,
                "timestamp": int(datetime.now(timezone.utc).timestamp()),
                "slug": "sol-updown-5m-partial",
                "outcome": "Up",
            }
        ],
        wallet_address="0xABC",
    ) == 1
    activity_order = next(
        item for item in repository.recent_live_orders(limit=5) if item["order_id"] == "0xpartialmaker"
    )
    assert activity_order["notional"] == 4.5


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
        scan_rejection_counts={"bid_depth_below_min": 9, "spread_above_max": 4},
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
    assert dashboard["scan_rejection_counts"]["bid_depth_below_min"] == 9
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

    duplicate_created_at = datetime(2026, 5, 24, 8, 29, 31, tzinfo=timezone.utc)
    assert repository.save_polymarket_activity_trades(
        [
            {
                "proxyWallet": "0xabc",
                "timestamp": int(duplicate_created_at.timestamp()),
                "type": "TRADE",
                "size": 3.285453,
                "usdcSize": 2.562654,
                "transactionHash": "0xduplicatetx",
                "price": 0.78000020088554,
                "asset": "duplicate-token",
                "side": "BUY",
                "slug": "sol-updown-5m-1779611100",
                "outcome": "Up",
            }
        ],
        wallet_address="0xABC",
    ) == 1
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="local-maker-after-activity",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=duplicate_created_at - timedelta(seconds=2),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="duplicate-token",
                    market_slug="sol-updown-5m-1779611100",
                    outcome_label="Up",
                    target_price=0.78000020088554,
                    requested_size=3.285453,
                    order_id="0xlocalmaker",
                    status="submitted",
                )
            ],
        )
    )
    assert repository.save_clob_fills(
        [
            {
                "id": "fill-duplicate",
                "transactionHash": "0xduplicatetx",
                "taker_order_id": "0xcounterparty",
                "asset_id": "counterparty-token",
                "side": "SELL",
                "size": "3.285453",
                "price": "0.21999979911446",
                "status": "CONFIRMED",
                "outcome": "Down",
                "maker_orders": [
                    {
                        "order_id": "0xlocalmaker",
                        "maker_address": "0xabc",
                        "asset_id": "duplicate-token",
                        "side": "BUY",
                        "matched_amount": "3.285453",
                        "price": "0.78000020088554",
                        "outcome": "Up",
                    }
                ],
                "match_time": str(int(duplicate_created_at.timestamp())),
            }
        ],
        wallet_address="0xABC",
    ) == 1
    duplicate_rows = repository.connection.fetchall(
        """
        SELECT order_id, status
        FROM live_trades
        WHERE response_json LIKE ?
        ORDER BY order_id
        """,
        ("%0xduplicatetx%",),
    )
    assert {row["order_id"]: row["status"] for row in duplicate_rows} == {
        "0xlocalmaker": "CONFIRMED",
        "data-api:0xduplicatetx:duplicate-token:BUY": "MISATTRIBUTED_FILL_IGNORED",
    }
    local_maker_row = repository.connection.fetchone(
        "SELECT response_json FROM live_trades WHERE order_id = ?",
        ("0xlocalmaker",),
    )
    local_maker_response = json.loads(local_maker_row["response_json"])
    assert local_maker_response["actual_fill_source"] == "user_fill"
    assert round(float(local_maker_response["actual_fill_price"]), 6) == 0.780000
    assert round(float(local_maker_response["actual_matched_size"]), 6) == 3.285453
    assert round(float(local_maker_response["clob_fill"]["price"]), 6) == 0.220000
    duplicate_group = next(
        group for group in repository.live_trade_groups(limit=10) if group["market_slug"] == "sol-updown-5m-1779611100"
    )
    assert round(float(duplicate_group["entry_notional"]), 6) == 2.562654

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


def test_submitted_stop_exit_matched_response_counts_in_live_pnl(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "matched-stop-exit.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "entry",
                1,
                "BUY",
                "token-btc",
                "btc-updown-15m-test",
                "Up",
                0.91,
                5.0,
                "0xentry",
                "CONFIRMED",
                json.dumps({"strategy_variant": "near_close_maker"}),
                "2026-05-28T00:24:44+00:00",
            ),
        )
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stop-exit:btc-updown-15m-test:token-btc",
                1,
                "SELL",
                "token-btc",
                "btc-updown-15m-test",
                "Up",
                0.69,
                5.0,
                "0xstop",
                "submitted",
                json.dumps(
                    {
                        "strategy_variant": "near_close_stop_exit",
                        "status": "matched",
                        "success": True,
                        "takingAmount": "3.6",
                        "makingAmount": "5",
                        "transactionsHashes": ["0xtx"],
                    }
                ),
                "2026-05-28T00:24:51+00:00",
            ),
        )

    stop_order = next(order for order in repository.recent_live_orders(limit=5) if order["order_id"] == "0xstop")
    assert stop_order["raw_status"] == "submitted"
    assert stop_order["status"] == "matched"
    assert round(float(stop_order["notional"]), 2) == 3.60

    group = repository.live_trade_groups(limit=5)[0]
    assert group["latest_status"] == "MATCHED"
    assert group["open_size"] == 0
    assert round(float(group["estimated_realized_pnl"]), 2) == -0.95

    journal = repository.live_trade_journal_summary()
    assert round(float(journal["estimated_realized_pnl_total"]), 2) == -0.95
    assert journal["open_size_total"] == 0

    repository.connection.execute("UPDATE live_trades SET status = 'REDEEMED' WHERE order_id = ?", ("0xentry",))
    redeemed_group = repository.live_trade_groups(limit=5)[0]
    assert redeemed_group["open_size"] == 0
    assert round(float(redeemed_group["estimated_realized_pnl"]), 2) == -0.95


def test_near_close_entry_bucket_report_groups_live_trade_performance(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "entry-bucket-report.db"))

    def insert_live_trade(
        *,
        action: str,
        token_id: str,
        market_slug: str,
        price: float,
        size: float,
        status: str,
        response: dict[str, object],
        created_at: str,
    ) -> None:
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"{market_slug}-{action}-{created_at}",
                1,
                action,
                token_id,
                market_slug,
                "Up",
                price,
                size,
                f"0x{market_slug}{action}",
                status,
                json.dumps(response),
                created_at,
            ),
        )

    base_entry = {
        "strategy_variant": "near_close_maker",
        "best_bid": 0.89,
        "best_ask": 0.91,
        "spread": 0.02,
        "midpoint": 0.90,
        "bid_depth_at_best": 25,
        "ask_depth_at_best": 30,
        "crypto_start_distance": 0.003,
    }
    insert_live_trade(
        action="BUY",
        token_id="token-120",
        market_slug="bucket-120",
        price=0.90,
        size=10,
        status="CONFIRMED",
        response={**base_entry, "time_to_resolution_sec": 110, "entry_price": 0.90},
        created_at="2026-05-30T00:00:01+00:00",
    )
    insert_live_trade(
        action="SELL",
        token_id="token-120",
        market_slug="bucket-120",
        price=0.95,
        size=10,
        status="submitted",
        response={
            "strategy_variant": "near_close_stop_exit",
            "status": "matched",
            "success": True,
            "transactionsHashes": ["0xtx"],
        },
        created_at="2026-05-30T00:00:02+00:00",
    )
    insert_live_trade(
        action="BUY",
        token_id="token-90",
        market_slug="bucket-90",
        price=0.92,
        size=5,
        status="REDEEMED",
        response={**base_entry, "time_to_resolution_sec": 75, "entry_price": 0.92, "spread": 0.03},
        created_at="2026-05-30T00:00:03+00:00",
    )
    insert_live_trade(
        action="BUY",
        token_id="token-60",
        market_slug="bucket-60",
        price=0.88,
        size=5,
        status="SETTLED_LOST",
        response={**base_entry, "time_to_resolution_sec": 45, "entry_price": 0.88, "crypto_start_distance": 0.004},
        created_at="2026-05-30T00:00:04+00:00",
    )
    insert_live_trade(
        action="BUY",
        token_id="token-30",
        market_slug="bucket-30",
        price=0.91,
        size=5,
        status="CONFIRMED",
        response={**base_entry, "time_to_resolution_sec": 15, "entry_price": 0.91},
        created_at="2026-05-30T00:00:05+00:00",
    )

    report = {row["bucket"]: row for row in repository.near_close_entry_bucket_report()}

    assert report["120-90"]["trade_count"] == 1
    assert report["120-90"]["realized_trade_count"] == 1
    assert report["120-90"]["win_rate"] == 1.0
    assert round(float(report["120-90"]["average_realized_pnl"]), 2) == 0.50
    assert report["90-60"]["trade_count"] == 1
    assert round(float(report["90-60"]["average_realized_pnl"]), 2) == 0.40
    assert report["60-30"]["trade_count"] == 1
    assert round(float(report["60-30"]["max_loss"]), 2) == -4.40
    assert report["30-0"]["trade_count"] == 1
    assert report["30-0"]["realized_trade_count"] == 0
    assert report["30-0"]["average_realized_pnl"] is None
    assert round(float(report["90-60"]["average_spread"]), 2) == 0.03
    assert round(float(report["60-30"]["average_crypto_start_distance"]), 3) == 0.004


def test_near_close_signal_replay_report_groups_settled_opportunities(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "signal-replay-report.db"))
    now = datetime.now(timezone.utc)
    repository.save_markets(
        [
            EventRecord(event_id="replay-event-btc", title="BTC replay"),
            EventRecord(event_id="replay-event-eth", title="ETH replay"),
            EventRecord(event_id="replay-event-sol", title="SOL replay"),
        ],
        [
            MarketRecord(
                market_id="replay-btc",
                event_id="replay-event-btc",
                question="BTC Up/Down",
                slug="btc-updown-5m-1780202700",
                outcome_labels=["Up", "Down"],
                token_ids=["btc-up", "btc-down"],
                active=False,
                closed=True,
                end_date=now - timedelta(minutes=10),
                raw={"outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]'},
            ),
            MarketRecord(
                market_id="replay-eth",
                event_id="replay-event-eth",
                question="ETH Up/Down",
                slug="eth-updown-5m-1780202700",
                outcome_labels=["Up", "Down"],
                token_ids=["eth-up", "eth-down"],
                active=False,
                closed=True,
                end_date=now - timedelta(minutes=10),
                raw={"outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]'},
            ),
            MarketRecord(
                market_id="replay-sol",
                event_id="replay-event-sol",
                question="SOL Up/Down",
                slug="sol-updown-5m-1780203000",
                outcome_labels=["Up", "Down"],
                token_ids=["sol-up", "sol-down"],
                active=True,
                closed=False,
                end_date=now + timedelta(minutes=5),
                raw={"outcomes": '["Up", "Down"]', "outcomePrices": '["0.52", "0.48"]'},
            ),
        ],
    )

    def make_near_close_signal(
        opportunity_id: str,
        *,
        slug: str,
        token_id: str,
        outcome_label: str,
        seconds_left: float,
        entry_price: float,
        spread: float,
        start_distance: float,
        depth: float,
        timestamp: datetime,
    ) -> Opportunity:
        return Opportunity(
            opportunity_id=opportunity_id,
            strategy_type=StrategyType.LATE_RESOLUTION,
            direction=SignalDirection.REVIEW,
            title=slug,
            summary=slug,
            market_slugs=[slug],
            market_ids=[slug],
            token_ids=[token_id],
            prices={"entry_price": entry_price},
            gross_edge=1.0 - entry_price,
            estimated_fees=0.0,
            slippage_estimate=0.0,
            net_edge=1.0 - entry_price,
            max_safe_size=depth,
            available_liquidity=depth,
            confidence_score=0.8,
            suggested_action="Buy near close",
            timestamp=timestamp,
            details={
                "strategy_variant": "near_close_maker",
                "near_close_variant": "crypto_updown",
                "market_slug": slug,
                "token_id": token_id,
                "outcome_label": outcome_label,
                "time_to_resolution_sec": seconds_left,
                "entry_price": entry_price,
                "spread": spread,
                "crypto_start_distance": start_distance,
                "bid_depth_at_best": depth,
            },
        )

    repository.save_opportunities(
        [
            make_near_close_signal(
                "replay-btc-win",
                slug="btc-updown-5m-1780202700",
                token_id="btc-down",
                outcome_label="Down",
                seconds_left=110,
                entry_price=0.90,
                spread=0.01,
                start_distance=0.0006,
                depth=80,
                timestamp=now - timedelta(minutes=20),
            ),
            make_near_close_signal(
                "replay-eth-loss",
                slug="eth-updown-5m-1780202700",
                token_id="eth-down",
                outcome_label="Down",
                seconds_left=70,
                entry_price=0.80,
                spread=0.03,
                start_distance=0.0012,
                depth=40,
                timestamp=now - timedelta(minutes=19),
            ),
            make_near_close_signal(
                "replay-eth-loss-duplicate",
                slug="eth-updown-5m-1780202700",
                token_id="eth-down",
                outcome_label="Down",
                seconds_left=75,
                entry_price=0.81,
                spread=0.03,
                start_distance=0.0012,
                depth=40,
                timestamp=now - timedelta(minutes=18),
            ),
            make_near_close_signal(
                "replay-sol-unresolved",
                slug="sol-updown-5m-1780203000",
                token_id="sol-up",
                outcome_label="Up",
                seconds_left=45,
                entry_price=0.86,
                spread=0.02,
                start_distance=0.0004,
                depth=20,
                timestamp=now - timedelta(minutes=1),
            ),
        ]
    )

    deduped = repository.near_close_signal_replay_report()
    overall = next(row for row in deduped if row["group_type"] == "overall")
    time_rows = {row["group"]: row for row in deduped if row["group_type"] == "time_bucket"}
    asset_rows = {row["group"]: row for row in deduped if row["group_type"] == "asset"}

    assert overall["sample_count"] == 3
    assert overall["resolved_count"] == 2
    assert overall["unresolved_count"] == 1
    assert overall["win_rate"] == 0.5
    assert round(float(overall["average_ev_per_share"]), 2) == -0.35
    assert time_rows["120-90"]["win_rate"] == 1.0
    assert round(float(time_rows["90-60"]["average_ev_per_share"]), 2) == -0.80
    assert asset_rows["BTC"]["win_rate"] == 1.0
    assert asset_rows["ETH"]["win_rate"] == 0.0

    all_signals = repository.near_close_signal_replay_report(dedupe=False)
    all_overall = next(row for row in all_signals if row["group_type"] == "overall")
    assert all_overall["sample_count"] == 4


def test_expire_open_orders_for_ended_timestamp_slug_without_market_row(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-slug-orders.db"))
    ended_slug = f"doge-updown-5m-{int(time.time()) - 360}"
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
    slug = f"btc-updown-15m-{int(time.time()) - 960}"
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


def test_recent_live_orders_use_outcome_prices_after_market_end(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-outcome-prices-order.db"))
    token_id = "sol-up"
    slug = f"sol-updown-15m-{int(time.time()) - 960}"
    repository.save_markets(
        [EventRecord(event_id="event-ended-prices", title="Ended", active=True, closed=True)],
        [
            MarketRecord(
                market_id="market-ended-prices",
                event_id="event-ended-prices",
                question="SOL ended?",
                slug=slug,
                outcome_labels=["Up", "Down"],
                token_ids=[token_id, "sol-down"],
                active=True,
                closed=True,
                end_date=datetime.now(timezone.utc) - timedelta(minutes=1),
                raw={"outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]'},
            )
        ],
    )
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="ended-outcome-prices",
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
                    target_price=0.91,
                    requested_size=5.0,
                    order_id="confirmed-ended-prices-1",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker"},
                )
            ],
        )
    )

    order = repository.recent_live_orders(limit=5)[0]

    assert order["status"] == "settlement_pending"
    assert order["market_ended"] is True
    assert order["current_price"] == 0.0
    assert order["current_price_source"] == "settlement_outcome"
    assert order["current_value"] == 0.0
    assert round(order["pnl"], 2) == -4.55


def test_live_trade_groups_offset_settled_lost_entry_with_stop_exit_sell(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "settled-lost-stop-offset.db"))
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lost-entry",
                1,
                "BUY",
                "sol-up",
                "sol-updown-15m-test",
                "Up",
                0.92,
                5.0,
                "0xbuy",
                "SETTLED_LOST",
                "{}",
                "2026-05-21T21:11:13+00:00",
            ),
        )
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stop-exit-second-chance:sol-updown-15m-test:sol-up",
                1,
                "SELL",
                "sol-up",
                "sol-updown-15m-test",
                "Up",
                0.68,
                5.0,
                "0xsell",
                "CONFIRMED",
                json.dumps({"strategy_variant": "near_close_stop_exit"}),
                "2026-05-21T21:14:40+00:00",
            ),
        )

    group = repository.live_trade_groups(limit=5)[0]

    assert group["buy_size"] == 5.0
    assert group["sell_size"] == 5.0
    assert group["open_size"] == 0.0
    assert round(group["entry_notional"], 2) == 4.6
    assert round(group["exit_notional"], 2) == 3.4
    assert round(group["estimated_realized_pnl"], 2) == -1.2
    assert round(group["total_pnl"], 2) == -1.2
    assert group["position_status"] == "closed"

    journal = repository.live_trade_journal_summary()
    assert round(float(journal["estimated_realized_pnl_total"]), 2) == -1.2
    assert round(float(journal["open_size_total"]), 2) == 0.0


def test_near_close_performance_report_splits_modes_buckets_and_risk_recovery(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "near-close-performance.db"))
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    rows = [
        (
            "maker-win",
            "BUY",
            "btc-up",
            "btc-updown-5m-maker-win",
            "Up",
            0.90,
            5.0,
            "0xmakerwin",
            "REDEEMED",
            {"strategy_variant": "near_close_maker", "order_type": "GTD", "post_only": True},
            created_at,
        ),
        (
            "taker-stop",
            "BUY",
            "eth-down",
            "eth-updown-5m-taker-stop",
            "Down",
            0.87,
            5.0,
            "0xtakerstopentry",
            "SETTLED_LOST",
            {
                "strategy_variant": "near_close_maker",
                "entry_execution_mode": "taker_fallback",
                "order_type": "FAK",
                "post_only": False,
            },
            created_at + timedelta(minutes=5),
        ),
        (
            "stop-exit:eth-updown-5m-taker-stop:eth-down",
            "SELL",
            "eth-down",
            "eth-updown-5m-taker-stop",
            "Down",
            0.25,
            5.0,
            "0xtakerstopexit",
            "MATCHED",
            {"strategy_variant": "near_close_stop_exit", "order_type": "FAK", "post_only": False},
            created_at + timedelta(minutes=5, seconds=20),
        ),
        (
            "taker-zero",
            "BUY",
            "sol-up",
            "sol-updown-5m-taker-zero",
            "Up",
            0.86,
            5.0,
            "0xtakerzero",
            "SETTLED_LOST",
            {
                "strategy_variant": "near_close_maker",
                "fee_rate_bps": "1000",
            },
            created_at + timedelta(minutes=10),
        ),
    ]
    with repository.connection.transaction():
        for index, row in enumerate(rows, start=1):
            repository.connection.execute(
                """
                INSERT INTO live_trades (
                    opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                    target_price, requested_size, order_id, status, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row[0],
                    index,
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    row[5],
                    row[6],
                    row[7],
                    row[8],
                    json.dumps(row[9]),
                    row[10].isoformat(),
                ),
            )
    report = repository.near_close_performance_report(taker_fee_rate=0.07)
    summary = report["periods"]["all"]["summary"]
    maker = report["periods"]["all"]["modes"]["maker"]
    taker = report["periods"]["all"]["modes"]["taker"]
    buckets = {bucket["key"]: bucket for bucket in report["periods"]["all"]["price_buckets"]}

    assert summary["count"] == 3
    assert summary["zero_loss_count"] == 1
    assert round(float(summary["zero_loss_rate"]), 6) == round(1 / 3, 6)
    assert summary["risk_exit_count"] == 1
    assert round(float(summary["risk_recovered"]), 2) == 1.25
    assert summary["ci95_low"] is not None
    assert summary["ci95_high"] is not None
    assert maker["count"] == 1
    assert round(float(maker["net_pnl"]), 2) == 0.50
    assert taker["count"] == 2
    assert taker["estimated_fees"] > 0
    assert buckets["0.86-0.87"]["count"] == 2
    assert buckets["0.89-0.90"]["count"] == 1
    assert report["periods"]["today"]["summary"]["count"] == 3


def test_recent_live_orders_do_not_use_entry_prediction_as_settlement_winner(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-response-winner-order.db"))
    token_id = "eth-up"
    slug = f"eth-updown-15m-{int(time.time()) - 960}"
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
    assert order["current_price"] is None
    assert order["current_value"] is None
    assert order["pnl"] is None


def test_recent_live_orders_fall_back_to_book_value_for_unresolved_matched_order(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-unresolved-order.db"))
    token_id = "sol-up"
    slug = f"sol-updown-15m-{int(time.time()) - 960}"
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


def test_near_close_live_exposure_ignores_positions_from_ended_markets(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "ended-near-close.db"))
    now_ts = int(datetime.now(timezone.utc).timestamp())
    ended_slug = f"btc-updown-5m-{now_ts - 600}"
    active_slug = f"eth-updown-5m-{now_ts + 300}"
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="mixed-near-close",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="btc-yes",
                    market_slug=ended_slug,
                    outcome_label="Up",
                    target_price=0.9,
                    requested_size=5.0,
                    order_id="ended-confirmed",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker"},
                ),
                LiveExecutionLegResult(
                    leg_index=2,
                    action="BUY",
                    token_id="eth-yes",
                    market_slug=active_slug,
                    outcome_label="Up",
                    target_price=0.9,
                    requested_size=5.0,
                    order_id="active-confirmed",
                    status="CONFIRMED",
                    response={"strategy_variant": "near_close_maker"},
                ),
            ],
        )
    )

    exposure = repository.near_close_live_exposure()

    assert exposure["total"] == 4.5
    assert ended_slug not in exposure["by_market"]
    assert exposure["by_market"][active_slug] == 4.5


def test_near_close_live_exposure_offsets_matched_stop_exit_sells(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "closed-near-close-stop-exit.db"))
    token_id = "yes"
    market_slug = "eth-updown"
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
                    token_id=token_id,
                    market_slug=market_slug,
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
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="stop-exit-near-close",
            status="submitted",
            message="ok",
            order_type="FAK",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="SELL",
                    token_id=token_id,
                    market_slug=market_slug,
                    outcome_label="Yes",
                    target_price=0.65,
                    requested_size=5.0,
                    order_id="stop-exit-1",
                    status="submitted",
                    response={
                        "strategy_variant": "near_close_stop_exit",
                        "status": "matched",
                        "success": True,
                        "transactionsHashes": ["0xabc"],
                    },
                )
            ],
        )
    )

    exposure = repository.near_close_live_exposure()

    assert exposure["total"] == 0.0
    assert exposure["by_market"] == {}
    assert exposure["by_position"] == {}


def test_near_close_live_exposure_offsets_cancelled_row_with_matched_response(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "cancelled-row-matched-stop-exit.db"))
    token_id = "yes"
    market_slug = "eth-updown"
    created_at = datetime.now(timezone.utc)
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "open-near-close",
                1,
                "BUY",
                token_id,
                market_slug,
                "Yes",
                0.97,
                5.0,
                "confirmed-1",
                "CONFIRMED",
                json.dumps({"strategy_variant": "near_close_maker"}),
                created_at.isoformat(),
            ),
        )
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stop-exit-near-close",
                1,
                "SELL",
                token_id,
                market_slug,
                "Yes",
                0.03,
                5.0,
                "stop-exit-1",
                "cancelled",
                json.dumps(
                    {
                        "strategy_variant": "near_close_stop_exit",
                        "status": "matched",
                        "success": True,
                        "transactionsHashes": ["0xabc"],
                    }
                ),
                (created_at + timedelta(seconds=1)).isoformat(),
            ),
        )

    exposure = repository.near_close_live_exposure()

    assert exposure["total"] == 0.0
    assert exposure["by_market"] == {}
    assert exposure["by_position"] == {}


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


def test_loss_autopsy_records_entry_books_stop_and_hedge_context(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "loss-autopsy.db"))
    created_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    token_id = "doge-down"
    with repository.connection.transaction():
        repository.connection.execute(
            """
            INSERT INTO live_trades (
                opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                target_price, requested_size, order_id, status, response_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "loss-entry",
                1,
                "BUY",
                token_id,
                "doge-updown-5m-test",
                "Down",
                0.87,
                5.0,
                "0xloss",
                "SETTLED_LOST",
                json.dumps(
                    {
                        "strategy_variant": "near_close_maker",
                        "outcome_label": "Down",
                        "minutes_to_resolution": 2.4,
                        "crypto_start_distance": 0.0016,
                        "crypto_winning_outcome": "Down",
                        "entry_bid": 0.87,
                        "entry_ask": 0.88,
                        "current_bid": 0.86,
                        "current_midpoint": 0.87,
                    }
                ),
                created_at.isoformat(),
            ),
        )
    trade_id = repository.connection.fetchone("SELECT id FROM live_trades WHERE order_id = ?", ("0xloss",))["id"]
    repository.save_orderbooks(
        [
            OrderBookSnapshot(
                token_id=token_id,
                bids=[BookLevel(price=0.86, size=20)],
                asks=[BookLevel(price=0.88, size=20)],
                updated_at=created_at + timedelta(seconds=5),
            ),
            OrderBookSnapshot(
                token_id=token_id,
                bids=[BookLevel(price=0.17, size=5)],
                asks=[BookLevel(price=0.27, size=10)],
                updated_at=created_at + timedelta(minutes=1),
            ),
        ]
    )
    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id=f"stop-exit:doge-updown-5m-test:{token_id}",
        status="failed",
        message="PolyApiException: no orders found to match with FAK order",
        details={
            "strategy_variant": "near_close_stop_exit",
            "market_slug": "doge-updown-5m-test",
            "token_id": token_id,
            "reference_price": 0.18,
            "target_price": 0.15,
        },
    )
    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id=None,
        status="hedge_skipped",
        message="too_close_to_resolution",
        details={
            "strategy_variant": "near_close_post_fill_hedge",
            "entry_market_slug": "doge-updown-5m-test",
            "entry_token_id": token_id,
        },
    )

    autopsy = repository.save_loss_autopsy(
        [trade_id],
        risk_settings={
            "taker_exit_price": 0.52,
            "hard_stop_offset": 0.025,
            "emergency_slippage": 0.03,
        },
        settlement_details={"outcome_prices": "[\"1\", \"0\"]"},
    )
    repository.save_loss_autopsy([trade_id])

    events = [
        event
        for event in repository.recent_execution_events(limit=10)
        if event["status"] == "loss_autopsy"
    ]
    assert autopsy is not None
    assert len(events) == 1
    details = events[0]["details"]
    assert details["entry"]["market_slug"] == "doge-updown-5m-test"
    assert round(details["entry"]["entry_price"], 6) == 0.87
    assert details["first_stop_cross"]["best_bid"] == 0.17
    assert details["stop_exit"]["attempted"] is True
    assert details["hedge"]["observed"] is True
    assert "stop_exit_fak_no_match" in details["diagnosis"]
    assert "hedge_skipped_too_close_to_resolution" in details["diagnosis"]


def test_trade_autopsy_entry_snapshot_is_reportable(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "trade-autopsy-entry.db"))
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="entry-autopsy",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id="btc-up",
                    market_slug="btc-updown-5m-1780202400",
                    outcome_label="Up",
                    target_price=0.88,
                    requested_size=5.0,
                    order_id="0xentry-autopsy",
                    status="submitted",
                    response={
                        "strategy_variant": "near_close_maker",
                        "time_to_resolution_sec": 45,
                        "entry_price": 0.88,
                        "best_bid": 0.87,
                        "best_ask": 0.91,
                        "spread": 0.04,
                        "midpoint": 0.89,
                        "bid_depth_at_best": 25,
                        "ask_depth_at_best": 40,
                        "crypto_start_distance": 0.0012,
                    },
                )
            ],
        )
    )

    row = repository.connection.fetchone("SELECT response_json FROM live_trades WHERE order_id = ?", ("0xentry-autopsy",))
    response = json.loads(row["response_json"])
    report = repository.trade_autopsy_report(limit=5)

    assert response["trade_autopsy_id"].startswith("ta_")
    assert response["entry_autopsy_snapshot"]["resolution_bucket_key"] == "1780202700"
    assert report[0]["trade_autopsy_id"] == response["trade_autopsy_id"]
    assert report[0]["entry_price"] == 0.88
    assert report[0]["best_bid_at_entry"] == 0.87


def test_cancel_autopsy_records_fillability_and_hypothetical_hold_pnl(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "cancel-autopsy.db"))
    market_slug = "btc-updown-test"
    token_id = "btc-up"
    repository.save_markets(
        [],
        [
            MarketRecord(
                market_id="m-cancel-autopsy",
                question="BTC Up/Down",
                slug=market_slug,
                outcome_labels=["Up", "Down"],
                token_ids=[token_id, "btc-down"],
                active=False,
                closed=True,
                raw={"outcomes": '["Up","Down"]', "outcomePrices": '["1","0"]'},
            )
        ],
    )
    repository.save_live_execution(
        LiveExecutionResult(
            opportunity_id="cancel-autopsy-opportunity",
            status="submitted",
            message="ok",
            order_type="GTD",
            created_at=datetime.now(timezone.utc),
            leg_results=[
                LiveExecutionLegResult(
                    leg_index=1,
                    action="BUY",
                    token_id=token_id,
                    market_slug=market_slug,
                    outcome_label="Up",
                    target_price=0.89,
                    requested_size=5.0,
                    order_id="0xcancel-autopsy",
                    status="submitted",
                    response={"strategy_variant": "near_close_maker", "entry_price": 0.89},
                )
            ],
        )
    )

    repository.mark_live_orders_cancelled(
        ["0xcancel-autopsy"],
        status="qualification_cancelled",
        cancel_response={"canceled": ["0xcancel-autopsy"]},
        cancel_reason_by_order={
            "0xcancel-autopsy": {
                "not_open_reason": "would_cross_post_only",
                "not_open_reasons": ["would_cross_post_only"],
                "cancel_reason_context": {
                    "best_bid": 0.88,
                    "best_ask": 0.89,
                    "midpoint": 0.885,
                    "spread": 0.01,
                    "time_to_resolution_sec": 20,
                },
            }
        },
    )
    repository.save_orderbooks(
        [
            OrderBookSnapshot(
                token_id=token_id,
                market_id="m-cancel-autopsy",
                bids=[BookLevel(price=0.88, size=10)],
                asks=[BookLevel(price=0.89, size=8)],
                updated_at=datetime.now(timezone.utc) + timedelta(seconds=1),
            )
        ]
    )

    events = repository.cancel_autopsy_events(limit=5)
    report = repository.cancel_autopsy_report(limit=5)
    row = report["rows"][0]

    assert events[0]["status"] == "cancel_autopsy"
    assert events[0]["details"]["fillability"] == "would_cross_post_only"
    assert row["fillability"] == "likely_fill"
    assert row["cancel_quality"] == "bad_cancel"
    assert row["did_bought_outcome_win"] is True
    assert round(float(row["hypothetical_hold_pnl"]), 6) == 0.55


def test_candidate_autopsy_records_each_near_close_observation_and_reports_hold_pnl(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "candidate-autopsy.db"))
    market_slug = "btc-updown-test"
    token_id = "btc-up"
    repository.save_markets(
        [],
        [
            MarketRecord(
                market_id="m-candidate-autopsy",
                question="BTC Up/Down",
                slug=market_slug,
                outcome_labels=["Up", "Down"],
                token_ids=[token_id, "btc-down"],
                active=False,
                closed=True,
                raw={"outcomes": '["Up","Down"]', "outcomePrices": '["1","0"]'},
            )
        ],
    )
    observed_at = datetime(2026, 6, 9, 12, 0, 10, tzinfo=timezone.utc)
    base_details = {
        "strategy_variant": "near_close_maker",
        "outcome_label": "Up",
        "market_slug": market_slug,
        "token_id": token_id,
        "time_to_resolution_sec": 50,
        "entry_price": 0.89,
        "entry_bid": 0.89,
        "best_bid": 0.88,
        "best_ask": 0.91,
        "spread": 0.03,
        "midpoint": 0.895,
        "bid_depth_at_best": 30,
        "ask_depth_at_best": 24,
        "crypto_start_distance": 0.0012,
        "crypto_winning_outcome": "Up",
        "volatility_shadow_enabled": True,
        "volatility_shadow_ratio_threshold": 1.25,
        "volatility_shadow_window_sec": 60,
        "volatility_shadow_source": "binance_1s_range",
        "volatility_shadow_data_available": True,
        "volatility_shadow_range_bps": 12.0,
        "volatility_shadow_sample_count": 60,
        "volatility_shadow_ratio": 1.0,
        "volatility_shadow_would_block": True,
        "tradable_live": True,
        "post_only": True,
        "effective_order_size": 5,
    }
    opportunity = Opportunity(
        opportunity_id="candidate-autopsy-opportunity",
        strategy_type=StrategyType.LATE_RESOLUTION,
        direction=SignalDirection.BUY_BASKET,
        title="BTC Up/Down | near-close maker Up",
        summary="Near-close maker bid 0.890 on Up.",
        market_slugs=[market_slug],
        market_ids=["m-candidate-autopsy"],
        token_ids=[token_id],
        prices={"entry_bid": 0.89, "entry_ask": 0.91},
        gross_edge=0.11,
        estimated_fees=0.0,
        slippage_estimate=0.0,
        net_edge=0.10,
        max_safe_size=5.0,
        available_liquidity=30.0,
        confidence_score=0.9,
        timestamp=observed_at,
        suggested_action="Paper observe",
        details=base_details,
    )
    repository.save_opportunities([opportunity])
    repository.save_opportunities(
        [
            opportunity.model_copy(
                update={
                    "timestamp": observed_at + timedelta(seconds=8),
                    "prices": {"entry_bid": 0.90, "entry_ask": 0.92},
                    "details": {**base_details, "entry_price": 0.90, "entry_bid": 0.90, "best_ask": 0.92},
                }
            )
        ]
    )
    repository.save_orderbooks(
        [
            OrderBookSnapshot(
                token_id=token_id,
                market_id="m-candidate-autopsy",
                bids=[BookLevel(price=0.88, size=10)],
                asks=[BookLevel(price=0.89, size=8)],
                updated_at=observed_at + timedelta(seconds=20),
            )
        ]
    )

    events = repository.candidate_autopsy_events(limit=5)
    report = repository.candidate_autopsy_report(limit=5)
    first_observation = report["rows"][0]

    assert len(events) == 2
    assert len(report["rows"]) == 1
    assert report["summary"]["observation_count"] == 2
    assert events[0]["candidate_autopsy_id"].startswith("oa_")
    assert float(first_observation["entry_price"]) == 0.89
    assert first_observation["fillability"] == "likely_fill"
    assert first_observation["fillability_weight"] is None
    assert first_observation["fillability_weight_source"] == "insufficient_actual_samples"
    assert first_observation["candidate_quality"] == "would_profit"
    assert first_observation["did_bought_outcome_win"] is True
    assert first_observation["volatility_shadow_would_block"] is True
    assert float(first_observation["volatility_shadow_ratio"]) == 1.0
    assert round(float(first_observation["hypothetical_hold_pnl"]), 6) == 0.55


def test_candidate_autopsy_marks_missing_settlement_metadata(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "candidate-autopsy-missing-market.db"))
    market_slug = "eth-updown-5m-1700000000"
    token_id = "eth-down"
    observed_at = datetime(2023, 11, 14, 22, 12, 40, tzinfo=timezone.utc)
    opportunity = Opportunity(
        opportunity_id="candidate-autopsy-missing-market",
        strategy_type=StrategyType.LATE_RESOLUTION,
        direction=SignalDirection.BUY_BASKET,
        title="ETH Up/Down | near-close maker Down",
        summary="Near-close maker bid 0.900 on Down.",
        market_slugs=[market_slug],
        market_ids=["missing-market"],
        token_ids=[token_id],
        prices={"entry_bid": 0.90, "entry_ask": 0.93},
        gross_edge=0.10,
        estimated_fees=0.0,
        slippage_estimate=0.0,
        net_edge=0.09,
        max_safe_size=5.0,
        available_liquidity=30.0,
        confidence_score=0.9,
        timestamp=observed_at,
        suggested_action="Paper observe",
        details={
            "strategy_variant": "near_close_maker",
            "outcome_label": "Down",
            "market_slug": market_slug,
            "token_id": token_id,
            "time_to_resolution_sec": 34,
            "entry_price": 0.90,
            "entry_bid": 0.90,
            "best_bid": 0.92,
            "best_ask": 0.93,
            "spread": 0.01,
            "midpoint": 0.925,
            "bid_depth_at_best": 30,
            "ask_depth_at_best": 20,
            "crypto_start_distance": 0.0012,
            "crypto_winning_outcome": "Down",
            "tradable_live": True,
            "effective_order_size": 5,
        },
    )
    repository.save_opportunities([opportunity])

    report = repository.candidate_autopsy_report(limit=5)
    row = report["rows"][0]

    assert row["market_ended"] is True
    assert row["market_metadata_missing"] is True
    assert row["settlement_source"] is None
    assert row["final_outcome"] is None
    assert row["candidate_quality"] == "pending_settlement"
    assert repository.autopsy_market_slugs_needing_settlement_refresh(limit=5) == [market_slug]


def test_candidate_autopsy_maker_does_not_treat_best_bid_as_fill_evidence(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "candidate-maker-bid-touch.db"))
    observed_at = datetime(2026, 6, 28, 10, 0, 10, tzinfo=timezone.utc)
    market_slug = "btc-updown-maker-bid-touch"
    token_id = "btc-up-maker"
    opportunity = Opportunity(
        opportunity_id="candidate-maker-bid-touch",
        strategy_type=StrategyType.LATE_RESOLUTION,
        direction=SignalDirection.BUY_BASKET,
        title="BTC Up/Down | near-close maker Up",
        summary="Near-close maker candidate.",
        market_slugs=[market_slug],
        market_ids=["maker-bid-market"],
        token_ids=[token_id],
        prices={"entry_bid": 0.90, "entry_ask": 0.97},
        gross_edge=0.10,
        estimated_fees=0.0,
        slippage_estimate=0.0,
        net_edge=0.09,
        max_safe_size=5.0,
        available_liquidity=30.0,
        confidence_score=0.9,
        timestamp=observed_at,
        suggested_action="Paper observe",
        details={
            "strategy_variant": "near_close_maker",
            "outcome_label": "Up",
            "market_slug": market_slug,
            "token_id": token_id,
            "entry_price": 0.90,
            "best_bid": 0.95,
            "best_ask": 0.97,
            "ask_depth_at_best": 20,
            "effective_order_size": 5,
            "entry_execution_mode": "maker",
            "resolution_bucket_key": "maker-bid-touch-bucket",
            "rank": 1,
        },
    )
    repository.save_opportunities([opportunity])
    repository.save_orderbooks(
        [
            OrderBookSnapshot(
                token_id=token_id,
                market_id="maker-bid-market",
                bids=[BookLevel(price=0.99, size=100)],
                asks=[BookLevel(price=0.96, size=20)],
                updated_at=observed_at + timedelta(seconds=5),
            )
        ]
    )

    row = repository.candidate_autopsy_report(limit=5)["rows"][0]

    assert row["fillability"] == "unfillable"
    assert row["fillability_evidence"]["reason"] == "maker_best_ask_never_reached_bid"


def test_candidate_autopsy_taker_uses_immediate_ask_depth(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "candidate-taker-depth.db"))
    observed_at = datetime(2026, 6, 28, 10, 5, 10, tzinfo=timezone.utc)
    opportunity = Opportunity(
        opportunity_id="candidate-taker-depth",
        strategy_type=StrategyType.LATE_RESOLUTION,
        direction=SignalDirection.BUY_BASKET,
        title="SOL Up/Down | near-close taker Down",
        summary="Near-close taker candidate.",
        market_slugs=["sol-updown-taker-depth"],
        market_ids=["taker-depth-market"],
        token_ids=["sol-down-taker"],
        prices={"entry_bid": 0.86, "entry_ask": 0.87},
        gross_edge=0.13,
        estimated_fees=0.0,
        slippage_estimate=0.0,
        net_edge=0.12,
        max_safe_size=5.0,
        available_liquidity=20.0,
        confidence_score=0.9,
        timestamp=observed_at,
        suggested_action="Paper observe",
        details={
            "strategy_variant": "near_close_maker",
            "outcome_label": "Down",
            "market_slug": "sol-updown-taker-depth",
            "token_id": "sol-down-taker",
            "entry_price": 0.87,
            "best_bid": 0.86,
            "best_ask": 0.87,
            "ask_depth_at_best": 29.61,
            "effective_order_size": 5,
            "entry_execution_mode": "taker_fallback",
            "resolution_bucket_key": "taker-depth-bucket",
            "rank": 1,
        },
    )
    repository.save_opportunities([opportunity])

    row = repository.candidate_autopsy_report(limit=5)["rows"][0]

    assert row["fillability"] == "likely_fill"
    assert row["fillability_evidence"]["reason"] == "taker_immediate_ask_depth_sufficient"


def test_candidate_autopsy_prefers_actual_order_and_calibrates_from_fill_results(tmp_path) -> None:
    repository = ScannerRepository(connect_db(tmp_path / "candidate-calibration.db"))
    base_time = datetime(2026, 6, 28, 11, 0, 10, tzinfo=timezone.utc)
    for index in range(6):
        opportunity_id = f"candidate-calibration-{index}"
        opportunity = Opportunity(
            opportunity_id=opportunity_id,
            strategy_type=StrategyType.LATE_RESOLUTION,
            direction=SignalDirection.BUY_BASKET,
            title="ETH Up/Down | near-close taker Up",
            summary="Near-close taker calibration candidate.",
            market_slugs=[f"eth-updown-calibration-{index}"],
            market_ids=[f"calibration-market-{index}"],
            token_ids=[f"eth-up-{index}"],
            prices={"entry_bid": 0.86, "entry_ask": 0.87},
            gross_edge=0.13,
            estimated_fees=0.0,
            slippage_estimate=0.0,
            net_edge=0.12,
            max_safe_size=5.0,
            available_liquidity=20.0,
            confidence_score=0.9,
            timestamp=base_time + timedelta(minutes=5 * index),
            suggested_action="Paper observe",
            details={
                "strategy_variant": "near_close_maker",
                "outcome_label": "Up",
                "market_slug": f"eth-updown-calibration-{index}",
                "token_id": f"eth-up-{index}",
                "entry_price": 0.87,
                "best_bid": 0.86,
                "best_ask": 0.87,
                "ask_depth_at_best": 20,
                "effective_order_size": 5,
                "entry_execution_mode": "taker_fallback",
                "resolution_bucket_key": f"calibration-bucket-{index}",
                "rank": 1,
            },
        )
        repository.save_opportunities([opportunity])
        if index < 5:
            filled = index < 3
            repository.save_live_execution(
                LiveExecutionResult(
                    opportunity_id=opportunity_id,
                    status="confirmed" if filled else "failed",
                    message="calibration",
                    order_type="FAK",
                    created_at=base_time + timedelta(minutes=5 * index, seconds=1),
                    leg_results=[
                        LiveExecutionLegResult(
                            leg_index=1,
                            action="BUY",
                            token_id=f"eth-up-{index}",
                            market_slug=f"eth-updown-calibration-{index}",
                            outcome_label="Up",
                            target_price=0.87,
                            requested_size=5.0,
                            order_id=f"0xcalibration{index}" if filled else None,
                            status="confirmed" if filled else "failed",
                            response={
                                "status": "MATCHED" if filled else "FAILED",
                                "actual_fill_price": 0.87 if filled else None,
                                "actual_matched_size": 5.0 if filled else None,
                            },
                        )
                    ],
                )
            )

    report = repository.candidate_autopsy_report(limit=10)
    unattempted = next(row for row in report["rows"] if row["opportunity_id"] == "candidate-calibration-5")
    filled = next(row for row in report["rows"] if row["opportunity_id"] == "candidate-calibration-0")

    assert unattempted["fillability_weight"] == 0.6
    assert unattempted["fillability_weight_source"] == "mode_and_fillability"
    assert filled["actual_order_filled"] is True
    assert filled["fillability_weight"] == 1.0
    assert filled["fillability_evidence"]["source"] == "actual_live_fill"
