from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from math import log1p
import re

from app.clients.clob_client import ClobClient
from app.clients.crypto_price_client import CryptoPriceClient, binance_symbol_for_asset
from app.clients.gamma_client import GammaClient
from app.config import Settings
from app.models.core import EventRecord, MarketRecord, Opportunity, OrderBookSnapshot
from app.scanners.late_resolution_scanner import LateResolutionScanner
from app.scanners.liquidity_filter import LiquidityFilter
from app.scanners.near_close_market_filter import classify_near_close_market
from app.scanners.multi_outcome_scanner import MultiOutcomeScanner
from app.scanners.related_market_scanner import RelatedMarketScanner
from app.scanners.stale_price_scanner import StalePriceScanner
from app.scanners.sum_arb_scanner import BinarySumArbScanner
from app.storage.repositories import ScannerRepository
from app.strategy.opportunity_ranker import OpportunityRanker
from app.utils.time_utils import parse_datetime


@dataclass(slots=True)
class ScanCycleResult:
    events: list[EventRecord]
    markets: list[MarketRecord]
    shortlisted_markets: list[MarketRecord]
    books: dict[str, OrderBookSnapshot]
    opportunities: list[Opportunity]
    executed_at: datetime
    shortlist_diagnostics: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class MarketShortlistProfile:
    market: MarketRecord
    watch_score: float
    family_key: str
    family_size: int
    bucket_candidates: set[str]
    shortlist_reasons: list[str]
    excluded_long_tail: bool = False
    long_tail_exception: bool = False
    assigned_bucket: str | None = None


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(value, upper))


def _market_family_key(market: MarketRecord) -> str:
    return market.event_slug or market.event_id or market.category or market.slug


def _market_spread(market: MarketRecord) -> float | None:
    if market.spread is not None:
        return market.spread
    if market.best_bid is not None and market.best_ask is not None:
        return max(market.best_ask - market.best_bid, 0.0)
    return None


def _binary_price_pair(market: MarketRecord) -> tuple[float, float] | None:
    if not market.is_binary:
        return None
    if len(market.outcome_prices) >= 2 and all(price is not None for price in market.outcome_prices[:2]):
        return float(market.outcome_prices[0]), float(market.outcome_prices[1])
    yes_price = None
    if market.best_bid is not None and market.best_ask is not None:
        yes_price = (market.best_bid + market.best_ask) / 2
    elif market.last_trade_price is not None:
        yes_price = market.last_trade_price
    if yes_price is None:
        return None
    return float(yes_price), float(max(0.0, 1.0 - yes_price))


def _is_late_resolution_candidate(settings: Settings, market: MarketRecord) -> bool:
    if not settings.late_resolution_enabled or not market.is_binary:
        return False
    price_pair = _binary_price_pair(market)
    if price_pair is None:
        return False
    lead_price = max(price_pair)
    spread = _market_spread(market) or 1.0
    if not (settings.late_resolution_min_price <= lead_price <= settings.late_resolution_max_price):
        return False
    if spread > settings.late_resolution_max_spread * 1.5:
        return False
    if market.end_date is None:
        return False
    minutes_to_resolution = (market.end_date - datetime.now(timezone.utc)).total_seconds() / 60
    return minutes_to_resolution <= settings.late_resolution_max_minutes_to_resolution


def _minutes_to_resolution(market: MarketRecord, now: datetime | None = None) -> float | None:
    if market.end_date is None:
        return None
    current = now or datetime.now(timezone.utc)
    end_date = market.end_date if market.end_date.tzinfo else market.end_date.replace(tzinfo=timezone.utc)
    return (end_date - current).total_seconds() / 60


_CRYPTO_STRIKE_PATTERN = re.compile(
    r"\b(?P<asset>bitcoin|btc|ethereum|eth|solana|sol|xrp|dogecoin|doge|bnb)\b.*?"
    r"\b(?P<side>above|below)\b\s+(?P<strike>[0-9][0-9,]*(?:\.[0-9]+)?)",
    re.IGNORECASE,
)
_CRYPTO_UPDOWN_PATTERN = re.compile(
    r"\b(?P<asset>bitcoin|btc|ethereum|eth|solana|sol|xrp|dogecoin|doge|bnb)\b.*?"
    r"\bup\s+or\s+down\b",
    re.IGNORECASE,
)


