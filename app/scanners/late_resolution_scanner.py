from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR
from hashlib import md5

from app.config import Settings
from app.models.core import MarketRecord, Opportunity, OrderBookSnapshot, SignalDirection, StrategyType
from app.scanners.liquidity_filter import LiquidityFilter
from app.scanners.near_close_market_filter import classify_near_close_market
from app.utils.math_utils import clamp_confidence, utc_now
from app.utils.time_utils import minutes_to


class LateResolutionScanner:
    """Detect conservative near-close maker bids on highly likely outcomes."""

    def __init__(self, settings: Settings, liquidity_filter: LiquidityFilter) -> None:
        self.settings = settings
        self.liquidity_filter = liquidity_filter

    def scan(
        self,
        markets: list[MarketRecord],
        books: dict[str, OrderBookSnapshot],
    ) -> list[Opportunity]:
        if not self.settings.near_close_maker_enabled:
            return []
        opportunities: list[Opportunity] = []
        for market in markets:
            if not self._allow_market(market, books):
                continue
            minutes_left = minutes_to(market.end_date)
            if minutes_left is None:
                continue
            for index, outcome in enumerate(market.outcome_refs):
                book = books.get(outcome.token_id)
                if book is None:
                    continue
                opportunity = self._scan_outcome(
                    market=market,
                    book=book,
                    outcome_label=outcome.label or f"Outcome {index + 1}",
                    minutes_left=minutes_left,
                )
                if opportunity is not None:
                    opportunities.append(opportunity)
        return opportunities

    def _allow_market(self, market: MarketRecord, books: dict[str, OrderBookSnapshot]) -> bool:
        gate_reason = self.liquidity_filter.market_gate_reason(market, books, relaxed=True)
        if gate_reason not in {None, "near_resolution"}:
            return False
        if not market.is_binary or len(market.token_ids) != 2:
            return False
        minutes_left = minutes_to(market.end_date)
        if minutes_left is None or minutes_left <= 0:
            return False
        decision = classify_near_close_market(market)
        if not decision.allowed:
            return False
        min_minutes, max_minutes = self._time_window(decision.variant)
        if minutes_left < min_minutes:
            return False
        if minutes_left > max_minutes:
            return False
        if not market.resolution_source:
            return False
        return True

    def _scan_outcome(
        self,
        *,
        market: MarketRecord,
        book: OrderBookSnapshot,
        outcome_label: str,
        minutes_left: float,
    ) -> Opportunity | None:
        best_ask = book.best_ask
        best_bid = book.best_bid
        midpoint = book.midpoint
        spread = book.spread
        decision = classify_near_close_market(market)
        if decision.variant == "crypto_updown":
            if not (self.settings.near_close_crypto_enabled and self.settings.near_close_crypto_updown_enabled):
                return None
            winning_outcome = str(market.raw.get("near_close_crypto_winning_outcome") or "")
            if outcome_label.lower() != winning_outcome.lower():
                return None
            if (
                float(market.raw.get("near_close_crypto_start_distance") or 0.0)
                < self.settings.near_close_crypto_updown_min_start_distance
            ):
                return None
            min_best_ask = self.settings.near_close_crypto_updown_min_best_ask
            min_midpoint = self.settings.near_close_crypto_updown_min_midpoint
            max_spread = self.settings.near_close_crypto_updown_max_spread
            order_size = self.settings.near_close_crypto_updown_order_size
            max_bid_price = self.settings.near_close_crypto_updown_max_bid_price
            min_depth = self.settings.near_close_crypto_updown_min_depth
            entry_formula = "max(best_bid + tick, midpoint - discount)"
        elif decision.variant == "crypto":
            if not self.settings.near_close_crypto_enabled:
                return None
            winning_outcome = str(market.raw.get("near_close_crypto_winning_outcome") or "")
            if outcome_label.lower() != winning_outcome.lower():
                return None
            if float(market.raw.get("near_close_crypto_strike_distance") or 0.0) < self.settings.near_close_crypto_min_strike_distance:
                return None
            min_best_ask = self.settings.near_close_crypto_min_best_ask
            min_midpoint = self.settings.near_close_crypto_min_midpoint
            max_spread = self.settings.near_close_crypto_max_spread
            order_size = self.settings.near_close_crypto_order_size
            max_bid_price = self.settings.near_close_max_bid_price
            min_depth = self.settings.near_close_min_depth
            entry_formula = "best_bid + tick"
        else:
            min_best_ask = self.settings.near_close_min_best_ask
            min_midpoint = self.settings.near_close_min_midpoint
            max_spread = self.settings.near_close_max_spread
            order_size = self.settings.near_close_order_size
            max_bid_price = self.settings.near_close_max_bid_price
            min_depth = self.settings.near_close_min_depth
            entry_formula = "best_bid + tick"
        if best_ask is None or best_bid is None or midpoint is None or spread is None:
            return None
        if best_ask < min_best_ask:
            return None
        if midpoint < min_midpoint:
            return None
        if spread > max_spread:
            return None
        if book.tick_size is not None and book.tick_size > 0.01:
            return None

        tick = book.tick_size or 0.001
        entry_candidate = best_bid + tick
        if decision.variant == "crypto_updown":
            entry_candidate = max(
                entry_candidate,
                midpoint - self.settings.near_close_crypto_updown_midpoint_discount,
            )
        entry_bid = self._floor_to_tick(min(entry_candidate, max_bid_price), tick)
        if entry_bid <= 0 or entry_bid >= best_ask:
            return None

        bid_depth = book.depth_for_side("bid", best_bid)
        if bid_depth < min_depth:
            return None

        gross_edge = 1.0 - entry_bid
        risk_penalty = self.settings.estimated_cost_per_leg + 0.005
        net_edge = gross_edge - risk_penalty
        if net_edge <= self.settings.candidate_min_net_edge:
            return None

        confidence = clamp_confidence(
            0.55
            + max(best_ask - min_best_ask, 0.0) * 10
            + max(midpoint - min_midpoint, 0.0) * 6
            + max(max_spread - spread, 0.0) * 5
            + max(0.0, 1 - (minutes_left / max(self.settings.near_close_max_minutes_to_end, 1))) * 0.08
        )
        max_safe_size = min(
            order_size,
            self.settings.live_max_order_size,
            max(bid_depth, min_depth),
        )
        emergency_worst_price = max(
            best_bid - self.settings.near_close_emergency_slippage,
            entry_bid - self.settings.near_close_emergency_max_loss,
        )
        details = {
            "strategy_variant": "near_close_maker",
            "outcome_label": outcome_label,
            "entry_bid": entry_bid,
            "entry_ask": best_ask,
            "entry_formula": entry_formula,
            "max_bid_price": max_bid_price,
            "min_depth": min_depth,
            "current_bid": best_bid,
            "current_midpoint": midpoint,
            "target_exit_price": 1.0,
            "minutes_to_resolution": round(minutes_left, 1),
            "redeem_net_edge": round(gross_edge, 6),
            "primary_exit_mode": "redeem",
            "resolution_source": market.resolution_source,
            "market_filter_reason": decision.reason,
            "near_close_variant": decision.variant,
            "restricted": bool(market.restricted),
            "crypto_spot_price": market.raw.get("near_close_crypto_spot_price"),
            "crypto_strike_price": market.raw.get("near_close_crypto_strike_price"),
            "crypto_strike_distance": market.raw.get("near_close_crypto_strike_distance"),
            "crypto_start_price": market.raw.get("near_close_crypto_start_price"),
            "crypto_start_time": market.raw.get("near_close_crypto_start_time"),
            "crypto_start_distance": market.raw.get("near_close_crypto_start_distance"),
            "crypto_winning_outcome": market.raw.get("near_close_crypto_winning_outcome"),
            "tradable_live": bool(self.settings.near_close_maker_live_enabled),
            "requires_exit_order": False,
            "post_only": True,
            "order_type": "GTD",
            "expiration_sec": self.settings.near_close_gtd_seconds,
            "gtd_safety_buffer_sec": self.settings.near_close_gtd_safety_buffer_sec,
            "max_market_exposure": self.settings.near_close_max_market_exposure,
            "max_total_exposure": self.settings.near_close_max_total_exposure,
            "soft_stop_price": round(entry_bid - self.settings.near_close_soft_stop_offset, 6),
            "hard_stop_midpoint": round(entry_bid - self.settings.near_close_hard_stop_offset, 6),
            "hard_stop_bid": self.settings.near_close_hard_stop_bid,
            "emergency_worst_price": round(emergency_worst_price, 6),
            "cancel_if": {
                "minutes_to_end_below": self._time_window(decision.variant)[0],
                "best_ask_below": min_best_ask,
                "midpoint_below": min_midpoint,
                "spread_above": max_spread,
                "reprice_threshold": self.settings.near_close_reprice_threshold,
                "reprice_cooldown_sec": self.settings.near_close_reprice_cooldown_sec,
                "crypto_strike_distance_below": self.settings.near_close_crypto_cancel_strike_distance,
                "crypto_start_distance_below": self.settings.near_close_crypto_updown_cancel_start_distance,
                "short_drop": self.settings.near_close_short_drop,
                "long_drop": self.settings.near_close_long_drop,
            },
            "paper_observation_required": self.settings.near_close_min_paper_signals_for_live,
        }
        summary = (
            f"Near-close maker bid {entry_bid:.3f} on {outcome_label}; "
            f"{minutes_left:.1f} minutes to close; post-only GTD entry only."
        )
        return Opportunity(
            opportunity_id=self._make_id(market.slug, book.token_id),
            strategy_type=StrategyType.LATE_RESOLUTION,
            direction=SignalDirection.BUY_BASKET,
            title=f"{market.question} | near-close maker {outcome_label}",
            summary=summary,
            market_slugs=[market.slug],
            market_ids=[market.market_id],
            token_ids=[book.token_id],
            prices={
                "entry_bid": entry_bid,
                "entry_ask": best_ask,
                "current_bid": best_bid,
                "current_midpoint": midpoint,
                "target_exit_price": 1.0,
            },
            gross_edge=gross_edge,
            estimated_fees=self.settings.fees_bps / 10_000,
            slippage_estimate=self.settings.slippage_bps / 10_000,
            net_edge=net_edge,
            max_safe_size=max_safe_size,
            available_liquidity=bid_depth,
            confidence_score=confidence,
            timestamp=utc_now(),
            suggested_action=(
                f"Paper observe post-only GTD bid {entry_bid:.3f} on {outcome_label}; "
                "live remains gated until the near-close paper signal requirement is met."
            ),
            link_slugs=[market.slug],
            details=details,
        )

    @staticmethod
    def _make_id(slug: str, token_id: str) -> str:
        return md5(f"{slug}:{token_id}:near_close_maker".encode("utf-8"), usedforsecurity=False).hexdigest()

    @staticmethod
    def _floor_to_tick(price: float, tick: float) -> float:
        if tick <= 0:
            return round(price, 6)
        step = Decimal(str(tick))
        value = Decimal(str(price))
        units = (value / step).to_integral_value(rounding=ROUND_FLOOR)
        return float(units * step)

    def _time_window(self, variant: str) -> tuple[float, float]:
        if variant == "crypto_updown":
            return (
                self.settings.near_close_crypto_updown_min_minutes_to_end,
                self.settings.near_close_crypto_updown_max_minutes_to_end,
            )
        if variant == "crypto":
            return self.settings.near_close_crypto_min_minutes_to_end, self.settings.near_close_crypto_max_minutes_to_end
        return self.settings.near_close_min_minutes_to_end, self.settings.near_close_max_minutes_to_end
