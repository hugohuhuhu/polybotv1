from __future__ import annotations

from hashlib import md5
from pathlib import Path

import yaml

from app.config import Settings
from app.models.core import MarketRecord, Opportunity, OrderBookSnapshot, RelatedRule, SignalDirection, StrategyType
from app.utils.math_utils import clamp_confidence, estimate_total_cost, utc_now


class RelatedMarketScanner:
    """Rule-based logical inconsistency scanner."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.rules = self._load_rules(settings.related_rules_path)

    def _load_rules(self, path: Path) -> list[RelatedRule]:
        if not path.exists():
            return []
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return [RelatedRule.model_validate(item) for item in payload.get("rules", [])]

    def scan(self, markets: list[MarketRecord], books: dict[str, OrderBookSnapshot]) -> list[Opportunity]:
        market_map = {market.slug: market for market in markets}
        opportunities: list[Opportunity] = []
        for rule in self.rules:
            if rule.kind == "less_than_or_equal" and rule.left and rule.right:
                opportunity = self._scan_lte(rule, market_map, books)
                if opportunity is not None:
                    opportunities.append(opportunity)
            elif rule.kind == "sum_less_than_or_equal" and rule.components and rule.total:
                opportunity = self._scan_sum_lte(rule, market_map, books)
                if opportunity is not None:
                    opportunities.append(opportunity)
        return opportunities

    def _resolve_outcome_book(
        self,
        market_map: dict[str, MarketRecord],
        books: dict[str, OrderBookSnapshot],
        slug: str,
        outcome: str,
    ) -> tuple[MarketRecord, OrderBookSnapshot] | None:
        market = market_map.get(slug)
        if market is None:
            return None
        outcome_lower = outcome.lower()
        token_id: str | None = None
        for label, candidate in zip(market.outcome_labels, market.token_ids, strict=False):
            if label.lower() == outcome_lower:
                token_id = candidate
                break
        if token_id is None and market.token_ids:
            token_id = market.token_ids[0]
        if token_id is None or token_id not in books:
            return None
        return market, books[token_id]

    def _scan_lte(
        self,
        rule: RelatedRule,
        market_map: dict[str, MarketRecord],
        books: dict[str, OrderBookSnapshot],
    ) -> Opportunity | None:
        left = self._resolve_outcome_book(market_map, books, rule.left.slug, rule.left.outcome)
        right = self._resolve_outcome_book(market_map, books, rule.right.slug, rule.right.outcome)
        if left is None or right is None:
            return None
        left_market, left_book = left
        right_market, right_book = right
        if left_book.best_bid is None or right_book.best_ask is None:
            return None
        gross_edge = left_book.best_bid - right_book.best_ask - rule.tolerance
        net_edge = gross_edge - estimate_total_cost(2, self.settings.fees_bps, self.settings.slippage_bps)
        if net_edge <= self.settings.candidate_min_net_edge:
            return None
        max_size = min(left_book.depth_for_side("bid", left_book.best_bid), right_book.depth_for_side("ask", right_book.best_ask))
        return Opportunity(
            opportunity_id=self._make_id(rule.rule_id, "lte"),
            strategy_type=StrategyType.RELATED_RULE,
            direction=SignalDirection.RELATIVE_VALUE,
            title=f"邏輯規則偏離: {rule.description}",
            summary=f"{rule.left.slug}:{rule.left.outcome} bid {left_book.best_bid:.3f} > {rule.right.slug}:{rule.right.outcome} ask {right_book.best_ask:.3f}",
            market_slugs=[left_market.slug, right_market.slug],
            market_ids=[left_market.market_id, right_market.market_id],
            token_ids=[left_book.token_id, right_book.token_id],
            prices={"left_bid": left_book.best_bid, "right_ask": right_book.best_ask},
            gross_edge=gross_edge,
            estimated_fees=2 * (self.settings.fees_bps / 10_000),
            slippage_estimate=2 * (self.settings.slippage_bps / 10_000),
            net_edge=net_edge,
            max_safe_size=max_size,
            available_liquidity=max_size,
            confidence_score=clamp_confidence(0.35 + net_edge * 6),
            timestamp=utc_now(),
            suggested_action="人工檢查兩腿相對價格，若具備庫存或可手動對沖，可執行賣左買右。",
            link_slugs=[left_market.slug, right_market.slug],
            details={"rule_id": rule.rule_id, "requires_inventory": True},
        )

    def _scan_sum_lte(
        self,
        rule: RelatedRule,
        market_map: dict[str, MarketRecord],
        books: dict[str, OrderBookSnapshot],
    ) -> Opportunity | None:
        component_pairs = [
            self._resolve_outcome_book(market_map, books, component.slug, component.outcome)
            for component in rule.components
        ]
        total_pair = self._resolve_outcome_book(market_map, books, rule.total.slug, rule.total.outcome)
        if total_pair is None or any(pair is None for pair in component_pairs):
            return None
        components = [pair for pair in component_pairs if pair is not None]
        total_market, total_book = total_pair
        if total_book.best_ask is None or any(book.best_bid is None for _, book in components):
            return None
        component_bid_sum = sum(book.best_bid or 0.0 for _, book in components)
        gross_edge = component_bid_sum - total_book.best_ask - rule.tolerance
        net_edge = gross_edge - estimate_total_cost(len(components) + 1, self.settings.fees_bps, self.settings.slippage_bps)
        if net_edge <= self.settings.candidate_min_net_edge:
            return None
        max_size = min(
            [total_book.depth_for_side("ask", total_book.best_ask)]
            + [book.depth_for_side("bid", book.best_bid) for _, book in components if book.best_bid is not None]
        )
        return Opportunity(
            opportunity_id=self._make_id(rule.rule_id, "sum_lte"),
            strategy_type=StrategyType.RELATED_RULE,
            direction=SignalDirection.RELATIVE_VALUE,
            title=f"邏輯總和偏離: {rule.description}",
            summary=f"子市場 bid 合計 {component_bid_sum:.3f} > 母市場 ask {total_book.best_ask:.3f}",
            market_slugs=[market.slug for market, _ in components] + [total_market.slug],
            market_ids=[market.market_id for market, _ in components] + [total_market.market_id],
            token_ids=[book.token_id for _, book in components] + [total_book.token_id],
            prices={
                "component_bid_sum": component_bid_sum,
                "total_ask": total_book.best_ask,
            },
            gross_edge=gross_edge,
            estimated_fees=(len(components) + 1) * (self.settings.fees_bps / 10_000),
            slippage_estimate=(len(components) + 1) * (self.settings.slippage_bps / 10_000),
            net_edge=net_edge,
            max_safe_size=max_size,
            available_liquidity=max_size,
            confidence_score=clamp_confidence(0.3 + net_edge * 4),
            timestamp=utc_now(),
            suggested_action="檢查子市場與母市場的價差，必要時人工做相對價值對沖。",
            link_slugs=[market.slug for market, _ in components] + [total_market.slug],
            details={"rule_id": rule.rule_id, "requires_inventory": True},
        )

    @staticmethod
    def _make_id(rule_id: str, suffix: str) -> str:
        return md5(f"{rule_id}:{suffix}".encode("utf-8"), usedforsecurity=False).hexdigest()
