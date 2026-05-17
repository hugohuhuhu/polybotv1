from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models.core import BookLevel, MarketRecord, OrderBookSnapshot
from app.orchestration import run_scanners, shortlist_markets, shortlist_near_close_markets


def make_market(
    market_id: str,
    *,
    event_id: str,
    slug: str,
    question: str,
    yes_price: float,
    liquidity: float,
    spread: float = 0.01,
    volume: float = 1000.0,
    end_hours: int = 12,
) -> MarketRecord:
    return MarketRecord(
        market_id=market_id,
        event_id=event_id,
        event_slug=event_id,
        question=question,
        slug=slug,
        outcome_labels=["Yes", "No"],
        outcome_prices=[yes_price, round(1.0 - yes_price, 4)],
        token_ids=[f"{market_id}-yes", f"{market_id}-no"],
        active=True,
        closed=False,
        liquidity=liquidity,
        volume=volume,
        spread=spread,
        best_bid=max(yes_price - (spread / 2), 0.0),
        best_ask=min(yes_price + (spread / 2), 1.0),
        last_trade_price=yes_price,
        end_date=datetime.now(timezone.utc) + timedelta(hours=end_hours),
        resolution_source="UMA market rules with an unambiguous data source",
    )


def test_shortlist_excludes_extreme_long_tail_binary_by_default() -> None:
    settings = Settings(
        WATCH_MARKET_LIMIT=2,
        WATCH_BUCKET_GENERAL_LIMIT=2,
        WATCH_BUCKET_EVENT_LIMIT=0,
        WATCH_BUCKET_RECENT_LIMIT=0,
        WATCH_BUCKET_SPECIAL_LIMIT=0,
    )
    markets = [
        make_market(
            "tail",
            event_id="world-cup",
            slug="world-cup-very-long-tail",
            question="Very long tail market",
            yes_price=0.01,
            liquidity=50000,
            spread=0.03,
        ),
        make_market(
            "core",
            event_id="macro",
            slug="macro-core-market",
            question="Tradable core market",
            yes_price=0.48,
            liquidity=8000,
            spread=0.01,
        ),
    ]

    shortlisted, diagnostics = shortlist_markets(markets, 2, settings=settings)

    assert [market.slug for market in shortlisted] == ["macro-core-market"]
    assert diagnostics["excluded_long_tail_count"] == 1


def test_shortlist_respects_event_family_cap_and_bucket_diversity() -> None:
    settings = Settings(
        WATCH_MARKET_LIMIT=5,
        WATCH_BUCKET_GENERAL_LIMIT=5,
        WATCH_BUCKET_EVENT_LIMIT=0,
        WATCH_BUCKET_RECENT_LIMIT=0,
        WATCH_BUCKET_SPECIAL_LIMIT=0,
        WATCH_EVENT_FAMILY_CAP=2,
    )
    markets = [
        make_market(
            f"fam-{index}",
            event_id="same-family",
            slug=f"same-family-{index}",
            question=f"Family market {index}",
            yes_price=0.45 + (index * 0.01),
            liquidity=10000 - (index * 100),
        )
        for index in range(4)
    ]
    markets.append(
        make_market(
            "other",
            event_id="other-family",
            slug="other-family-market",
            question="Other family market",
            yes_price=0.5,
            liquidity=7000,
        )
    )

    shortlisted, diagnostics = shortlist_markets(markets, 5, settings=settings)

    shortlisted_slugs = [market.slug for market in shortlisted]
    assert shortlisted_slugs.count("other-family-market") == 1
    assert len([slug for slug in shortlisted_slugs if slug.startswith("same-family-")]) == 2
    assert diagnostics["excluded_family_cap_count"] >= 1


