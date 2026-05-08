from __future__ import annotations

import math

from app.models.core import Opportunity


class OpportunityRanker:
    """Rank opportunities by practical execution value."""

    def rank(self, opportunities: list[Opportunity]) -> list[Opportunity]:
        ranked = sorted(opportunities, key=self.score, reverse=True)
        for index, opportunity in enumerate(ranked, start=1):
            opportunity.details["rank"] = index
            opportunity.details["ranking_score"] = round(self.score(opportunity), 4)
        return ranked

    def score(self, opportunity: Opportunity) -> float:
        liquidity_score = math.log1p(max(opportunity.available_liquidity, 0.0))
        return (
            opportunity.net_edge * 100
            + opportunity.confidence_score * 10
            + liquidity_score
            + min(opportunity.max_safe_size, 5000) / 5000
        )
