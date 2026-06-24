from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models.core import BookLevel, MarketRecord, OrderBookSnapshot
from app.orchestration import run_scanners
from app.scanners.liquidity_filter import LiquidityFilter
from app.scanners.late_resolution_scanner import LateResolutionScanner
from app.scanners.multi_outcome_scanner import MultiOutcomeScanner
from app.scanners.related_market_scanner import RelatedMarketScanner
from app.scanners.stale_price_scanner import StalePriceScanner
from app.scanners.sum_arb_scanner import BinarySumArbScanner


def make_book(token_id: str, *, bid: float, ask: float, size: float = 200, updated_at: datetime | None = None) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=size)],
        updated_at=updated_at or datetime.now(timezone.utc),
    )


def make_binary_market() -> MarketRecord:
    return MarketRecord(
        market_id="m-binary",
        event_id="e-binary",
        question="Will it rain?",
        slug="will-it-rain",
        outcome_labels=["Yes", "No"],
        token_ids=["yes", "no"],
        active=True,
        closed=False,
        liquidity=5000,
        end_date=datetime.now(timezone.utc) + timedelta(hours=6),
    )


def make_multi_market() -> MarketRecord:
    return MarketRecord(
        market_id="m-multi",
        event_id="e-multi",
        question="Who wins?",
        slug="who-wins",
        outcome_labels=["A", "B", "C"],
        token_ids=["a", "b", "c"],
        active=True,
        closed=False,
        liquidity=8000,
        end_date=datetime.now(timezone.utc) + timedelta(hours=6),
    )


def test_settings_clamps_near_close_gtd_to_gemini_30m() -> None:
    assert Settings(NEAR_CLOSE_GTD_SECONDS=5400).near_close_gtd_seconds == 1800


def test_settings_defaults_to_sixty_to_thirty_second_entry_experiment() -> None:
    settings = Settings()

    assert settings.near_close_entry_window_seconds() == (30.0, 60.0)
    assert settings.near_close_final_seconds_allow_entry is True
    assert settings.near_close_log_entry_telemetry is True
    assert settings.near_close_entry_seconds_allowed(15.0) is False
    assert settings.near_close_entry_seconds_allowed(30.0) is True
    assert settings.near_close_entry_seconds_allowed(61.0) is False
    assert settings.near_close_crypto_updown_min_entry_price == 0.86
    assert settings.near_close_crypto_updown_max_entry_price == 0.90
    assert settings.near_close_crypto_updown_prewarm_seconds == 60.0
    assert settings.near_close_crypto_updown_fast_scan_sec == 2.0
    assert settings.near_close_crypto_updown_taker_fallback_enabled is False
    assert settings.near_close_crypto_updown_taker_fallback_min_seconds == 30.0
    assert settings.near_close_crypto_updown_taker_fallback_max_seconds == 45.0
    assert settings.near_close_crypto_updown_taker_fallback_max_price == 0.90
    assert settings.near_close_crypto_updown_taker_fallback_max_spread == 0.02
    assert settings.near_close_crypto_updown_taker_fallback_min_start_distance_ratio == 2.0


def test_settings_switches_light_mode_when_us_equity_market_is_closed() -> None:
    settings = Settings(NEAR_CLOSE_WEEKEND_MODE_ENABLED=True, NEAR_CLOSE_US_MARKET_MODE_ENABLED=True)
    saturday = datetime(2026, 5, 23, 15, 0, tzinfo=timezone.utc)

    payload = settings.market_mode_payload(saturday)

    assert payload["us_equity_market_open"] is False
    assert payload["weekend_light_mode"] is True
    assert payload["high_frequency_mode"] is False
    assert payload["active_mode"] == "weekend_light"


def test_settings_switches_high_frequency_mode_during_us_equity_market_hours() -> None:
    settings = Settings(NEAR_CLOSE_WEEKEND_MODE_ENABLED=True, NEAR_CLOSE_US_MARKET_MODE_ENABLED=True)
    friday_core_session = datetime(2026, 5, 22, 15, 0, tzinfo=timezone.utc)

    payload = settings.market_mode_payload(friday_core_session)

    assert payload["us_equity_market_open"] is True
    assert payload["weekend_light_mode"] is False
    assert payload["high_frequency_mode"] is True
    assert payload["active_mode"] == "high_frequency"


def test_binary_sum_scanner_detects_underround() -> None:
    settings = Settings(MIN_NET_EDGE=0.001, MIN_DEPTH=10, MAX_SPREAD=0.2)
    scanner = BinarySumArbScanner(settings, LiquidityFilter(settings))
    opportunities = scanner.scan(
        [make_binary_market()],
        {
            "yes": make_book("yes", bid=0.45, ask=0.47),
            "no": make_book("no", bid=0.50, ask=0.50),
        },
    )
    assert any(op.direction.value == "buy_basket" for op in opportunities)