def test_shortlist_prefers_recent_positive_edge_signal_over_raw_liquidity() -> None:
    settings = Settings(
        WATCH_MARKET_LIMIT=1,
        WATCH_BUCKET_GENERAL_LIMIT=1,
        WATCH_BUCKET_EVENT_LIMIT=0,
        WATCH_BUCKET_RECENT_LIMIT=0,
        WATCH_BUCKET_SPECIAL_LIMIT=0,
    )
    markets = [
        make_market(
            "high-liq",
            event_id="liq",
            slug="high-liquidity-stale",
            question="High liquidity stale market",
            yes_price=0.08,
            liquidity=50000,
            spread=0.025,
        ),
        make_market(
            "edge",
            event_id="edge",
            slug="recent-positive-edge",
            question="Recent positive edge market",
            yes_price=0.49,
            liquidity=6000,
            spread=0.008,
        ),
    ]

    shortlisted, diagnostics = shortlist_markets(
        markets,
        1,
        settings=settings,
        positive_edge_hits={"recent-positive-edge": 3},
    )

    assert shortlisted[0].slug == "recent-positive-edge"
    assert diagnostics["shortlist_reason_counts"]["recent_positive_edge"] >= 1


def test_shortlist_excludes_xrp_related_markets() -> None:
    settings = Settings(
        WATCH_MARKET_LIMIT=2,
        WATCH_BUCKET_GENERAL_LIMIT=2,
        WATCH_BUCKET_EVENT_LIMIT=0,
        WATCH_BUCKET_RECENT_LIMIT=0,
        WATCH_BUCKET_SPECIAL_LIMIT=0,
    )
    markets = [
        make_market(
            "xrp",
            event_id="crypto",
            slug="xrp-updown-market",
            question="XRP Up or Down - later today?",
            yes_price=0.52,
            liquidity=12000,
        ),
        make_market(
            "btc",
            event_id="crypto",
            slug="bitcoin-core-market",
            question="Bitcoin core market",
            yes_price=0.51,
            liquidity=9000,
        ),
    ]

    shortlisted, _diagnostics = shortlist_markets(markets, 2, settings=settings)

    assert [market.slug for market in shortlisted] == ["bitcoin-core-market"]


def test_near_close_pool_only_keeps_upcoming_clear_binary_markets() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_POOL_LIMIT=2,
        NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES=20,
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
    )
    now = datetime.now(timezone.utc)
    clear = make_market(
        "clear",
        event_id="near",
        slug="clear-near-close",
        question="Will alpha happen?",
        yes_price=0.99,
        liquidity=5000,
    ).model_copy(update={"end_date": now + timedelta(minutes=8)})
    far = clear.model_copy(update={"market_id": "far", "slug": "far-market", "end_date": now + timedelta(minutes=45)})
    ambiguous = clear.model_copy(
        update={
            "market_id": "court",
            "slug": "court-market",
            "question": "Will the court officially approve alpha?",
        }
    )

    shortlisted, diagnostics = shortlist_near_close_markets([far, ambiguous, clear], settings=settings)

    assert [market.slug for market in shortlisted] == ["clear-near-close"]
    assert diagnostics["watch_bucket_counts"]["near_close"] == 1
    assert diagnostics["shortlist_mode"] == "near_close"
    assert diagnostics["shortlisted_markets"][0]["reasons"][-1] in {"official_data", "unambiguous_data_source"}


def test_near_close_pool_accepts_official_data_and_rejects_live_games() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_POOL_LIMIT=5,
        NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES=75,
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
    )
    now = datetime.now(timezone.utc)
    official_data = make_market(
        "cpi",
        event_id="macro",
        slug="cpi-above-forecast",
        question="Will CPI be above 3.0% after the official release?",
        yes_price=0.99,
        liquidity=9000,
    ).model_copy(
        update={
            "end_date": now + timedelta(minutes=12),
            "resolution_source": "Official Bureau of Labor Statistics CPI data release",
            "category": "Economics",
            "tags": ["Macro", "Official data"],
        }
    )
    live_game = official_data.model_copy(
        update={
            "market_id": "game",
            "slug": "live-game-market",
            "question": "Will Team A win Game 1 of this match?",
            "resolution_source": "Official match score",
            "category": "Esports",
            "tags": ["League of Legends"],
        }
    )

    shortlisted, diagnostics = shortlist_near_close_markets([official_data, live_game], settings=settings)

    assert [market.slug for market in shortlisted] == ["cpi-above-forecast"]
    assert diagnostics["shortlisted_markets"][0]["reasons"][-1] == "official_data"