def _parse_crypto_strike_market(market: MarketRecord) -> tuple[str, str, float] | None:
    text = " ".join(value for value in (market.question, market.event_title or "") if value)
    match = _CRYPTO_STRIKE_PATTERN.search(text)
    if match is None:
        return None
    symbol = binance_symbol_for_asset(match.group("asset"))
    if symbol is None:
        return None
    strike = float(match.group("strike").replace(",", ""))
    return symbol, match.group("side").lower(), strike


def _parse_crypto_updown_market(market: MarketRecord) -> tuple[str, datetime] | None:
    text = " ".join(value for value in (market.question, market.event_title or "") if value)
    match = _CRYPTO_UPDOWN_PATTERN.search(text)
    if match is None:
        return None
    symbol = binance_symbol_for_asset(match.group("asset"))
    if symbol is None:
        return None
    start_time = parse_datetime(market.raw.get("eventStartTime"))
    if start_time is None:
        return None
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    return symbol, start_time


async def enrich_crypto_near_close_markets(settings: Settings, markets: list[MarketRecord]) -> None:
    if not settings.near_close_crypto_enabled:
        return
    parsed_by_market: dict[str, tuple[str, str, float]] = {}
    updown_by_market: dict[str, tuple[str, datetime]] = {}
    symbols: set[str] = set()
    start_price_requests: dict[str, tuple[str, int]] = {}
    for market in markets:
        parsed = _parse_crypto_strike_market(market)
        if parsed is not None:
            symbol, _side, _strike = parsed
            parsed_by_market[market.market_id] = parsed
            symbols.add(symbol)
            continue
        updown = _parse_crypto_updown_market(market)
        if updown is not None:
            symbol, start_time = updown
            updown_by_market[market.market_id] = updown
            symbols.add(symbol)
            if start_time <= datetime.now(timezone.utc):
                start_price_requests[market.market_id] = (symbol, int(start_time.timestamp() * 1000))
    if not symbols:
        return
    client = CryptoPriceClient()
    try:
        prices = await client.get_prices(symbols)
        start_prices = await client.get_open_prices_for_requests(start_price_requests) if start_price_requests else {}
    finally:
        await client.close()
    for market in markets:
        parsed = parsed_by_market.get(market.market_id)
        if parsed is not None:
            symbol, side, strike = parsed
            spot = prices.get(symbol)
            if spot is None or strike <= 0:
                continue
            condition_true = spot > strike if side == "above" else spot < strike
            market.raw["near_close_crypto_variant"] = "fixed_strike"
            market.raw["near_close_crypto_symbol"] = symbol
            market.raw["near_close_crypto_side"] = side
            market.raw["near_close_crypto_spot_price"] = spot
            market.raw["near_close_crypto_strike_price"] = strike
            market.raw["near_close_crypto_strike_distance"] = abs(spot - strike) / strike
            market.raw["near_close_crypto_winning_outcome"] = "Yes" if condition_true else "No"
            continue
        updown = updown_by_market.get(market.market_id)
        if updown is None:
            continue
        symbol, start_time = updown
        spot = prices.get(symbol)
        start_price = start_prices.get(market.market_id)
        if spot is None or start_price is None or start_price <= 0:
            continue
        market.raw["near_close_crypto_variant"] = "updown_proxy"
        market.raw["near_close_crypto_symbol"] = symbol
        market.raw["near_close_crypto_side"] = "updown"
        market.raw["near_close_crypto_spot_price"] = spot
        market.raw["near_close_crypto_start_price"] = start_price
        market.raw["near_close_crypto_start_time"] = start_time.isoformat()
        market.raw["near_close_crypto_start_distance"] = abs(spot - start_price) / start_price
        market.raw["near_close_crypto_winning_outcome"] = "Up" if spot > start_price else "Down"


def _is_near_close_pool_candidate(settings: Settings, market: MarketRecord, now: datetime) -> bool:
    if not settings.near_close_maker_enabled:
        return False
    if not market.active or market.closed:
        return False
    if not market.is_binary or len(market.token_ids) != 2:
        return False
    minutes_left = _minutes_to_resolution(market, now)
    if minutes_left is None:
        return False
    decision = classify_near_close_market(market)
    if not decision.allowed:
        return False
    min_minutes, max_minutes = _near_close_time_window(settings, decision.variant)
    if minutes_left < min_minutes:
        return False
    if minutes_left > min(settings.near_close_scan_lookahead_minutes, max_minutes):
        return False
    if not market.resolution_source:
        return False
    return True


