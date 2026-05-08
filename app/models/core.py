from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field


class StrategyType(str, Enum):
    BINARY_SUM = "binary_sum"
    MULTI_OUTCOME_SUM = "multi_outcome_sum"
    RELATED_RULE = "related_rule"
    STALE_PRICE = "stale_price"
    LATE_RESOLUTION = "late_resolution"


class SignalDirection(str, Enum):
    BUY_BASKET = "buy_basket"
    SELL_BASKET = "sell_basket"
    RELATIVE_VALUE = "relative_value"
    REVIEW = "review"


class BookLevel(BaseModel):
    price: float
    size: float


class OutcomeRef(BaseModel):
    label: str
    token_id: str
    price: float | None = None


class EventRecord(BaseModel):
    event_id: str
    slug: str | None = None
    title: str
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    active: bool = True
    closed: bool = False
    start_date: datetime | None = None
    end_date: datetime | None = None
    liquidity: float | None = None
    volume: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class MarketRecord(BaseModel):
    market_id: str
    event_id: str | None = None
    question: str
    slug: str
    condition_id: str | None = None
    resolution_source: str | None = None
    end_date: datetime | None = None
    start_date: datetime | None = None
    outcome_labels: list[str] = Field(default_factory=list)
    outcome_prices: list[float] = Field(default_factory=list)
    token_ids: list[str] = Field(default_factory=list)
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    active: bool = True
    closed: bool = False
    restricted: bool = False
    liquidity: float | None = None
    volume: float | None = None
    spread: float | None = None
    best_bid: float | None = None
    best_ask: float | None = None
    last_trade_price: float | None = None
    event_title: str | None = None
    event_slug: str | None = None
    fees_enabled: bool = False
    raw: dict[str, Any] = Field(default_factory=dict)

    @computed_field
    @property
    def is_binary(self) -> bool:
        return len(self.outcome_labels) == 2 and len(self.token_ids) == 2

    @computed_field
    @property
    def outcome_refs(self) -> list[OutcomeRef]:
        refs: list[OutcomeRef] = []
        for idx, label in enumerate(self.outcome_labels):
            token_id = self.token_ids[idx] if idx < len(self.token_ids) else ""
            price = self.outcome_prices[idx] if idx < len(self.outcome_prices) else None
            refs.append(OutcomeRef(label=label, token_id=token_id, price=price))
        return refs


class OrderBookSnapshot(BaseModel):
    token_id: str
    market_id: str | None = None
    bids: list[BookLevel] = Field(default_factory=list)
    asks: list[BookLevel] = Field(default_factory=list)
    last_trade_price: float | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "rest"

    @computed_field
    @property
    def best_bid(self) -> float | None:
        return max((level.price for level in self.bids), default=None)

    @computed_field
    @property
    def best_ask(self) -> float | None:
        return min((level.price for level in self.asks), default=None)

    @computed_field
    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return max(self.best_ask - self.best_bid, 0.0)

    @computed_field
    @property
    def midpoint(self) -> float | None:
        if self.best_bid is None and self.best_ask is None:
            return self.last_trade_price
        if self.best_bid is None:
            return self.best_ask
        if self.best_ask is None:
            return self.best_bid
        return (self.best_bid + self.best_ask) / 2

    def depth_for_side(self, side: str, limit_price: float | None = None) -> float:
        levels = self.asks if side.lower() == "ask" else self.bids
        if limit_price is None:
            return sum(level.size for level in levels)
        if side.lower() == "ask":
            return sum(level.size for level in levels if level.price <= limit_price)
        return sum(level.size for level in levels if level.price >= limit_price)


class Opportunity(BaseModel):
    opportunity_id: str
    strategy_type: StrategyType
    direction: SignalDirection
    title: str
    summary: str
    market_slugs: list[str]
    market_ids: list[str]
    token_ids: list[str]
    prices: dict[str, float | None]
    gross_edge: float
    estimated_fees: float
    slippage_estimate: float
    net_edge: float
    max_safe_size: float
    available_liquidity: float
    confidence_score: float
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    suggested_action: str
    link_slugs: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class ExecutionLeg(BaseModel):
    action: str
    token_id: str
    market_slug: str
    outcome_label: str
    target_price: float
    size: float
    order_type: str | None = None
    post_only: bool = False
    expiration_sec: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExecutionPlan(BaseModel):
    opportunity_id: str
    summary: str
    legs: list[ExecutionLeg]
    max_slippage_bps: float
    cancel_conditions: list[str]
    requires_manual_approval: bool = True
    live_trading_allowed: bool = False
    strategy_type: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaperTradeResult(BaseModel):
    opportunity_id: str
    filled: bool
    average_entry_price: float | None = None
    filled_size: float = 0.0
    gross_notional: float = 0.0
    estimated_fees_paid: float = 0.0
    expected_pnl: float | None = None
    notes: str = ""


class LiveExecutionLegResult(BaseModel):
    leg_index: int
    action: str
    token_id: str
    market_slug: str
    outcome_label: str
    target_price: float
    requested_size: float
    order_id: str | None = None
    status: str
    response: dict[str, Any] = Field(default_factory=dict)


class LiveExecutionResult(BaseModel):
    opportunity_id: str
    status: str
    message: str
    order_type: str
    leg_results: list[LiveExecutionLegResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RelatedRuleLeg(BaseModel):
    slug: str
    outcome: str = "Yes"


class RelatedRule(BaseModel):
    rule_id: str
    description: str
    kind: str
    left: RelatedRuleLeg | None = None
    right: RelatedRuleLeg | None = None
    components: list[RelatedRuleLeg] = Field(default_factory=list)
    total: RelatedRuleLeg | None = None
    tolerance: float = 0.0
