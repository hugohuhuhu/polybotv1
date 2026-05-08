from __future__ import annotations

from dataclasses import dataclass

from app.models.core import MarketRecord


@dataclass(frozen=True)
class NearCloseMarketDecision:
    allowed: bool
    reason: str
    variant: str = "none"


_BLOCKED_KEYWORDS = (
    "court",
    "lawsuit",
    "legal",
    "approval",
    "approved",
    "regulation",
    "judge",
    "sentenced",
    "indicted",
    "resign",
    "appeal",
    "media",
    "celebrity",
    "tweet",
    "post on x",
    "nba",
    "nfl",
    "mlb",
    "nhl",
    "soccer",
    "football",
    "tennis",
    "ufc",
    "lol",
    "league of legends",
    "valorant",
    "dota",
    "cs2",
    "game",
    "match",
    "score",
    "spread",
)

_CRYPTO_PRICE_KEYWORDS = (
    "bitcoin",
    "btc",
    "ethereum",
    "eth",
    "solana",
    "sol",
    "xrp",
    "dogecoin",
    "doge",
    "bnb",
)

_OFFICIAL_DATA_KEYWORDS = (
    "cpi",
    "inflation",
    "pce",
    "fed",
    "fomc",
    "interest rate",
    "rate decision",
    "jobs",
    "unemployment",
    "payroll",
    "nonfarm",
    "gdp",
    "retail sales",
    "treasury",
    "auction",
    "economic",
    "data",
    "release",
    "released",
    "published",
    "report",
)

_RELIABLE_SOURCE_KEYWORDS = (
    "official",
    "unambiguous data source",
    "data source",
    "government",
    "federal",
    "bureau",
    "treasury",
    "federal reserve",
    "bls",
    "bea",
    "census",
    "eia",
    "sec",
    "nasdaq",
    "nyse",
)

_OBJECTIVE_EVENT_KEYWORDS = (
    "announced",
    "confirmed",
    "declared",
    "certified",
    "final",
    "officially",
    "happen",
    "happened",
    "above",
    "below",
    "greater than",
    "less than",
)


def near_close_market_text(market: MarketRecord) -> str:
    values = (
        market.question,
        market.event_title,
        market.event_slug,
        market.category,
        market.resolution_source,
        " ".join(market.tags),
    )
    return " ".join(str(value).lower() for value in values if value)


def classify_near_close_market(market: MarketRecord) -> NearCloseMarketDecision:
    """Allow only objective official-data or already-happened confirmation markets."""

    text = near_close_market_text(market)
    blocked = next((keyword for keyword in _BLOCKED_KEYWORDS if keyword in text), None)
    if blocked:
        return NearCloseMarketDecision(False, f"blocked_keyword:{blocked}")

    if any(keyword in text for keyword in _CRYPTO_PRICE_KEYWORDS):
        crypto_variant = market.raw.get("near_close_crypto_variant")
        if crypto_variant == "updown_proxy":
            distance = market.raw.get("near_close_crypto_start_distance")
            winning_outcome = market.raw.get("near_close_crypto_winning_outcome")
            if distance is None or winning_outcome not in {"Up", "Down"}:
                return NearCloseMarketDecision(False, "crypto_updown_missing_proxy_price")
            if float(distance) < 0.0025:
                return NearCloseMarketDecision(False, "crypto_updown_too_close")
            return NearCloseMarketDecision(True, "crypto_updown_proxy_far_from_start", "crypto_updown")

        distance = market.raw.get("near_close_crypto_strike_distance")
        winning_outcome = market.raw.get("near_close_crypto_winning_outcome")
        if distance is None or winning_outcome not in {"Yes", "No"}:
            return NearCloseMarketDecision(False, "crypto_missing_spot_or_strike")
        if float(distance) < 0.02:
            return NearCloseMarketDecision(False, "crypto_strike_too_close")
        return NearCloseMarketDecision(True, "crypto_far_from_strike", "crypto")

    has_reliable_source = any(keyword in text for keyword in _RELIABLE_SOURCE_KEYWORDS)
    has_official_data_shape = any(keyword in text for keyword in _OFFICIAL_DATA_KEYWORDS)
    has_objective_event_shape = any(keyword in text for keyword in _OBJECTIVE_EVENT_KEYWORDS)

    if has_reliable_source and has_official_data_shape:
        return NearCloseMarketDecision(True, "official_data", "official")
    if has_reliable_source and has_objective_event_shape:
        return NearCloseMarketDecision(True, "official_event_confirmation", "official")
    if "unambiguous data source" in text:
        return NearCloseMarketDecision(True, "unambiguous_data_source", "official")

    return NearCloseMarketDecision(False, "missing_official_objective_source")
