from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import md5

from app.models.core import MarketRecord, Opportunity, OrderBookSnapshot, SignalDirection, StrategyType
from app.utils.math_utils import clamp_confidence


class StalePriceScanner:
    """Detect stale quotes when sibling markets moved but one book stopped updating."""

    def __init__(self, stale_threshold_sec: int = 90, peer_move_threshold: float = 0.03) -> None:
        self.stale_threshold_sec = stale_threshold_sec
        self.peer_move_threshold = peer_move_threshold

    def scan(
        self,
        markets: list[MarketRecord],
        books: dict[str, OrderBookSnapshot],
        previous_midpoints: dict[str, float],
        *,
        now: datetime | None = None,
    ) -> list[Opportunity]:
        current = now or datetime.now(timezone.utc)
        grouped: dict[str, list[MarketRecord]] = defaultdict(list)
        for market in markets:
            if market.event_id:
                grouped[market.event_id].append(market)
        opportunities: list[Opportunity] = []
        for event_markets in grouped.values():
            for market in event_markets:
                if not market.token_ids:
                    continue
                token_id = market.token_ids[0]
                book = books.get(token_id)
                if book is None or book.midpoint is None:
                    continue
                age = (current - book.updated_at).total_seconds()
                if age < self.stale_threshold_sec:
                    continue
                peer_moves: list[float] = []
                for peer_market in event_markets:
                    if peer_market.market_id == market.market_id or not peer_market.token_ids:
                        continue
                    peer_book = books.get(peer_market.token_ids[0])
                    if peer_book is None or peer_book.midpoint is None:
                        continue
                    previous_mid = previous_midpoints.get(peer_book.token_id)
                    if previous_mid is None:
                        continue
                    peer_moves.append(abs(peer_book.midpoint - previous_mid))
                if not peer_moves or max(peer_moves) < self.peer_move_threshold:
                    continue
                opportunities.append(
                    Opportunity(
                        opportunity_id=self._make_id(market.slug),
                        strategy_type=StrategyType.STALE_PRICE,
                        direction=SignalDirection.REVIEW,
                        title=f"疑似陳舊報價: {market.question}",
                        summary=f"報價 {age:.0f} 秒未更新，但相關市場已有明顯變動。",
                        market_slugs=[market.slug],
                        market_ids=[market.market_id],
                        token_ids=[token_id],
                        prices={"midpoint": book.midpoint, "book_age_sec": age},
                        gross_edge=max(peer_moves),
                        estimated_fees=0.0,
                        slippage_estimate=0.0,
                        net_edge=max(peer_moves) / 2,
                        max_safe_size=0.0,
                        available_liquidity=0.0,
                        confidence_score=clamp_confidence(0.2 + max(peer_moves) * 4),
                        suggested_action="列為低信心候選，先人工確認是否為靜態掛單或資料延遲。",
                        link_slugs=[market.slug],
                        details={"peer_moves": peer_moves, "book_age_sec": age, "lower_confidence": True},
                    )
                )
        return opportunities

    @staticmethod
    def _make_id(slug: str) -> str:
        return md5(f"{slug}:stale".encode("utf-8"), usedforsecurity=False).hexdigest()