def test_near_close_pool_accepts_crypto_far_from_strike() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_POOL_LIMIT=5,
        NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES=75,
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
    )
    now = datetime.now(timezone.utc)
    crypto = make_market(
        "btc",
        event_id="crypto",
        slug="bitcoin-above-70000",
        question="Bitcoin above 70,000 on May 1?",
        yes_price=0.99,
        liquidity=9000,
    ).model_copy(
        update={
            "end_date": now + timedelta(minutes=12),
            "resolution_source": "https://www.binance.com/en/trade/BTC_USDT",
            "category": "Crypto",
            "raw": {
                "near_close_crypto_spot_price": 72500.0,
                "near_close_crypto_strike_price": 70000.0,
                "near_close_crypto_strike_distance": 0.0357,
                "near_close_crypto_winning_outcome": "Yes",
            },
        }
    )

    shortlisted, diagnostics = shortlist_near_close_markets([crypto], settings=settings)

    assert [market.slug for market in shortlisted] == ["bitcoin-above-70000"]
    assert diagnostics["shortlisted_markets"][0]["reasons"][-1] == "crypto_far_from_strike"


def test_near_close_pool_accepts_crypto_updown_proxy_far_from_start() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_POOL_LIMIT=5,
        NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES=75,
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
    )
    now = datetime.now(timezone.utc)
    crypto = make_market(
        "eth-updown",
        event_id="crypto",
        slug="ethereum-updown",
        question="Ethereum Up or Down - May 2, 5:55AM-6:00AM ET",
        yes_price=0.68,
        liquidity=9000,
    ).model_copy(
        update={
            "outcome_labels": ["Up", "Down"],
            "token_ids": ["eth-up", "eth-down"],
            "end_date": now + timedelta(minutes=8),
            "resolution_source": "https://data.chain.link/streams/eth-usd",
            "raw": {
                "near_close_crypto_variant": "updown_proxy",
                "near_close_crypto_spot_price": 3010.0,
                "near_close_crypto_start_price": 3000.0,
                "near_close_crypto_start_distance": 0.003333,
                "near_close_crypto_winning_outcome": "Up",
            },
        }
    )

    shortlisted, diagnostics = shortlist_near_close_markets([crypto], settings=settings)

    assert [market.slug for market in shortlisted] == ["ethereum-updown"]
    assert diagnostics["shortlisted_markets"][0]["reasons"][-1] == "crypto_updown_proxy_price_ready"


def test_near_close_pool_crypto_updown_only_filters_first() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_POOL_LIMIT=5,
        NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES=75,
        NEAR_CLOSE_SCAN_CRYPTO_UPDOWN_ONLY=True,
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
    )
    now = datetime.now(timezone.utc)
    official = make_market(
        "official",
        event_id="macro",
        slug="cpi-above-forecast",
        question="Will official CPI data be above forecast?",
        yes_price=0.99,
        liquidity=12000,
    ).model_copy(
        update={
            "end_date": now + timedelta(minutes=8),
            "resolution_source": "https://www.bls.gov/cpi/",
        }
    )
    fixed_strike = make_market(
        "btc",
        event_id="crypto",
        slug="bitcoin-above-70000",
        question="Bitcoin above 70,000 on May 1?",
        yes_price=0.99,
        liquidity=9000,
    ).model_copy(
        update={
            "end_date": now + timedelta(minutes=8),
            "resolution_source": "https://www.binance.com/en/trade/BTC_USDT",
            "category": "Crypto",
            "raw": {
                "near_close_crypto_spot_price": 72500.0,
                "near_close_crypto_strike_price": 70000.0,
                "near_close_crypto_strike_distance": 0.0357,
                "near_close_crypto_winning_outcome": "Yes",
            },
        }
    )
    updown = make_market(
        "eth-updown",
        event_id="crypto",
        slug="ethereum-updown",
        question="Ethereum Up or Down - May 2, 5:55AM-6:00AM ET",
        yes_price=0.68,
        liquidity=9000,
    ).model_copy(
        update={
            "event_title": "Ethereum Up or Down - May 2, 5:55AM-6:00AM ET",
            "outcome_labels": ["Up", "Down"],
            "token_ids": ["eth-up", "eth-down"],
            "end_date": now + timedelta(minutes=8),
            "resolution_source": "https://data.chain.link/streams/eth-usd",
            "raw": {
                "eventStartTime": (now - timedelta(minutes=7)).isoformat(),
                "near_close_crypto_variant": "updown_proxy",
                "near_close_crypto_spot_price": 3010.0,
                "near_close_crypto_start_price": 3000.0,
                "near_close_crypto_start_distance": 0.003333,
                "near_close_crypto_winning_outcome": "Up",
            },
        }
    )

    shortlisted, diagnostics = shortlist_near_close_markets([official, fixed_strike, updown], settings=settings)

    assert [market.slug for market in shortlisted] == ["ethereum-updown"]
    assert diagnostics["near_close_scan_crypto_updown_only"] is True
    assert diagnostics["crypto_updown_discovered_count"] == 1


