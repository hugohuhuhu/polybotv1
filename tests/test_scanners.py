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
        NEAR_CLOSE_MIN_BEST_ASK=0.985,
        NEAR_CLOSE_MIN_MIDPOINT=0.982,
        NEAR_CLOSE_MAX_SPREAD=0.02,
        NEAR_CLOSE_MIN_DEPTH=20,
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
    assert opportunities[0].details["tradable_live"] is False


def test_late_resolution_scanner_keeps_restricted_market_after_clob_smoke_test() -> None:
    settings = Settings(
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
        NEAR_CLOSE_MAX_MINUTES_TO_END=6,
        NEAR_CLOSE_MAX_BID_PRICE=0.97,
        NEAR_CLOSE_MIN_BEST_ASK=0.985,
        NEAR_CLOSE_MIN_MIDPOINT=0.982,
        NEAR_CLOSE_MAX_SPREAD=0.02,
        NEAR_CLOSE_MIN_DEPTH=20,
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
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=8,
        NEAR_CLOSE_MIN_DEPTH=20,
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


def test_late_resolution_scanner_prices_crypto_updown_with_gemini_30m_params() -> None:
    settings = Settings(
        CANDIDATE_MIN_NET_EDGE=-0.0035,
        NEAR_CLOSE_GTD_SECONDS=1800,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MINUTES_TO_END=1.5,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_MINUTES_TO_END=45,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_START_DISTANCE=0.003,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_BEST_ASK=0.75,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_MIDPOINT=0.60,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_SPREAD=0.04,
        NEAR_CLOSE_CRYPTO_UPDOWN_MAX_BID_PRICE=0.988,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIN_DEPTH=10,
        NEAR_CLOSE_CRYPTO_UPDOWN_MIDPOINT_DISCOUNT=0.003,
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
        NEAR_CLOSE_MIN_DEPTH=20,
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