def _near_close_time_window(settings: Settings, variant: str) -> tuple[float, float]:
    if variant == "crypto_updown":
        return (
            settings.near_close_crypto_updown_min_minutes_to_end,
            settings.near_close_crypto_updown_max_minutes_to_end,
        )
    if variant == "crypto":
        return settings.near_close_crypto_min_minutes_to_end, settings.near_close_crypto_max_minutes_to_end
    return settings.near_close_min_minutes_to_end, settings.near_close_max_minutes_to_end


def build_near_close_funnel(
    settings: Settings,
    markets: list[MarketRecord],
    *,
    shortlisted: list[MarketRecord] | None = None,
    books: dict[str, OrderBookSnapshot] | None = None,
    opportunities: list[Opportunity] | None = None,
    now: datetime | None = None,
) -> list[dict[str, object]]:
    current = now or datetime.now(timezone.utc)
    open_markets = [market for market in markets if market.active and not market.closed]
    binary_token_ready = [
        market
        for market in open_markets
        if market.is_binary and len(market.token_ids) == 2
    ]
    restricted_markets = [market for market in binary_token_ready if market.restricted]
    sourced = [
        market
        for market in binary_token_ready
        if market.end_date is not None and bool(market.resolution_source)
    ]
    typed = [market for market in sourced if classify_near_close_market(market).allowed]
    in_window: list[MarketRecord] = []
    for market in typed:
        minutes_left = _minutes_to_resolution(market, current)
        decision = classify_near_close_market(market)
        min_minutes, max_minutes = _near_close_time_window(settings, decision.variant)
        if minutes_left is not None and min_minutes <= minutes_left <= min(settings.near_close_scan_lookahead_minutes, max_minutes):
            in_window.append(market)

    shortlisted_markets = shortlisted if shortlisted is not None else in_window[: settings.near_close_scan_pool_limit]
    book_ready_count = 0
    if books is not None:
        book_ready_count = sum(1 for market in shortlisted_markets if all(token_id in books for token_id in market.token_ids))

    opportunity_count = len(opportunities) if opportunities is not None else 0
    return [
        {
            "label": "\u63a2\u7d22\u5230\u7684\u5e02\u5834",
            "count": len(markets),
            "description": "Gamma discovery \u672c\u8f2a\u56de\u50b3\u7684 open market universe\u3002",
        },
        {
            "label": "Active / \u672a closed",
            "count": len(open_markets),
            "description": "\u5e02\u5834\u4ecd\u5728 Polymarket \u958b\u653e\u72c0\u614b\uff0c\u5c1a\u672a\u95dc\u9589\u3002",
        },
        {
            "label": "\u4e8c\u5143\u4e14 token \u5b8c\u6574",
            "count": len(binary_token_ready),
            "description": "\u53ea\u4fdd\u7559 Yes/No \u4e8c\u5143\u5e02\u5834\uff0c\u4e14\u5169\u500b outcome token \u90fd\u53ef\u8b80\u53d6\u3002",
        },
        {
            "label": "restricted \u6a19\u8a18",
            "count": len(binary_token_ready),
            "description": f"\u4e0d\u518d\u786c\u64cb\uff1b\u5176\u4e2d {len(restricted_markets)} \u500b\u5e02\u5834\u5e36 restricted \u6a19\u8a18\uff0c\u6539\u7531 CLOB / orderbook / post-only \u6aa2\u67e5\u63a7\u7ba1\u3002",
        },
        {
            "label": "\u6709\u7d50\u675f\u6642\u9593\u8207\u4f86\u6e90",
            "count": len(sourced),
            "description": "\u5177\u5099 end date \u8207 resolution source\u3002",
        },
        {
            "label": "\u7b26\u5408\u7b56\u7565\u985e\u578b",
            "count": len(typed),
            "description": "\u5b98\u65b9/\u5ba2\u89c0\u4f86\u6e90\u3001crypto above/below \u56fa\u5b9a strike\u3001\u6216 crypto Up/Down proxy fair value\u3002",
        },
        {
            "label": "\u843d\u5728\u6642\u9593\u7a97",
            "count": len(in_window),
            "description": (
                f"\u5b98\u65b9\u76e4 {settings.near_close_max_minutes_to_end:g}-{settings.near_close_min_minutes_to_end:g} \u5206\u9418\uff1b"
                f"crypto \u56fa\u5b9a\u9580\u6abb {settings.near_close_crypto_max_minutes_to_end:g}-{settings.near_close_crypto_min_minutes_to_end:g} \u5206\u9418\uff1b"
                f"crypto Up/Down {settings.near_close_crypto_updown_max_minutes_to_end:g}-{settings.near_close_crypto_updown_min_minutes_to_end:g} \u5206\u9418\u3002"
            ),
        },
        {
            "label": "\u9032\u5165\u76e3\u770b shortlist",
            "count": len(shortlisted_markets),
            "description": f"\u4f9d\u6d41\u52d5\u6027\u8207\u6642\u9593\u6392\u5e8f\uff0c\u6700\u591a {settings.near_close_scan_pool_limit} \u500b\u5e02\u5834\u3002",
        },
        {
            "label": "Orderbook \u53ef\u8b80\u53d6",
            "count": book_ready_count,
            "description": "shortlist \u5167\u5169\u5074 outcome token \u90fd\u53d6\u5f97 orderbook\u3002",
        },
        {
            "label": "\u901a\u904e\u50f9\u683c/\u6d41\u52d5\u6027",
            "count": opportunity_count,
            "description": "start distance\u3001best ask\u3001midpoint\u3001spread\u3001depth \u8207 post-only entry \u5168\u90e8\u901a\u904e\u3002",
        },
    ]