def test_near_close_pool_excludes_xrp_related_markets() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_POOL_LIMIT=5,
        NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES=75,
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
    )
    now = datetime.now(timezone.utc)
    xrp_market = make_market(
        "xrp-updown",
        event_id="crypto",
        slug="xrp-updown",
        question="XRP Up or Down - May 2, 5:55AM-6:00AM ET",
        yes_price=0.68,
        liquidity=9000,
    ).model_copy(
        update={
            "outcome_labels": ["Up", "Down"],
            "token_ids": ["xrp-up", "xrp-down"],
            "end_date": now + timedelta(minutes=8),
            "resolution_source": "https://data.chain.link/streams/xrp-usd",
            "raw": {
                "near_close_crypto_variant": "updown_proxy",
                "near_close_crypto_spot_price": 2.01,
                "near_close_crypto_start_price": 2.0,
                "near_close_crypto_start_distance": 0.005,
                "near_close_crypto_winning_outcome": "Up",
            },
        }
    )

    shortlisted, diagnostics = shortlist_near_close_markets([xrp_market], settings=settings)

    assert shortlisted == []
    assert diagnostics["watch_bucket_counts"]["near_close"] == 0


def test_near_close_pool_keeps_restricted_markets_as_tradeable_risk_label() -> None:
    settings = Settings(
        NEAR_CLOSE_SCAN_POOL_LIMIT=5,
        NEAR_CLOSE_SCAN_LOOKAHEAD_MINUTES=75,
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
    )
    now = datetime.now(timezone.utc)
    restricted_market = make_market(
        "restricted",
        event_id="near",
        slug="restricted-near-close",
        question="Will alpha happen?",
        yes_price=0.99,
        liquidity=5000,
    ).model_copy(
        update={
            "end_date": now + timedelta(minutes=8),
            "restricted": True,
        }
    )

    shortlisted, diagnostics = shortlist_near_close_markets([restricted_market], settings=settings)

    assert [market.slug for market in shortlisted] == ["restricted-near-close"]
    assert "restricted_market" in diagnostics["shortlisted_markets"][0]["reasons"]
    restricted_stage = next(stage for stage in diagnostics["near_close_funnel"] if stage["label"] == "restricted 標記")
    assert restricted_stage["count"] == 1


def test_run_scanners_defaults_to_near_close_only() -> None:
    settings = Settings(
        NEAR_CLOSE_MIN_MINUTES_TO_END=3,
        NEAR_CLOSE_MAX_MINUTES_TO_END=6,
        NEAR_CLOSE_MIN_DEPTH=20,
    )
    market = make_market(
        "near",
        event_id="near",
        slug="near-close-market",
        question="Will alpha happen?",
        yes_price=0.99,
        liquidity=5000,
    ).model_copy(update={"end_date": datetime.now(timezone.utc) + timedelta(minutes=4)})
    books = {
        "near-yes": OrderBookSnapshot(
            token_id="near-yes",
            bids=[BookLevel(price=0.979, size=80)],
            asks=[BookLevel(price=0.986, size=80)],
            updated_at=datetime.now(timezone.utc),
        ),
        "near-no": OrderBookSnapshot(
            token_id="near-no",
            bids=[BookLevel(price=0.010, size=80)],
            asks=[BookLevel(price=0.014, size=80)],
            updated_at=datetime.now(timezone.utc),
        ),
    }

    opportunities = run_scanners(settings, [market], books, previous_midpoints={"near-yes": 0.5})

    assert len(opportunities) == 1
    assert opportunities[0].details["strategy_variant"] == "near_close_maker"