def test_binary_sum_scanner_detects_overround() -> None:
    settings = Settings(MIN_NET_EDGE=0.001, MIN_DEPTH=10, MAX_SPREAD=0.2)
    scanner = BinarySumArbScanner(settings, LiquidityFilter(settings))
    opportunities = scanner.scan(
        [make_binary_market()],
        {
            "yes": make_book("yes", bid=0.55, ask=0.57),
            "no": make_book("no", bid=0.48, ask=0.50),
        },
    )
    assert any(op.direction.value == "sell_basket" for op in opportunities)


def test_binary_sum_scanner_keeps_near_miss_candidate() -> None:
    settings = Settings(
        MIN_NET_EDGE=0.015,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        MIN_DEPTH=100,
        CANDIDATE_MIN_DEPTH=10,
        MAX_SPREAD=0.2,
    )
    scanner = BinarySumArbScanner(settings, LiquidityFilter(settings))
    opportunities = scanner.scan(
        [make_binary_market()],
        {
            "yes": make_book("yes", bid=0.49, ask=0.50, size=50),
            "no": make_book("no", bid=0.49, ask=0.501, size=50),
        },
    )
    assert len(opportunities) == 1
    assert opportunities[0].net_edge < 0


def test_multi_outcome_scanner_detects_underround() -> None:
    settings = Settings(MIN_NET_EDGE=0.001, MIN_DEPTH=10, MAX_SPREAD=0.2)
    scanner = MultiOutcomeScanner(settings, LiquidityFilter(settings))
    opportunities = scanner.scan(
        [make_multi_market()],
        {
            "a": make_book("a", bid=0.25, ask=0.30),
            "b": make_book("b", bid=0.20, ask=0.25),
            "c": make_book("c", bid=0.10, ask=0.20),
        },
    )
    assert any(op.strategy_type.value == "multi_outcome_sum" for op in opportunities)


def test_related_market_scanner_detects_cross_rule(tmp_path) -> None:
    rule_file = tmp_path / "rules.yaml"
    rule_file.write_text(
        """
rules:
  - rule_id: "child-lte-parent"
    description: "child should not exceed parent"
    kind: "less_than_or_equal"
    left:
      slug: "child-market"
      outcome: "Yes"
    right:
      slug: "parent-market"
      outcome: "Yes"
    tolerance: 0.01
""".strip(),
        encoding="utf-8",
    )
    settings = Settings(RELATED_RULES_PATH=str(rule_file), MIN_NET_EDGE=0.001, MIN_DEPTH=10)
    scanner = RelatedMarketScanner(settings)
    markets = [
        MarketRecord(
            market_id="child",
            event_id="e1",
            question="Child?",
            slug="child-market",
            outcome_labels=["Yes", "No"],
            token_ids=["child_yes", "child_no"],
            liquidity=4000,
        ),
        MarketRecord(
            market_id="parent",
            event_id="e1",
            question="Parent?",
            slug="parent-market",
            outcome_labels=["Yes", "No"],
            token_ids=["parent_yes", "parent_no"],
            liquidity=4000,
        ),
    ]
    books = {
        "child_yes": make_book("child_yes", bid=0.70, ask=0.72),
        "parent_yes": make_book("parent_yes", bid=0.55, ask=0.57),
    }
    opportunities = scanner.scan(markets, books)
    assert len(opportunities) == 1


def test_stale_price_scanner_flags_old_book_when_peers_move() -> None:
    now = datetime.now(timezone.utc)
    stale_market = make_binary_market()
    peer_market = MarketRecord(
        market_id="m-peer",
        event_id=stale_market.event_id,
        question="Peer market",
        slug="peer-market",
        outcome_labels=["Yes", "No"],
        token_ids=["peer_yes", "peer_no"],
        active=True,
        closed=False,
        liquidity=4000,
    )
    scanner = StalePriceScanner(stale_threshold_sec=60, peer_move_threshold=0.03)
    opportunities = scanner.scan(
        [stale_market, peer_market],
        {
            "yes": make_book("yes", bid=0.40, ask=0.42, updated_at=now - timedelta(seconds=180)),
            "peer_yes": make_book("peer_yes", bid=0.55, ask=0.60, updated_at=now),
        },
        previous_midpoints={"peer_yes": 0.45},
        now=now,
    )
    assert len(opportunities) == 1


