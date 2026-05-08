from __future__ import annotations

from hashlib import md5

from app.config import Settings
from app.models.core import MarketRecord, Opportunity, OrderBookSnapshot, SignalDirection, StrategyType
from app.scanners.liquidity_filter import LiquidityFilter
from app.utils.math_utils import clamp_confidence, estimate_total_cost, utc_now


class MultiOutcomeScanner:
    """Detect underround/overround in markets with 3+ outcomes."""

    def __init__(self, settings: Settings, liquidity_filter: LiquidityFilter) -> None:
        self.settings = settings
        self.liquidity_filter = liquidity_filter

    def scan(self, markets: list[MarketRecord], books: dict[str, OrderBookSnapshot]) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for market in markets:
            if len(market.token_ids) < 3 or not self.liquidity_filter.allow_market(market, books, relaxed=True):
                continue
            snapshots = [books.get(token_id) for token_id in market.token_ids]
            if any(snapshot is None for snapshot in snapshots):
                continue
            orderbooks = [snapshot for snapshot in snapshots if snapshot is not None]
            if any(book.best_ask is None for book in orderbooks):
                continue
            ask_sum = sum(book.best_ask or 0.0 for book in orderbooks)
            cost = estimate_total_cost(len(orderbooks), self.settings.fees_bps, self.settings.slippage_bps)
            gross_edge = 1.0 - ask_sum
            net_edge = gross_edge - cost
            if net_edge > self.settings.candidate_min_net_edge:
                max_size = min(self.liquidity_filter.minimum_depth(book, "ask") for book in orderbooks)
                opportunities.append(
                    Opportunity(
                        opportunity_id=self._make_id(market.slug, "multi_underround"),
                        strategy_type=StrategyType.MULTI_OUTCOME_SUM,
                        direction=SignalDirection.BUY_BASKET,
                        title=f"{market.question} | 多結果總和偏低",
                        summary=f"多結果 ask 合計 {ask_sum:.3f}",
                        market_slugs=[market.slug],
                        market_ids=[market.market_id],
                        token_ids=market.token_ids,
                        prices={f"ask_{idx}": book.best_ask for idx, book in enumerate(orderbooks, start=1)},
                        gross_edge=gross_edge,
                        estimated_fees=len(orderbooks) * (self.settings.fees_bps / 10_000),
                        slippage_estimate=len(orderbooks) * (self.settings.slippage_bps / 10_000),
                        net_edge=net_edge,
                        max_safe_size=max_size,
                        available_liquidity=max_size,
                        confidence_score=clamp_confidence(0.45 + max(net_edge, 0.0) * 8),
                        timestamp=utc_now(),
                        suggested_action="多結果籃子接近失衡，適合先列入備選清單。",
                        link_slugs=[market.slug],
                        details={"locked_profit_per_share": net_edge},
                    )
                )
            if any(book.best_bid is None for book in orderbooks):
                continue
            bid_sum = sum(book.best_bid or 0.0 for book in orderbooks)
            gross_edge = bid_sum - 1.0
            net_edge = gross_edge - cost
            if net_edge > self.settings.candidate_min_net_edge:
                max_size = min(self.liquidity_filter.minimum_depth(book, "bid") for book in orderbooks)
                opportunities.append(
                    Opportunity(
                        opportunity_id=self._make_id(market.slug, "multi_overround"),
                        strategy_type=StrategyType.MULTI_OUTCOME_SUM,
                        direction=SignalDirection.SELL_BASKET,
                        title=f"{market.question} | 多結果總和偏高",
                        summary=f"多結果 bid 合計 {bid_sum:.3f}",
                        market_slugs=[market.slug],
                        market_ids=[market.market_id],
                        token_ids=market.token_ids,
                        prices={f"bid_{idx}": book.best_bid for idx, book in enumerate(orderbooks, start=1)},
                        gross_edge=gross_edge,
                        estimated_fees=len(orderbooks) * (self.settings.fees_bps / 10_000),
                        slippage_estimate=len(orderbooks) * (self.settings.slippage_bps / 10_000),
                        net_edge=net_edge,
                        max_safe_size=max_size,
                        available_liquidity=max_size,
                        confidence_score=clamp_confidence(0.3 + max(net_edge, 0.0) * 5),
                        timestamp=utc_now(),
                        suggested_action="如果能手動管理多腿部位，這筆可以先追蹤。",
                        link_slugs=[market.slug],
                        details={"requires_inventory": True, "locked_profit_per_share": net_edge},
                    )
                )
        return opportunities

    @staticmethod
    def _make_id(slug: str, suffix: str) -> str:
        return md5(f"{slug}:{suffix}".encode("utf-8"), usedforsecurity=False).hexdigest()