def shortlist_near_close_markets(
    markets: list[MarketRecord],
    *,
    settings: Settings,
    limit: int | None = None,
) -> tuple[list[MarketRecord], dict[str, object]]:
    now = datetime.now(timezone.utc)
    candidates = [market for market in markets if _is_near_close_pool_candidate(settings, market, now)]
    selected = sorted(
        candidates,
        key=lambda market: (
            0
            if (_minutes_to_resolution(market, now) or 999) <= settings.near_close_max_minutes_to_end
            else 1,
            abs((_minutes_to_resolution(market, now) or 999) - settings.near_close_max_minutes_to_end),
            -float(market.liquidity or 0.0),
            -float(market.volume or 0.0),
        ),
    )[: limit or settings.near_close_scan_pool_limit]

    def shortlisted_entry(market: MarketRecord) -> dict[str, object]:
        decision = classify_near_close_market(market)
        reasons = ["near_close_pool", "binary", "resolution_source", decision.reason]
        if market.restricted:
            reasons.append("restricted_market")
        return {
            "question": market.question,
            "slug": market.slug,
            "liquidity": market.liquidity,
            "watch_score": round(max(0.0, 1.0 - abs((_minutes_to_resolution(market, now) or 999) - 6.0) / 20.0), 4),
            "bucket": "near_close",
            "family_key": _market_family_key(market),
            "reasons": reasons,
            "minutes_to_resolution": round(_minutes_to_resolution(market, now) or 0.0, 1),
            "discovered_at": now.isoformat(),
        }

    shortlisted_entries = [
        shortlisted_entry(market)
        for market in selected
    ]
    diagnostics = {
        "watch_bucket_counts": {"near_close": len(selected)},
        "shortlist_reason_counts": {"near_close_pool": len(selected)},
        "excluded_long_tail_count": 0,
        "excluded_family_cap_count": 0,
        "shortlisted_markets": shortlisted_entries,
        "shortlist_mode": "near_close",
        "near_close_candidates": len(candidates),
        "near_close_funnel": build_near_close_funnel(settings, markets, shortlisted=selected, now=now),
    }
    return selected, diagnostics


def _historical_positive_edge_score(market: MarketRecord, positive_edge_hits: dict[str, int]) -> float:
    hits = positive_edge_hits.get(market.slug, 0)
    return _clamp(hits / 3.0)