def test_late_resolution_scanner_detects_high_probability_market() -> None:
    settings = Settings(
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
        NEAR_CLOSE_MAX_MINUTES_TO_END=6,
        NEAR_CLOSE_MAX_BID_PRICE=0.97,
        NEAR_CLOSE_LIVE_MAX_MINUTES_TO_END=3,
        NEAR_CLOSE_MIN_BEST_ASK=0.985,
        NEAR_CLOSE_MIN_MIDPOINT=0.982,
        NEAR_CLOSE_MAX_SPREAD=0.02,
        NEAR_CLOSE_MIN_DEPTH=20,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=360,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-late",
        event_id="e-late",
        question="Will alpha happen?",
        slug="will-alpha-happen",
        outcome_labels=["Yes", "No"],
        token_ids=["alpha_yes", "alpha_no"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="UMA market rules with an unambiguous data source",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    opportunities = scanner.scan(
        [market],
        {
            "alpha_yes": make_book("alpha_yes", bid=0.979, ask=0.986, size=80),
            "alpha_no": make_book("alpha_no", bid=0.010, ask=0.014, size=80),
        },
    )
    assert len(opportunities) == 1
    assert opportunities[0].strategy_type.value == "late_resolution"
    assert opportunities[0].details["strategy_variant"] == "near_close_maker"
    assert opportunities[0].details["post_only"] is True
    assert opportunities[0].details["order_type"] == "GTD"
    assert opportunities[0].prices["entry_bid"] == 0.97
    assert opportunities[0].details["tradable_live"] is True


def test_late_resolution_scanner_keeps_restricted_market_after_clob_smoke_test() -> None:
    settings = Settings(
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
        NEAR_CLOSE_MAX_MINUTES_TO_END=6,
        NEAR_CLOSE_MAX_BID_PRICE=0.97,
        NEAR_CLOSE_MIN_BEST_ASK=0.985,
        NEAR_CLOSE_MIN_MIDPOINT=0.982,
        NEAR_CLOSE_MAX_SPREAD=0.02,
        NEAR_CLOSE_MIN_DEPTH=20,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=360,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-restricted",
        event_id="e-restricted",
        question="Will alpha happen?",
        slug="restricted-alpha",
        outcome_labels=["Yes", "No"],
        token_ids=["restricted_yes", "restricted_no"],
        active=True,
        closed=False,
        restricted=True,
        liquidity=4000,
        resolution_source="UMA market rules with an unambiguous data source",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    opportunities = scanner.scan(
        [market],
        {
            "restricted_yes": make_book("restricted_yes", bid=0.979, ask=0.986, size=80),
            "restricted_no": make_book("restricted_no", bid=0.010, ask=0.014, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["restricted"] is True


def test_late_resolution_scanner_rejects_ambiguous_or_wide_spread_market() -> None:
    settings = Settings(
        NEAR_CLOSE_MIN_DEPTH=20,
        NEAR_CLOSE_MAX_SPREAD=0.02,
        NEAR_CLOSE_MAX_MINUTES_TO_END=6,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    ambiguous_market = MarketRecord(
        market_id="m-ambiguous",
        event_id="e-ambiguous",
        question="Will the court officially approve alpha?",
        slug="court-approve-alpha",
        outcome_labels=["Yes", "No"],
        token_ids=["court_yes", "court_no"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="Official court docket",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    clear_market = ambiguous_market.model_copy(
        update={
            "market_id": "m-clear",
            "slug": "clear-alpha",
            "question": "Will alpha happen?",
            "resolution_source": "UMA market rules with an unambiguous data source",
            "token_ids": ["clear_yes", "clear_no"],
        }
    )
    opportunities = scanner.scan(
        [ambiguous_market, clear_market],
        {
            "court_yes": make_book("court_yes", bid=0.970, ask=0.986, size=80),
            "court_no": make_book("court_no", bid=0.020, ask=0.025, size=80),
            "clear_yes": make_book("clear_yes", bid=0.960, ask=0.986, size=80),
            "clear_no": make_book("clear_no", bid=0.020, ask=0.026, size=80),
        },
    )
    assert opportunities == []


def test_late_resolution_scanner_accepts_official_data_but_not_live_games() -> None:
    settings = Settings(
        NEAR_CLOSE_MIN_DEPTH=20,
        NEAR_CLOSE_MAX_SPREAD=0.02,
        NEAR_CLOSE_MAX_MINUTES_TO_END=6,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=360,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    base_market = MarketRecord(
        market_id="m-cpi",
        event_id="e-cpi",
        question="Will CPI be above 3.0% after the official release?",
        slug="cpi-above-forecast",
        outcome_labels=["Yes", "No"],
        token_ids=["cpi_yes", "cpi_no"],
        active=True,
        closed=False,
        liquidity=4000,
        category="Economics",
        tags=["Macro", "Official data"],
        resolution_source="Official Bureau of Labor Statistics CPI data release",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=4),
    )
    live_game = base_market.model_copy(
        update={
            "market_id": "m-game",
            "question": "Will Team A win Game 1 of this match?",
            "slug": "live-game",
            "category": "Esports",
            "tags": ["League of Legends"],
            "resolution_source": "Official match score",
            "token_ids": ["game_yes", "game_no"],
        }
    )

    opportunities = scanner.scan(
        [base_market, live_game],
        {
            "cpi_yes": make_book("cpi_yes", bid=0.979, ask=0.986, size=80),
            "cpi_no": make_book("cpi_no", bid=0.010, ask=0.014, size=80),
            "game_yes": make_book("game_yes", bid=0.979, ask=0.986, size=80),
            "game_no": make_book("game_no", bid=0.010, ask=0.014, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].market_slugs == ["cpi-above-forecast"]
    assert opportunities[0].details["market_filter_reason"] == "official_data"


def test_late_resolution_scanner_uses_crypto_variant_thresholds_and_winner() -> None:
    settings = Settings(
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_ORDER_SIZE=2,
        NEAR_CLOSE_CRYPTO_MIN_MINUTES_TO_END=5,
        NEAR_CLOSE_CRYPTO_MAX_MINUTES_TO_END=20,
        NEAR_CLOSE_CRYPTO_MIN_BEST_ASK=0.985,
        NEAR_CLOSE_CRYPTO_MIN_MIDPOINT=0.982,
        NEAR_CLOSE_CRYPTO_MAX_SPREAD=0.015,
        NEAR_CLOSE_MIN_DEPTH=20,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=1200,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-btc",
        event_id="e-btc",
        question="Bitcoin above 70,000 on May 1?",
        slug="bitcoin-above-70000",
        outcome_labels=["Yes", "No"],
        token_ids=["btc_yes", "btc_no"],
        active=True,
        closed=False,
        liquidity=4000,
        category="Crypto",
        resolution_source="https://www.binance.com/en/trade/BTC_USDT",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=12),
        raw={
            "near_close_crypto_spot_price": 72500.0,
            "near_close_crypto_strike_price": 70000.0,
            "near_close_crypto_strike_distance": 0.0357,
            "near_close_crypto_winning_outcome": "Yes",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "btc_yes": make_book("btc_yes", bid=0.979, ask=0.986, size=80),
            "btc_no": make_book("btc_no", bid=0.010, ask=0.014, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["near_close_variant"] == "crypto"
    assert opportunities[0].details["crypto_winning_outcome"] == "Yes"
    assert opportunities[0].max_safe_size == 2


def test_late_resolution_scanner_uses_crypto_updown_proxy_variant() -> None:
    settings = Settings(
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE=5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.0025,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.65,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.08,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=8,
        NEAR_CLOSE_MIN_DEPTH=20,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=480,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-eth-updown",
        event_id="e-eth",
        question="Ethereum Up or Down - May 2, 5:55AM-6:00AM ET",
        slug="eth-updown",
        outcome_labels=["Up", "Down"],
        token_ids=["eth_up", "eth_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/eth-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=5),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 3010.0,
            "near_close_crypto_start_price": 3000.0,
            "near_close_crypto_start_distance": 0.003333,
            "near_close_crypto_winning_outcome": "Up",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "eth_up": make_book("eth_up", bid=0.64, ask=0.67, size=80),
            "eth_down": make_book("eth_down", bid=0.33, ask=0.36, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["near_close_variant"] == "crypto_updown"
    assert opportunities[0].details["crypto_winning_outcome"] == "Up"
    assert opportunities[0].max_safe_size == 5


def test_late_resolution_scanner_rejects_crypto_updown_too_close_to_start() -> None:
    settings = Settings(
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.65,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.08,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=8,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=480,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-sol-updown",
        event_id="e-sol",
        question="Solana Up or Down - May 2, 5:55AM-6:00AM ET",
        slug="sol-updown",
        outcome_labels=["Up", "Down"],
        token_ids=["sol_up", "sol_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/sol-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=3),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 85.98,
            "near_close_crypto_start_price": 86.12,
            "near_close_crypto_start_distance": 0.0016256,
            "near_close_crypto_winning_outcome": "Down",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "sol_up": make_book("sol_up", bid=0.01, ask=0.04, size=80),
            "sol_down": make_book("sol_down", bid=0.95, ask=0.98, size=80),
        },
    )

    assert opportunities == []


def test_late_resolution_scanner_uses_dynamic_crypto_updown_start_distance_ladder() -> None:
    settings = Settings(
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_START_DISTANCE_LADDER="7:0.0024,6:0.0018,5:0.00121,3.5:0.0010,1.5:0.00085,0.35:0.00085",
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.86,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_ENTRY_PRICE=0.95,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=0.35,
        NEAR_CLOSE_CRYPTO_UPDOWN_NO_NEW_ENTRY_LAST_SECONDS=0,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=8,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=480,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))

    pass_cases = [
        (7.0, 0.0024, 0.0024, "<= 7m"),
        (5.5, 0.0018, 0.0018, "<= 6m"),
        (4.0, 0.00121, 0.00121, "<= 5m"),
        (2.0, 0.0010, 0.0010, "<= 3.5m"),
        (1.0, 0.00085, 0.00085, "<= 1.5m"),
    ]
    for minutes_left, start_distance, required, rule in pass_cases:
        market = _make_crypto_updown_market(minutes_left=minutes_left, start_distance=start_distance)
        opportunities = scanner.scan(
            [market],
            {
                "dynamic_up": make_book("dynamic_up", bid=0.88, ask=0.90, size=80),
                "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.12, size=80),
            },
        )

        assert len(opportunities) == 1
        assert opportunities[0].details["crypto_start_distance_required"] == required
        assert opportunities[0].details["crypto_start_distance_rule"] == rule
        assert opportunities[0].details["crypto_start_distance_dynamic"] is True

    reject_cases = [
        (7.0, 0.0023),
        (5.5, 0.0017),
        (1.0, 0.0008),
    ]
    for minutes_left, start_distance in reject_cases:
        market = _make_crypto_updown_market(minutes_left=minutes_left, start_distance=start_distance)
        rejection_counts: dict[str, int] = {}
        opportunities = scanner.scan(
            [market],
            {
                "dynamic_up": make_book("dynamic_up", bid=0.88, ask=0.90, size=80),
                "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.12, size=80),
            },
            rejection_counts=rejection_counts,
        )

        assert opportunities == []
        assert rejection_counts == {"start_distance_below_min": 1}


def test_late_resolution_scanner_allows_crypto_updown_inside_legacy_last_90_seconds() -> None:
    settings = Settings(
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=0.35,
        NEAR_CLOSE_CRYPTO_UPDOWN_NO_NEW_ENTRY_LAST_SECONDS=90,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=8,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.0005,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.86,
        NEAR_CLOSE_ENTRY_MIN_SECONDS=0,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=1.0, start_distance=0.01)
    rejection_counts: dict[str, int] = {}

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.88, ask=0.90, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.12, size=80),
        },
        rejection_counts=rejection_counts,
    )

    assert len(opportunities) == 1
    assert rejection_counts == {}
    assert opportunities[0].details["time_to_resolution_sec"] <= 120
    assert opportunities[0].details["legacy_no_new_entry_last_seconds"] == 90
    assert opportunities[0].details["tradable_live"] is True


def test_late_resolution_scanner_blocks_entries_before_two_minute_window() -> None:
    settings = Settings(
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=8,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.0005,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.86,
        NEAR_CLOSE_ENTRY_MIN_SECONDS=0,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=2.5, start_distance=0.01)
    rejection_counts: dict[str, int] = {}

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.88, ask=0.90, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.12, size=80),
        },
        rejection_counts=rejection_counts,
    )

    assert opportunities == []
    assert rejection_counts == {"entry_before_window": 1}


def test_late_resolution_scanner_allows_final_30_seconds_when_configured() -> None:
    settings = Settings(
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.0005,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.86,
        NEAR_CLOSE_ENTRY_MIN_SECONDS=0,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=0.25, start_distance=0.01)

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.88, ask=0.90, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.12, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["time_to_resolution_sec"] <= 30
    assert opportunities[0].details["best_bid"] == 0.88
    assert opportunities[0].details["best_ask"] == 0.90
    assert round(opportunities[0].details["spread"], 6) == 0.02
    assert opportunities[0].details["bid_depth_at_best"] == 80
    assert opportunities[0].details["ask_depth_at_best"] == 80


def test_late_resolution_scanner_uses_crypto_updown_taker_fallback_in_tight_window() -> None:
    settings = Settings(
        NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MIN_SECONDS=30,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MAX_SECONDS=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MAX_PRICE=0.90,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MAX_SPREAD=0.02,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MIN_ASK_DEPTH=5,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MIN_START_DISTANCE_RATIO=2,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.00085,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.86,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_ENTRY_PRICE=0.90,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=18,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=0.65, start_distance=0.002)

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.89, ask=0.90, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.11, size=80),
        },
    )

    assert len(opportunities) == 1
    opportunity = opportunities[0]
    assert opportunity.details["entry_execution_mode"] == "taker_fallback"
    assert opportunity.details["post_only"] is False
    assert opportunity.details["order_type"] == "FAK"
    assert opportunity.details["entry_price"] == 0.90
    assert opportunity.details["taker_fallback_price"] == 0.90
    assert opportunity.details["taker_fallback_trigger"] == "tight_taker_window"
    assert opportunity.details["taker_fallback_reasons"] == []
    assert opportunity.prices["entry_bid"] == 0.90
    assert opportunity.prices["taker_fallback_price"] == 0.90
    assert opportunity.available_liquidity == 80


def test_late_resolution_scanner_keeps_maker_when_taker_fallback_window_missed() -> None:
    settings = Settings(
        NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_DYNAMIC_START_DISTANCE_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_TAKER_FALLBACK_MAX_SECONDS=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.00085,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.86,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_ENTRY_PRICE=0.90,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=18,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=0.9, start_distance=0.002)

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.89, ask=0.90, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.11, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["entry_execution_mode"] == "maker_post_only"
    assert opportunities[0].details["post_only"] is True
    assert opportunities[0].details["order_type"] == "GTD"
    assert "taker_fallback_before_window" in opportunities[0].details["taker_fallback_reasons"]


def test_weekend_mode_lightens_crypto_updown_size_and_relaxes_spread() -> None:
    settings = Settings(
        NEAR_CLOSE_WEEKEND_MODE_ENABLED=True,
        NEAR_CLOSE_WEEKEND_MODE_FORCE=True,
        NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER=0.5,
        NEAR_CLOSE_WEEKEND_SPREAD_MULTIPLIER=1.2,
        NEAR_CLOSE_WEEKEND_START_DISTANCE_MULTIPLIER=0.15,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_BEST_ASK=0.78,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_MIDPOINT=0.76,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.78,
        NEAR_CLOSE_CRYPTO_UPDOWN_CANCEL_START_DISTANCE=0.00012,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE=5,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=30, start_distance=0.0028)

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.88, ask=0.90, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.12, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].max_safe_size == 2.5
    assert opportunities[0].details["weekend_mode"] is True
    assert opportunities[0].details["cancel_if"]["best_ask_below"] == 0.78
    assert opportunities[0].details["cancel_if"]["midpoint_below"] == 0.76
    assert opportunities[0].details["min_entry_price"] == 0.78
    assert round(opportunities[0].details["crypto_start_distance_required"], 6) == 0.00045
    assert round(opportunities[0].details["effective_max_spread"], 6) == 0.06
    assert round(settings.effective_near_close_start_distance(0.00085), 7) == 0.0001275
    assert settings.effective_near_close_start_distance(0.00085) > settings.near_close_crypto_updown_cancel_start_distance


def test_weekend_mode_qualifies_crypto_updown_half_size_order() -> None:
    settings = Settings(
        NEAR_CLOSE_WEEKEND_MODE_ENABLED=True,
        NEAR_CLOSE_WEEKEND_MODE_FORCE=True,
        NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER=0.5,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_BEST_ASK=0.78,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_MIDPOINT=0.76,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.78,
        NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE=5,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    market = _make_crypto_updown_market(minutes_left=4, start_distance=0.01)

    opportunities = run_scanners(
        settings,
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.88, ask=0.89, size=10),
            "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.11, size=10),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].max_safe_size == 2.5
    assert opportunities[0].details["qualification_tier"] == "actionable"


def test_weekend_mode_can_override_crypto_updown_size_for_clob_minimum() -> None:
    settings = Settings(
        NEAR_CLOSE_WEEKEND_MODE_ENABLED=True,
        NEAR_CLOSE_WEEKEND_MODE_FORCE=True,
        NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER=0.5,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_ORDER_SIZE=5,
        NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE=5,
    )

    assert settings.effective_near_close_order_size("crypto_updown") == 5.0


def test_high_frequency_mode_keeps_crypto_updown_price_heat_thresholds() -> None:
    settings = Settings(
        NEAR_CLOSE_WEEKEND_MODE_ENABLED=True,
        NEAR_CLOSE_WEEKEND_MODE_FORCE=False,
        NEAR_CLOSE_WEEKEND_ORDER_SIZE_MULTIPLIER=0.5,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_ORDER_SIZE=7,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_BEST_ASK=0.78,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_MIDPOINT=0.76,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.78,
    )

    assert settings.effective_near_close_min_best_ask("crypto_updown") == 0.84
    assert settings.effective_near_close_min_midpoint("crypto_updown") == 0.84
    assert settings.effective_near_close_min_entry_price("crypto_updown") == 0.86
    assert settings.effective_near_close_order_size("crypto_updown") == 5.0


def test_weekend_mode_accepts_relaxed_crypto_updown_price_heat() -> None:
    settings = Settings(
        NEAR_CLOSE_WEEKEND_MODE_ENABLED=True,
        NEAR_CLOSE_WEEKEND_MODE_FORCE=True,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_BEST_ASK=0.78,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_MIDPOINT=0.76,
        NEAR_CLOSE_WEEKEND_CRYPTO_UPDOWN_MIN_ENTRY_PRICE=0.78,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.84,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=4, start_distance=0.01)

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.779, ask=0.79, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.20, ask=0.21, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["entry_bid"] >= 0.78
    assert opportunities[0].details["entry_ask"] == 0.79
    assert opportunities[0].details["cancel_if"]["best_ask_below"] == 0.78
    assert opportunities[0].details["cancel_if"]["midpoint_below"] == 0.76


def test_weekend_mode_accepts_crypto_updown_spread_after_relaxing() -> None:
    settings = Settings(
        NEAR_CLOSE_WEEKEND_MODE_ENABLED=True,
        NEAR_CLOSE_WEEKEND_MODE_FORCE=True,
        NEAR_CLOSE_WEEKEND_SPREAD_MULTIPLIER=1.2,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=30, start_distance=0.01)
    rejection_counts: dict[str, int] = {}

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.84, ask=0.895, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.10, ask=0.12, size=80),
        },
        rejection_counts=rejection_counts,
    )

    assert len(opportunities) == 1
    assert rejection_counts == {}
    assert round(opportunities[0].details["effective_max_spread"], 6) == 0.06


def _make_crypto_updown_market(*, minutes_left: float, start_distance: float) -> MarketRecord:
    return MarketRecord(
        market_id=f"m-dynamic-{minutes_left}-{start_distance}",
        event_id="e-dynamic",
        question="Ethereum Up or Down - May 2, 5:55AM-6:00AM ET",
        slug=f"dynamic-updown-{minutes_left}-{start_distance}",
        outcome_labels=["Up", "Down"],
        token_ids=["dynamic_up", "dynamic_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/eth-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=minutes_left),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 3000.0 * (1.0 + start_distance),
            "near_close_crypto_start_price": 3000.0,
            "near_close_crypto_start_distance": start_distance,
            "near_close_crypto_winning_outcome": "Up",
        },
    )


def test_late_resolution_scanner_blocks_live_below_crypto_updown_cancel_distance() -> None:
    settings = Settings(
        NEAR_CLOSE_MAKER_LIVE_ENABLED=True,
        NEAR_CLOSE_LIVE_MAX_MINUTES_TO_END=5,
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.001,
        NEAR_CLOSE_CRYPTO_UPDOWN_CANCEL_START_DISTANCE=0.002,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.65,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.08,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=8,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=480,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-sol-updown-live",
        event_id="e-sol",
        question="Solana Up or Down - May 2, 5:55AM-6:00AM ET",
        slug="sol-updown-live",
        outcome_labels=["Up", "Down"],
        token_ids=["sol_up", "sol_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/sol-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=3),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 85.98,
            "near_close_crypto_start_price": 86.12,
            "near_close_crypto_start_distance": 0.0016256,
            "near_close_crypto_winning_outcome": "Down",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "sol_up": make_book("sol_up", bid=0.01, ask=0.04, size=80),
            "sol_down": make_book("sol_down", bid=0.95, ask=0.98, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["tradable_live"] is False


def test_late_resolution_scanner_prices_crypto_updown_with_gemini_30m_params() -> None:
    settings = Settings(
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_GTD_SECONDS=1800,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE=0.988,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_ENTRY_PRICE=0.988,
        NEAR_CLOSE_CRYPTO_UPDOWN_SKIP_BID_AT_OR_ABOVE=1.0,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT=0.003,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-btc-updown",
        event_id="e-btc",
        question="Bitcoin Up or Down - May 2, 5:00AM-9:00AM ET",
        slug="btc-updown",
        outcome_labels=["Up", "Down"],
        token_ids=["btc_up", "btc_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/btc-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 101000.0,
            "near_close_crypto_start_price": 100000.0,
            "near_close_crypto_start_distance": 0.01,
            "near_close_crypto_winning_outcome": "Up",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "btc_up": make_book("btc_up", bid=0.986, ask=0.995, size=80),
            "btc_down": make_book("btc_down", bid=0.003, ask=0.006, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].prices["entry_bid"] == 0.987
    assert opportunities[0].details["expiration_sec"] == 1800
    assert opportunities[0].details["max_bid_price"] == 0.988
    assert opportunities[0].details["entry_formula"] == "max(best_bid + tick, midpoint - discount)"


def test_late_resolution_scanner_skips_crypto_updown_when_bid_is_too_high() -> None:
    settings = Settings(
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE=0.988,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT=0.003,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-btc-updown-cap",
        event_id="e-btc",
        question="Bitcoin Up or Down - May 2, 5:00AM-9:00AM ET",
        slug="btc-updown-cap",
        outcome_labels=["Up", "Down"],
        token_ids=["btc_up", "btc_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/btc-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 101000.0,
            "near_close_crypto_start_price": 100000.0,
            "near_close_crypto_start_distance": 0.01,
            "near_close_crypto_winning_outcome": "Up",
        },
    )

    rejection_counts: dict[str, int] = {}
    opportunities = scanner.scan(
        [market],
        {
            "btc_up": make_book("btc_up", bid=0.986, ask=0.995, size=80),
            "btc_down": make_book("btc_down", bid=0.003, ask=0.006, size=80),
        },
        rejection_counts=rejection_counts,
    )

    assert opportunities == []
    assert rejection_counts == {"bid_at_or_above_skip": 1}


def test_late_resolution_scanner_caps_crypto_updown_entry_at_090_below_skip_bid() -> None:
    settings = Settings(
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.05,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE=0.988,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT=0.003,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-btc-updown-cap",
        event_id="e-btc",
        question="Bitcoin Up or Down - May 2, 5:00AM-9:00AM ET",
        slug="btc-updown-cap",
        outcome_labels=["Up", "Down"],
        token_ids=["btc_up", "btc_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/btc-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 101000.0,
            "near_close_crypto_start_price": 100000.0,
            "near_close_crypto_start_distance": 0.01,
            "near_close_crypto_winning_outcome": "Up",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "btc_up": make_book("btc_up", bid=0.95, ask=0.995, size=80),
            "btc_down": make_book("btc_down", bid=0.003, ask=0.006, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].prices["entry_bid"] == 0.90
    assert opportunities[0].details["min_entry_price"] == 0.86
    assert opportunities[0].details["max_entry_price"] == 0.90
    assert opportunities[0].details["skip_bid_at_or_above"] == 0.96


def test_late_resolution_scanner_rejects_crypto_updown_entry_below_086() -> None:
    settings = Settings(
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.04,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-eth-updown-low",
        event_id="e-eth",
        question="Ethereum Up or Down - May 2, 5:00AM-9:00AM ET",
        slug="eth-updown-low",
        outcome_labels=["Up", "Down"],
        token_ids=["eth_up", "eth_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/eth-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 3010.0,
            "near_close_crypto_start_price": 3000.0,
            "near_close_crypto_start_distance": 0.00333,
            "near_close_crypto_winning_outcome": "Up",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "eth_up": make_book("eth_up", bid=0.84, ask=0.87, size=80),
            "eth_down": make_book("eth_down", bid=0.12, ask=0.15, size=80),
        },
    )

    assert opportunities == []


def test_late_resolution_scanner_accepts_small_crypto_updown_distance_and_depth() -> None:
    settings = Settings(
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.04,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-eth-updown-close",
        event_id="e-eth",
        question="Ethereum Up or Down - May 2, 5:00AM-9:00AM ET",
        slug="eth-updown-close",
        outcome_labels=["Up", "Down"],
        token_ids=["eth_up", "eth_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/eth-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=31),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 2989.5,
            "near_close_crypto_start_price": 3000.0,
            "near_close_crypto_start_distance": 0.0035,
            "near_close_crypto_winning_outcome": "Down",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "eth_up": make_book("eth_up", bid=0.06, ask=0.07, size=80),
            "eth_down": make_book("eth_down", bid=0.92, ask=0.93, size=12),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["near_close_variant"] == "crypto_updown"
    assert opportunities[0].details["min_depth"] == 10
    assert opportunities[0].available_liquidity == 12


def test_late_resolution_scanner_falls_back_to_best_bid_when_tick_would_cross() -> None:
    settings = Settings(
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.04,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
    )
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = _make_crypto_updown_market(minutes_left=4, start_distance=0.01)

    opportunities = scanner.scan(
        [market],
        {
            "dynamic_up": make_book("dynamic_up", bid=0.869, ask=0.87, size=80),
            "dynamic_down": make_book("dynamic_down", bid=0.12, ask=0.13, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].prices["entry_bid"] == 0.869
    assert opportunities[0].details["entry_bid"] < opportunities[0].details["entry_ask"]
    assert "fallback best_bid" in opportunities[0].details["entry_formula"]


def test_run_scanners_keeps_small_crypto_updown_order_size() -> None:
    settings = Settings(
        NEAR_CLOSE_CRYPTO_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ENABLED=True,
        NEAR_CLOSE_CRYPTO_UPDOWN_ORDER_SIZE=5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.0025,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.65,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.08,
        NEAR_CLOSE_CRYPTO_UPDOWN_SKIP_BID_AT_OR_ABOVE=1.0,
        NEAR_CLOSE_MIN_DEPTH=20,
        NEAR_CLOSE_ENTRY_MAX_SECONDS=2700,
        CANDIDATE_MIN_NET_EDGE=-0.0035,
    )
    market = MarketRecord(
        market_id="m-eth-updown",
        event_id="e-eth",
        question="Ethereum Up or Down - May 2, 8:00PM-12:00AM ET",
        slug="eth-updown",
        outcome_labels=["Up", "Down"],
        token_ids=["eth_up", "eth_down"],
        active=True,
        closed=False,
        liquidity=4000,
        resolution_source="https://data.chain.link/streams/eth-usd",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=30),
        raw={
            "near_close_crypto_variant": "updown_proxy",
            "near_close_crypto_spot_price": 3000.0,
            "near_close_crypto_start_price": 3020.0,
            "near_close_crypto_start_distance": 0.0066,
            "near_close_crypto_winning_outcome": "Down",
        },
    )

    opportunities = run_scanners(
        settings,
        [market],
        {
            "eth_up": make_book("eth_up", bid=0.003, ask=0.026, size=80),
            "eth_down": make_book("eth_down", bid=0.974, ask=0.997, size=80),
        },
    )

    assert len(opportunities) == 1
    assert opportunities[0].details["near_close_variant"] == "crypto_updown"
    assert opportunities[0].details["qualification_tier"] == "actionable"
    assert opportunities[0].max_safe_size == 5


def test_late_resolution_scanner_rejects_crypto_near_strike() -> None:
    settings = Settings(NEAR_CLOSE_CRYPTO_ENABLED=True)
    scanner = LateResolutionScanner(settings, LiquidityFilter(settings))
    market = MarketRecord(
        market_id="m-btc-close",
        event_id="e-btc",
        question="Bitcoin above 70,000 on May 1?",
        slug="bitcoin-above-70000-close",
        outcome_labels=["Yes", "No"],
        token_ids=["btc_yes", "btc_no"],
        active=True,
        closed=False,
        liquidity=4000,
        category="Crypto",
        resolution_source="https://www.binance.com/en/trade/BTC_USDT",
        end_date=datetime.now(timezone.utc) + timedelta(minutes=12),
        raw={
            "near_close_crypto_spot_price": 70700.0,
            "near_close_crypto_strike_price": 70000.0,
            "near_close_crypto_strike_distance": 0.01,
            "near_close_crypto_winning_outcome": "Yes",
        },
    )

    opportunities = scanner.scan(
        [market],
        {
            "btc_yes": make_book("btc_yes", bid=0.979, ask=0.986, size=80),
            "btc_no": make_book("btc_no", bid=0.010, ask=0.014, size=80),
        },
    )

    assert opportunities == []
