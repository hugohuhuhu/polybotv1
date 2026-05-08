from __future__ import annotations

from hashlib import md5

from app.config import Settings
from app.models.core import MarketRecord, Opportunity, OrderBookSnapshot, SignalDirection, StrategyType
from app.scanners.liquidity_filter import LiquidityFilter
from app.utils.math_utils import clamp_confidence, estimate_total_cost, utc_now


class BinarySumArbScanner:
    """Detect YES/NO underround and overround opportunities."""

    def __init__(self, settings: Settings, liquidity_filter: LiquidityFilter) -> None:
        self.settings = settings
        self.liquidity_filter = liquidity_filter

    def scan(
        self,
        markets: list[MarketRecord],
        books: dict[str, OrderBookSnapshot],
    ) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for market in markets:
            if not market.is_binary or not self.liquidity_filter.allow_market(market, books, relaxed=True):
                continue
            outcome_map = {
                label.lower(): token_id for label, token_id in zip(market.outcome_labels, market.token_ids, strict=False)
            }
            yes_token = outcome_map.get("yes") or market.token_ids[0]
            no_token = outcome_map.get("no") or market.token_ids[1]
            yes_book = books.get(yes_token)
            no_book = books.get(no_token)
            if yes_book is None or no_book is None:
                continue
            opportunities.extend(self._scan_underround(market, yes_book, no_book))
            opportunities.extend(self._scan_overround(market, yes_book, no_book))
        return opportunities

    def _scan_underround(
        self,
        market: MarketRecord,
        yes_book: OrderBookSnapshot,
        no_book: OrderBookSnapshot,
    ) -> list[Opportunity]:
        if yes_book.best_ask is None or no_book.best_ask is None:
            return []
        ask_sum = yes_book.best_ask + no_book.best_ask
        gross_edge = 1.0 - ask_sum
        total_cost = estimate_total_cost(2, self.settings.fees_bps, self.settings.slippage_bps)
        net_edge = gross_edge - total_cost
        if net_edge <= self.settings.candidate_min_net_edge:
            return []
        max_safe_size = min(
            self.liquidity_filter.minimum_depth(yes_book, "ask"),
            self.liquidity_filter.minimum_depth(no_book, "ask"),
        )
        confidence = clamp_confidence((max(net_edge, 0.0) / max(self.settings.min_net_edge, 0.001)) * 0.35 + 0.45)
        opportunity = Opportunity(
            opportunity_id=self._make_id(market.slug, "underround"),
            strategy_type=StrategyType.BINARY_SUM,
            direction=SignalDirection.BUY_BASKET,
            title=f"{market.question} | YES/NO 總和偏低",
            summary=f"YES ask {yes_book.best_ask:.3f} + NO ask {no_book.best_ask:.3f} = {ask_sum:.3f}",
            market_slugs=[market.slug],
            market_ids=[market.market_id],
            token_ids=[yes_book.token_id, no_book.token_id],
            prices={
                "yes_ask": yes_book.best_ask,
                "no_ask": no_book.best_ask,
                "basket_ask_sum": ask_sum,
            },
            gross_edge=gross_edge,
            estimated_fees=2 * (self.settings.fees_bps / 10_000),
            slippage_estimate=2 * (self.settings.slippage_bps / 10_000),
            net_edge=net_edge,
            max_safe_size=max_safe_size,
            available_liquidity=max_safe_size,
            confidence_score=confidence,
            timestamp=utc_now(),
            suggested_action="同步檢查 YES / NO 掛單，若報價再改善一跳就值得盯住。",
            link_slugs=[market.slug],
            details={"locked_profit_per_share": net_edge, "tradable_live": True},
        )
        return [opportunity]

    def _scan_overround(
        self,
        market: MarketRecord,
        yes_book: OrderBookSnapshot,
        no_book: OrderBookSnapshot,
    ) -> list[Opportunity]:
        if yes_book.best_bid is None or no_book.best_bid is None:
            return []
        bid_sum = yes_book.best_bid + no_book.best_bid
        gross_edge = bid_sum - 1.0
        total_cost = estimate_total_cost(2, self.settings.fees_bps, self.settings.slippage_bps)
        net_edge = gross_edge - total_cost
        if net_edge <= self.settings.candidate_min_net_edge:
            return []
        max_safe_size = min(
            self.liquidity_filter.minimum_depth(yes_book, "bid"),
            self.liquidity_filter.minimum_depth(no_book, "bid"),
        )
        confidence = clamp_confidence((max(net_edge, 0.0) / max(self.settings.min_net_edge, 0.001)) * 0.25 + 0.35)
        opportunity = Opportunity(
            opportunity_id=self._make_id(market.slug, "overround"),
            strategy_type=StrategyType.BINARY_SUM,
            direction=SignalDirection.SELL_BASKET,
            title=f"{market.question} | YES/NO 總和偏高",
            summary=f"YES bid {yes_book.best_bid:.3f} + NO bid {no_book.best_bid:.3f} = {bid_sum:.3f}",
            market_slugs=[market.slug],
            market_ids=[market.market_id],
            token_ids=[yes_book.token_id, no_book.token_id],
            prices={
                "yes_bid": yes_book.best_bid,
                "no_bid": no_book.best_bid,
                "basket_bid_sum": bid_sum,
            },
            gross_edge=gross_edge,
            estimated_fees=2 * (self.settings.fees_bps / 10_000),
            slippage_estimate=2 * (self.settings.slippage_bps / 10_000),
            net_edge=net_edge,
            max_safe_size=max_safe_size,
            available_liquidity=max_safe_size,
            confidence_score=confidence,
            timestamp=utc_now(),
            suggested_action="若已有部位或能手動對沖，可把這筆列為高優先備選。",
            link_slugs=[market.slug],
            details={"locked_profit_per_share": net_edge, "requires_inventory": True, "tradable_live": False},
        )
        return [opportunity]

    @staticmethod
    def _make_id(slug: str, suffix: str) -> str:
        return md5(f"{slug}:{suffix}".encode("utf-8"), usedforsecurity=False).hexdigest()