def _build_shortlist_profiles(
    settings: Settings,
    markets: list[MarketRecord],
    *,
    positive_edge_hits: dict[str, int] | None = None,
) -> tuple[list[MarketShortlistProfile], dict[str, int]]:
    positive_edge_hits = positive_edge_hits or {}
    family_sizes = Counter(_market_family_key(market) for market in markets)
    profiles: list[MarketShortlistProfile] = []
    diagnostics = {"excluded_long_tail_count": 0}

    for market in markets:
        if not market.active or market.closed:
            continue
        family_key = _market_family_key(market)
        family_size = family_sizes[family_key]
        spread = _market_spread(market)
        liquidity = float(market.liquidity or 0.0)
        volume = float(market.volume or 0.0)
        price_pair = _binary_price_pair(market)
        spread_score = _clamp(1.0 - ((spread or settings.candidate_max_spread) / max(settings.candidate_max_spread, 1e-6)))
        liquidity_score = _clamp(log1p(liquidity) / log1p(max(settings.min_liquidity * 20.0, 10.0)))
        volume_score = _clamp(log1p(volume) / log1p(50_000.0))
        activity_score = (
            (1.0 if market.best_bid is not None else 0.0)
            + (1.0 if market.best_ask is not None else 0.0)
            + (1.0 if market.last_trade_price is not None else 0.0)
        ) / 3.0
        cluster_score = _clamp((family_size - 1) / 4.0)
        positive_edge_score = _historical_positive_edge_score(market, positive_edge_hits)

        centrality_score = 0.3
        excluded_long_tail = False
        long_tail_exception = False
        shortlist_reasons: list[str] = []
        if price_pair is not None:
            min_price = min(price_pair)
            max_price = max(price_pair)
            centrality_score = _clamp(1.0 - (abs(price_pair[0] - 0.5) / 0.5))
            if min_price < settings.watch_long_tail_min_price or max_price > settings.watch_long_tail_max_price:
                long_tail_exception = (
                    (spread or 1.0) <= settings.watch_long_tail_exception_spread
                    and liquidity >= settings.watch_long_tail_exception_liquidity
                    and activity_score >= 0.66
                )
                if long_tail_exception:
                    shortlist_reasons.append("long_tail_exception")
                    centrality_score *= 0.5
                else:
                    excluded_long_tail = True
                    diagnostics["excluded_long_tail_count"] += 1
                    shortlist_reasons.append("excluded_long_tail")

        watch_score = (
            0.30 * liquidity_score
            + 0.20 * spread_score
            + 0.15 * centrality_score
            + 0.15 * activity_score
            + 0.10 * positive_edge_score
            + 0.05 * cluster_score
            + 0.05 * volume_score
        )
        if family_size > 1:
            shortlist_reasons.append("event_cluster")
        if market.restricted:
            shortlist_reasons.append("restricted_market")
        if positive_edge_score > 0:
            shortlist_reasons.append("recent_positive_edge")
        if spread_score >= 0.6:
            shortlist_reasons.append("tight_spread")
        if activity_score >= 0.66:
            shortlist_reasons.append("recent_activity")

        bucket_candidates: set[str] = set()
        if market.is_binary and not excluded_long_tail:
            bucket_candidates.add("general")
        if family_size > 1:
            bucket_candidates.add("event_cluster")
        if activity_score >= 0.66 and spread_score >= 0.4 and not excluded_long_tail:
            bucket_candidates.add("recent_active")
        if not market.is_binary or _is_late_resolution_candidate(settings, market):
            bucket_candidates.add("special")
            shortlist_reasons.append("special_strategy")

        if market.is_binary and price_pair is not None and centrality_score >= 0.7:
            shortlist_reasons.append("central_price_zone")

        profiles.append(
            MarketShortlistProfile(
                market=market,
                watch_score=watch_score,
                family_key=family_key,
                family_size=family_size,
                bucket_candidates=bucket_candidates,
                shortlist_reasons=sorted(set(shortlist_reasons)),
                excluded_long_tail=excluded_long_tail,
                long_tail_exception=long_tail_exception,
            )
        )
    return profiles, diagnostics


def _pick_bucket(
    candidates: list[MarketShortlistProfile],
    *,
    bucket_name: str,
    limit: int,
    selected_market_ids: set[str],
    family_counts: dict[str, int],
    family_cap: int,
) -> tuple[list[MarketShortlistProfile], int]:
    picked: list[MarketShortlistProfile] = []
    excluded_family_cap = 0
    for profile in sorted(candidates, key=lambda item: item.watch_score, reverse=True):
        if len(picked) >= limit:
            break
        if profile.market.market_id in selected_market_ids:
            continue
        if bucket_name not in profile.bucket_candidates:
            continue
        if family_counts.get(profile.family_key, 0) >= family_cap:
            excluded_family_cap += 1
            continue
        picked.append(profile)
        profile.assigned_bucket = bucket_name
        selected_market_ids.add(profile.market.market_id)
        family_counts[profile.family_key] = family_counts.get(profile.family_key, 0) + 1
    return picked, excluded_family_cap


