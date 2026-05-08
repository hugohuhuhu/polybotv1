from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.models.core import BookLevel, MarketRecord, Opportunity, OrderBookSnapshot, SignalDirection, StrategyType
from app.scanners.liquidity_filter import LiquidityFilter
from app.utils.math_utils import normalise_book_levels, parse_jsonish_list


def make_market(*, liquidity: float = 2000, minutes_to_end: int = 180) -> MarketRecord:
    return MarketRecord(
        market_id="m1",
        event_id="e1",
        question="Test market",
        slug="test-market",
        outcome_labels=["Yes", "No"],
        token_ids=["yes", "no"],
        active=True,
        closed=False,
        liquidity=liquidity,
        end_date=datetime.now(timezone.utc) + timedelta(minutes=minutes_to_end),
    )


def make_book(token_id: str, *, bid: float = 0.48, ask: float = 0.49, size: float = 200) -> OrderBookSnapshot:
    return OrderBookSnapshot(
        token_id=token_id,
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=size)],
        updated_at=datetime.now(timezone.utc),
    )


def test_parse_jsonish_list_handles_stringified_json() -> None:
    assert parse_jsonish_list('["Yes", "No"]') == ["Yes", "No"]


def test_normalise_book_levels_sorts_bids_desc() -> None:
    levels = normalise_book_levels(
        [{"price": "0.4", "size": "10"}, {"price": "0.6", "size": "2"}],
        "bid",
    )
    assert [level.price for level in levels] == [0.6, 0.4]


def test_liquidity_filter_rejects_low_liquidity_market() -> None:
    settings = Settings(MIN_LIQUIDITY=1000)
    liquidity_filter = LiquidityFilter(settings)
    market = make_market(liquidity=100)
    books = {"yes": make_book("yes"), "no": make_book("no")}
    assert liquidity_filter.allow_market(market, books) is False


def test_liquidity_filter_rejects_near_resolution_by_default() -> None:
    settings = Settings(MIN_MINUTES_TO_RESOLUTION=120, ALLOW_NEAR_RESOLUTION=False)
    liquidity_filter = LiquidityFilter(settings)
    market = make_market(minutes_to_end=30)
    books = {"yes": make_book("yes"), "no": make_book("no")}
    assert liquidity_filter.allow_market(market, books) is False


def test_liquidity_filter_relaxed_market_allows_near_resolution_candidate() -> None:
    settings = Settings(
        MIN_MINUTES_TO_RESOLUTION=120,
        CANDIDATE_MIN_MINUTES_TO_RESOLUTION=30,
        ALLOW_NEAR_RESOLUTION=False,
    )
    liquidity_filter = LiquidityFilter(settings)
    market = make_market(minutes_to_end=45)
    books = {"yes": make_book("yes"), "no": make_book("no")}
    assert liquidity_filter.allow_market(market, books) is False
    assert liquidity_filter.allow_market(market, books, relaxed=True) is True


def test_liquidity_filter_marks_candidate_without_alert_eligibility() -> None:
    settings = Settings(MIN_NET_EDGE=0.015, CANDIDATE_MIN_NET_EDGE=-0.0035, MIN_DEPTH=100, CANDIDATE_MIN_DEPTH=10)
    liquidity_filter = LiquidityFilter(settings)
    opportunity = Opportunity(
        opportunity_id="candidate-1",
        strategy_type=StrategyType.BINARY_SUM,
        direction=SignalDirection.BUY_BASKET,
        title="Candidate",
        summary="Near miss",
        market_slugs=["test-market"],
        market_ids=["m1"],
        token_ids=["yes", "no"],
        prices={"yes_ask": 0.50, "no_ask": 0.501},
        gross_edge=-0.001,
        estimated_fees=0.0,
        slippage_estimate=0.002,
        net_edge=-0.003,
        max_safe_size=25,
        available_liquidity=25,
        confidence_score=0.55,
        suggested_action="Watch",
    )
    assert liquidity_filter.annotate_opportunity(opportunity) is True
    assert opportunity.details["qualification_tier"] == "candidate"
    assert opportunity.details["alert_eligible"] is False