async def discover_markets(
    settings: Settings,
    limit: int | None = None,
    *,
    include_near_close_window: bool = False,
) -> tuple[list[EventRecord], list[MarketRecord]]:
    gamma = GammaClient(settings.gamma_base_url)
    try:
        events, markets = await gamma.discover_active_markets(limit=limit or settings.discovery_event_limit)
        if include_near_close_window:
            now = datetime.now(timezone.utc)
            near_events, near_markets = await gamma.discover_markets_by_end_date(
                end_date_min=now,
                end_date_max=now + timedelta(minutes=settings.near_close_scan_lookahead_minutes),
                limit=settings.near_close_scan_event_limit,
            )
            events_by_id = {event.event_id: event for event in events}
            for event in near_events:
                events_by_id.setdefault(event.event_id, event)
            markets_by_id = {market.market_id: market for market in markets}
            for market in near_markets:
                markets_by_id.setdefault(market.market_id, market)
            return list(events_by_id.values()), list(markets_by_id.values())
        return events, markets
    finally:
        await gamma.close()


def shortlist_markets(
    markets: list[MarketRecord],
    limit: int,
    *,
    settings: Settings | None = None,
    positive_edge_hits: dict[str, int] | None = None,
) -> tuple[list[MarketRecord], dict[str, object]]:
    current_settings = settings or Settings()
    profiles, base_diagnostics = _build_shortlist_profiles(
        current_settings,
        markets,
        positive_edge_hits=positive_edge_hits,
    )
    selected_market_ids: set[str] = set()
    family_counts: dict[str, int] = {}
    selected_profiles: list[MarketShortlistProfile] = []
    bucket_counts: dict[str, int] = {}
    excluded_family_cap_count = 0

    bucket_limits = [
        ("general", current_settings.watch_bucket_general_limit),
        ("event_cluster", current_settings.watch_bucket_event_limit),
        ("recent_active", current_settings.watch_bucket_recent_limit),
        ("special", current_settings.watch_bucket_special_limit),
    ]
    for bucket_name, bucket_limit in bucket_limits:
        picked, bucket_excluded = _pick_bucket(
            profiles,
            bucket_name=bucket_name,
            limit=bucket_limit,
            selected_market_ids=selected_market_ids,
            family_counts=family_counts,
            family_cap=current_settings.watch_event_family_cap,
        )
        selected_profiles.extend(picked)
        bucket_counts[bucket_name] = len(picked)
        excluded_family_cap_count += bucket_excluded

    if len(selected_profiles) < limit:
        fallback_candidates = [profile for profile in sorted(profiles, key=lambda item: item.watch_score, reverse=True)]
        for profile in fallback_candidates:
            if len(selected_profiles) >= limit:
                break
            if profile.market.market_id in selected_market_ids:
                continue
            if profile.excluded_long_tail:
                continue
            if family_counts.get(profile.family_key, 0) >= current_settings.watch_event_family_cap:
                excluded_family_cap_count += 1
                continue
            selected_profiles.append(profile)
            profile.assigned_bucket = "fallback"
            selected_market_ids.add(profile.market.market_id)
            family_counts[profile.family_key] = family_counts.get(profile.family_key, 0) + 1
            bucket_counts["fallback"] = bucket_counts.get("fallback", 0) + 1

    selected_profiles = selected_profiles[:limit]
    shortlist_reason_counts = Counter(
        reason
        for profile in selected_profiles
        for reason in profile.shortlist_reasons
        if not reason.startswith("excluded_")
    )
    shortlisted_entries = [
        {
            "question": profile.market.question,
            "slug": profile.market.slug,
            "liquidity": profile.market.liquidity,
            "watch_score": round(profile.watch_score, 4),
            "bucket": profile.assigned_bucket or "fallback",
            "family_key": profile.family_key,
            "reasons": profile.shortlist_reasons,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
        }
        for profile in selected_profiles
    ]
    diagnostics = {
        "watch_bucket_counts": dict(bucket_counts),
        "shortlist_reason_counts": dict(shortlist_reason_counts),
        "excluded_long_tail_count": base_diagnostics["excluded_long_tail_count"],
        "excluded_family_cap_count": excluded_family_cap_count,
        "shortlisted_markets": shortlisted_entries,
    }
    return [profile.market for profile in selected_profiles], diagnostics


async def fetch_books(settings: Settings, markets: list[MarketRecord]) -> dict[str, OrderBookSnapshot]:
    token_ids = [token_id for market in markets for token_id in market.token_ids]
    clob = ClobClient(settings.clob_base_url, concurrency=settings.book_fetch_concurrency)
    try:
        return await clob.get_order_books(token_ids)
    finally:
        await clob.close()


def collect_previous_midpoints(books: dict[str, OrderBookSnapshot]) -> dict[str, float]:
    previous: dict[str, float] = {}
    for token_id, snapshot in books.items():
        if snapshot.midpoint is not None:
            previous[token_id] = snapshot.midpoint
    return previous


def run_scanners(
    settings: Settings,
    markets: list[MarketRecord],
    books: dict[str, OrderBookSnapshot],
    previous_midpoints: dict[str, float] | None = None,
) -> list[Opportunity]:
    liquidity_filter = LiquidityFilter(settings)
    scanners = []
    if settings.strategy_binary_sum_enabled:
        scanners.append(BinarySumArbScanner(settings, liquidity_filter))
    if settings.strategy_multi_outcome_enabled:
        scanners.append(MultiOutcomeScanner(settings, liquidity_filter))
    if settings.strategy_related_market_enabled:
        scanners.append(RelatedMarketScanner(settings))
    if settings.late_resolution_enabled and settings.near_close_maker_enabled:
        scanners.append(LateResolutionScanner(settings, liquidity_filter))
    opportunities: list[Opportunity] = []
    for scanner in scanners:
        opportunities.extend(scanner.scan(markets, books))
    if previous_midpoints and settings.strategy_stale_price_enabled:
        stale_scanner = StalePriceScanner()
        opportunities.extend(stale_scanner.scan(markets, books, previous_midpoints))
    filtered = [opportunity for opportunity in opportunities if liquidity_filter.annotate_opportunity(opportunity)]
    return OpportunityRanker().rank(filtered)


async def execute_scan_cycle(
    settings: Settings,
    *,
    limit: int | None = None,
    previous_midpoints: dict[str, float] | None = None,
    repository: ScannerRepository | None = None,
) -> ScanCycleResult:
    discovery_limit = limit
    if settings.near_close_scan_pool_enabled and settings.near_close_maker_enabled:
        discovery_limit = max(limit or 0, settings.near_close_scan_event_limit)
    events, markets = await discover_markets(
        settings,
        discovery_limit,
        include_near_close_window=bool(settings.near_close_scan_pool_enabled and settings.near_close_maker_enabled),
    )
    await enrich_crypto_near_close_markets(settings, markets)
    positive_edge_hits = (
        repository.recent_positive_edge_by_slug(hours=settings.watch_positive_edge_lookback_hours)
        if repository is not None
        else {}
    )
    if settings.near_close_scan_pool_enabled and settings.near_close_maker_enabled:
        shortlisted, shortlist_diagnostics = shortlist_near_close_markets(markets, settings=settings)
    else:
        shortlisted, shortlist_diagnostics = shortlist_markets(
            markets,
            settings.watch_market_limit,
            settings=settings,
            positive_edge_hits=positive_edge_hits,
        )
    shortlist_diagnostics["positive_edge_candidates_24h"] = (
        repository.positive_edge_candidates_24h() if repository is not None else sum(positive_edge_hits.values())
    )
    books = await fetch_books(settings, shortlisted)
    opportunities = run_scanners(settings, shortlisted, books, previous_midpoints)
    if settings.near_close_scan_pool_enabled and settings.near_close_maker_enabled:
        shortlist_diagnostics["near_close_funnel"] = build_near_close_funnel(
            settings,
            markets,
            shortlisted=shortlisted,
            books=books,
            opportunities=opportunities,
        )
    return ScanCycleResult(
        events=events,
        markets=markets,
        shortlisted_markets=shortlisted,
        books=books,
        opportunities=opportunities,
        executed_at=datetime.now(timezone.utc),
        shortlist_diagnostics=shortlist_diagnostics,
    )


async def execute_monitor_cycle(
    settings: Settings,
    shortlisted_markets: list[MarketRecord],
    *,
    previous_midpoints: dict[str, float] | None = None,
    shortlist_diagnostics: dict[str, object] | None = None,
) -> ScanCycleResult:
    books = await fetch_books(settings, shortlisted_markets)
    opportunities = run_scanners(settings, shortlisted_markets, books, previous_midpoints)
    diagnostics = dict(shortlist_diagnostics or {})
    funnel = diagnostics.get("near_close_funnel")
    if isinstance(funnel, list):
        updated_funnel = [dict(stage) for stage in funnel if isinstance(stage, dict)]
        token_pairs = {tuple(market.token_ids) for market in shortlisted_markets if len(market.token_ids) == 2}
        readable_books = sum(1 for token_pair in token_pairs if token_pair[0] in books and token_pair[1] in books)
        if len(updated_funnel) >= 2:
            updated_funnel[-2]["count"] = readable_books
            updated_funnel[-1]["count"] = len(opportunities)
        diagnostics["near_close_funnel"] = updated_funnel
    return ScanCycleResult(
        events=[],
        markets=[],
        shortlisted_markets=shortlisted_markets,
        books=books,
        opportunities=opportunities,
        executed_at=datetime.now(timezone.utc),
        shortlist_diagnostics=diagnostics,
    )


def _opportunity_tier_counts(opportunities: list[Opportunity]) -> tuple[int, int]:
    actionable_count = 0
    candidate_count = 0
    for opportunity in opportunities:
        tier = opportunity.details.get("qualification_tier", "actionable")
        if tier == "candidate":
            candidate_count += 1
        else:
            actionable_count += 1
    return actionable_count, candidate_count


def persist_scan_cycle(repository: ScannerRepository, result: ScanCycleResult) -> None:
    actionable_count, candidate_count = _opportunity_tier_counts(result.opportunities)
    repository.save_markets(result.events, result.markets)
    repository.save_orderbooks(result.books.values())
    repository.save_opportunities(result.opportunities)
    repository.save_scan_cycle(
        executed_at=result.executed_at,
        discovered_market_count=len(result.markets),
        monitored_market_count=len(result.shortlisted_markets),
        book_count=len(result.books),
        opportunity_count=len(result.opportunities),
        actionable_count=actionable_count,
        candidate_count=candidate_count,
        watch_bucket_counts=result.shortlist_diagnostics.get("watch_bucket_counts", {}),
        shortlist_reason_counts=result.shortlist_diagnostics.get("shortlist_reason_counts", {}),
        shortlisted_markets=result.shortlist_diagnostics.get("shortlisted_markets", []),
        excluded_long_tail_count=int(result.shortlist_diagnostics.get("excluded_long_tail_count", 0)),
        excluded_family_cap_count=int(result.shortlist_diagnostics.get("excluded_family_cap_count", 0)),
        positive_edge_candidates_24h=int(result.shortlist_diagnostics.get("positive_edge_candidates_24h", 0)),
        near_close_funnel=result.shortlist_diagnostics.get("near_close_funnel", []),
    )


def persist_monitor_cycle(
    repository: ScannerRepository,
    result: ScanCycleResult,
    *,
    discovered_market_count: int,
) -> None:
    actionable_count, candidate_count = _opportunity_tier_counts(result.opportunities)
    repository.save_orderbooks(result.books.values())
    repository.save_opportunities(result.opportunities)
    repository.save_scan_cycle(
        executed_at=result.executed_at,
        discovered_market_count=discovered_market_count,
        monitored_market_count=len(result.shortlisted_markets),
        book_count=len(result.books),
        opportunity_count=len(result.opportunities),
        actionable_count=actionable_count,
        candidate_count=candidate_count,
        watch_bucket_counts=result.shortlist_diagnostics.get("watch_bucket_counts", {}),
        shortlist_reason_counts=result.shortlist_diagnostics.get("shortlist_reason_counts", {}),
        shortlisted_markets=result.shortlist_diagnostics.get("shortlisted_markets", []),
        excluded_long_tail_count=int(result.shortlist_diagnostics.get("excluded_long_tail_count", 0)),
        excluded_family_cap_count=int(result.shortlist_diagnostics.get("excluded_family_cap_count", 0)),
        positive_edge_candidates_24h=int(result.shortlist_diagnostics.get("positive_edge_candidates_24h", 0)),
        near_close_funnel=result.shortlist_diagnostics.get("near_close_funnel", []),
    )
