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
        rejection_counts: dict[str, int] | None = None,
    ) -> list[Opportunity]:
        if not self.settings.near_close_maker_enabled:
            return []
        opportunities: list[Opportunity] = []
        for market in markets:
            if not self._allow_market(market, books):
                self._record_crypto_updown_rejection(market, None, "market_not_allowed", rejection_counts)
                continue
            minutes_left = minutes_to(market.end_date)
            if minutes_left is None:
                self._record_crypto_updown_rejection(market, None, "missing_minutes_to_resolution", rejection_counts)
                continue
            for index, outcome in enumerate(market.outcome_refs):
                book = books.get(outcome.token_id)
                outcome_label = outcome.label or f"Outcome {index + 1}"
                if book is None:
                    self._record_crypto_updown_rejection(market, outcome_label, "missing_orderbook", rejection_counts)
                    continue
                opportunity = self._scan_outcome(
                    market=market,
                    book=book,
                    outcome_label=outcome_label,
                    minutes_left=minutes_left,
                    rejection_counts=rejection_counts,
                )
                if opportunity is not None:
                    opportunities.append(opportunity)
        return opportunities

    @staticmethod
    def _record_crypto_updown_rejection(
        market: MarketRecord,
        outcome_label: str | None,
        reason: str,
        rejection_counts: dict[str, int] | None,
    ) -> None:
        if rejection_counts is None:
            return
        decision = classify_near_close_market(market)
        if decision.variant != "crypto_updown":
            return
        winning_outcome = str(market.raw.get("near_close_crypto_winning_outcome") or "")
        if outcome_label is not None and outcome_label.lower() != winning_outcome.lower():
            return
        rejection_counts[reason] = int(rejection_counts.get(reason, 0)) + 1

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
        _min_minutes, max_minutes = self._time_window(decision.variant)
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
        rejection_counts: dict[str, int] | None = None,
    ) -> Opportunity | None:
        best_ask = book.best_ask
        best_bid = book.best_bid
        midpoint = book.midpoint
        spread = book.spread
        decision = classify_near_close_market(market)
        weekend_mode = self.settings.near_close_weekend_mode_active()

        def reject(reason: str) -> None:
            self._record_crypto_updown_rejection(market, outcome_label, reason, rejection_counts)

        seconds_left = minutes_left * 60.0
        entry_window_rejection = self.settings.near_close_entry_window_rejection(seconds_left)
        if entry_window_rejection is not None:
            reject(entry_window_rejection)
            return None

        crypto_start_distance: float | None = None
        required_start_distance: float | None = None
        start_distance_rule: str | None = None
        if decision.variant == "crypto_updown":
            if not (self.settings.near_close_crypto_enabled and self.settings.near_close_crypto_updown_enabled):
                reject("crypto_updown_disabled")
                return None
            winning_outcome = str(market.raw.get("near_close_crypto_winning_outcome") or "")
            if outcome_label.lower() != winning_outcome.lower():
                return None
            try:
                crypto_start_distance = float(market.raw.get("near_close_crypto_start_distance") or 0.0)
            except (TypeError, ValueError):
                reject("missing_start_distance")
                return None
            required_start_distance, start_distance_rule = self._crypto_updown_required_start_distance(minutes_left)
            if crypto_start_distance < required_start_distance:
                reject("start_distance_below_min")
                return None
            min_best_ask = self.settings.effective_near_close_min_best_ask(decision.variant)
            min_midpoint = self.settings.effective_near_close_min_midpoint(decision.variant)
            max_spread = self.settings.effective_near_close_max_spread(decision.variant)
            order_size = self.settings.effective_near_close_order_size(decision.variant)
            min_entry_price = self.settings.effective_near_close_min_entry_price(decision.variant)
            max_entry_price = self.settings.near_close_crypto_updown_max_entry_price
            max_bid_price = min(self.settings.near_close_crypto_updown_max_bid_price, max_entry_price)
            min_depth = self.settings.near_close_crypto_updown_min_depth
            entry_formula = "max(best_bid + tick, midpoint - discount)"
        elif decision.variant == "crypto":
            if not self.settings.near_close_crypto_enabled:
                reject("crypto_disabled")
                return None
            winning_outcome = str(market.raw.get("near_close_crypto_winning_outcome") or "")
            if outcome_label.lower() != winning_outcome.lower():
                return None
            if float(market.raw.get("near_close_crypto_strike_distance") or 0.0) < self.settings.near_close_crypto_min_strike_distance:
                reject("strike_distance_below_min")
                return None
            min_best_ask = self.settings.near_close_crypto_min_best_ask
            min_midpoint = self.settings.near_close_crypto_min_midpoint
            max_spread = self.settings.effective_near_close_max_spread(decision.variant)
            order_size = self.settings.effective_near_close_order_size(decision.variant)
            min_entry_price = 0.0
            max_bid_price = self.settings.near_close_max_bid_price
            min_depth = self.settings.near_close_min_depth
            entry_formula = "best_bid + tick"
        else:
            min_best_ask = self.settings.near_close_min_best_ask
            min_midpoint = self.settings.near_close_min_midpoint
            max_spread = self.settings.effective_near_close_max_spread(decision.variant)
            order_size = self.settings.effective_near_close_order_size(decision.variant)
            min_entry_price = 0.0
            max_bid_price = self.settings.near_close_max_bid_price
            min_depth = self.settings.near_close_min_depth
            entry_formula = "best_bid + tick"
        if best_ask is None or best_bid is None or midpoint is None or spread is None:
            reject("missing_orderbook_prices")
            return None
        if best_ask < min_best_ask:
            reject("best_ask_below_min")
            return None
        if midpoint < min_midpoint:
            reject("midpoint_below_min")
            return None
        if spread > max_spread:
            reject("spread_above_max")
            return None
        if (
            decision.variant == "crypto_updown"
            and best_bid >= self.settings.near_close_crypto_updown_skip_bid_at_or_above
        ):
            reject("bid_at_or_above_skip")
            return None
        if book.tick_size is not None and book.tick_size > 0.01:
            reject("tick_size_too_large")
            return None
        bid_depth = book.depth_for_side("bid", best_bid)
        if bid_depth < min_depth:
            reject("bid_depth_below_min")
            return None
        ask_depth = book.depth_for_side("ask", best_ask)

        tick = book.tick_size or 0.001
        entry_candidate = best_bid + tick
        if decision.variant == "crypto_updown":
            entry_candidate = max(
                entry_candidate,
                midpoint - self.settings.near_close_crypto_updown_midpoint_discount,
            )
        entry_bid = self._floor_to_tick(min(entry_candidate, max_bid_price), tick)
        if entry_bid >= best_ask:
            passive_entry = self._floor_to_tick(min(best_bid, max_bid_price), tick)
            if passive_entry > 0 and passive_entry < best_ask:
                entry_bid = passive_entry
                entry_formula = f"{entry_formula}; fallback best_bid to avoid crossing"
        maker_entry_bid = entry_bid
        maker_would_cross = entry_bid <= 0 or entry_bid >= best_ask
        taker_fallback_reasons: list[str] = []
        taker_fallback_thresholds: dict[str, float | bool] | None = None
        taker_fallback_trigger: str | None = None
        if decision.variant == "crypto_updown":
            taker_fallback_thresholds = self._crypto_updown_taker_fallback_thresholds(
                order_size=order_size,
                required_start_distance=required_start_distance,
            )
            taker_fallback_reasons = self._crypto_updown_taker_fallback_reasons(
                seconds_left=seconds_left,
                best_ask=best_ask,
                spread=spread,
                ask_depth=ask_depth,
                order_size=order_size,
                crypto_start_distance=crypto_start_distance,
                required_start_distance=required_start_distance,
            )
        taker_fallback_eligible = decision.variant == "crypto_updown" and not taker_fallback_reasons
        entry_price = entry_bid
        entry_execution_mode = "maker_post_only"
        order_type = "GTD"
        post_only = True
        expiration_sec: int | None = self.settings.near_close_gtd_seconds
        execution_depth = bid_depth
        if taker_fallback_eligible:
            entry_price = best_ask
            entry_bid = best_ask
            entry_execution_mode = "taker_fallback"
            taker_fallback_trigger = "would_cross_post_only" if maker_would_cross else "tight_taker_window"
            order_type = "FAK"
            post_only = False
            expiration_sec = None
            execution_depth = ask_depth
            entry_formula = f"{entry_formula}; taker fallback best_ask"
        elif maker_would_cross:
            reject("would_cross_post_only")
            return None
        if entry_price < min_entry_price or entry_price > max_bid_price:
            reject("entry_price_out_of_range")
            return None

        gross_edge = 1.0 - entry_price
        risk_penalty = self.settings.estimated_cost_per_leg + 0.005
        net_edge = gross_edge - risk_penalty
        if net_edge <= self.settings.candidate_min_net_edge:
            reject("net_edge_below_min")
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
            execution_depth,
        )
        emergency_worst_price = max(
            best_bid - self.settings.near_close_emergency_slippage,
            entry_price - self.settings.near_close_emergency_max_loss,
        )
        live_distance_allowed = not (
            decision.variant == "crypto_updown"
            and crypto_start_distance is not None
            and crypto_start_distance < self.settings.near_close_crypto_updown_cancel_start_distance
        )
        details = {
            "strategy_variant": "near_close_maker",
            "outcome_label": outcome_label,
            "time_to_resolution_sec": round(seconds_left, 3),
            "entry_price": entry_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "midpoint": midpoint,
            "bid_depth_at_best": bid_depth,
            "ask_depth_at_best": ask_depth,
            "market_slug": market.slug,
            "token_id": book.token_id,
            "entry_bid": entry_price,
            "maker_entry_bid": maker_entry_bid,
            "entry_ask": best_ask,
            "entry_formula": entry_formula,
            "entry_execution_mode": entry_execution_mode,
            "min_entry_price": min_entry_price,
            "max_entry_price": max_bid_price,
            "max_bid_price": max_bid_price,
            "skip_bid_at_or_above": self.settings.near_close_crypto_updown_skip_bid_at_or_above,
            "min_depth": min_depth,
            "current_bid": best_bid,
            "current_midpoint": midpoint,
            "target_exit_price": 1.0,
            "minutes_to_resolution": round(minutes_left, 1),
            "entry_window_min_seconds": self.settings.near_close_entry_window_seconds()[0],
            "entry_window_max_seconds": self.settings.near_close_entry_window_seconds()[1],
            "final_seconds_allow_entry": self.settings.near_close_final_seconds_allow_entry,
            "legacy_no_new_entry_last_seconds": self.settings.near_close_crypto_updown_no_new_entry_last_seconds
            if decision.variant == "crypto_updown"
            else None,
            "redeem_net_edge": round(gross_edge, 6),
            "primary_exit_mode": "redeem",
            "resolution_source": market.resolution_source,
            "market_filter_reason": decision.reason,
            "near_close_variant": decision.variant,
            "weekend_mode": weekend_mode,
            "weekend_mode_name": "light" if weekend_mode else None,
            "effective_order_size": order_size,
            "effective_max_spread": max_spread,
            "restricted": bool(market.restricted),
            "crypto_spot_price": market.raw.get("near_close_crypto_spot_price"),
            "crypto_strike_price": market.raw.get("near_close_crypto_strike_price"),
            "crypto_strike_distance": market.raw.get("near_close_crypto_strike_distance"),
            "crypto_start_price": market.raw.get("near_close_crypto_start_price"),
            "crypto_start_time": market.raw.get("near_close_crypto_start_time"),
            "crypto_start_distance": market.raw.get("near_close_crypto_start_distance"),
            "crypto_start_distance_required": required_start_distance if decision.variant == "crypto_updown" else None,
            "crypto_start_distance_rule": start_distance_rule if decision.variant == "crypto_updown" else None,
            "crypto_start_distance_dynamic": (
                bool(self.settings.near_close_crypto_updown_dynamic_start_distance_enabled)
                if decision.variant == "crypto_updown"
                else None
            ),
            "crypto_winning_outcome": market.raw.get("near_close_crypto_winning_outcome"),
            "taker_fallback_enabled": bool(self.settings.near_close_crypto_updown_taker_fallback_enabled)
            if decision.variant == "crypto_updown"
            else None,
            "taker_fallback_eligible": taker_fallback_eligible if decision.variant == "crypto_updown" else None,
            "taker_fallback_trigger": taker_fallback_trigger,
            "taker_fallback_reasons": taker_fallback_reasons if decision.variant == "crypto_updown" else None,
            "taker_fallback_thresholds": taker_fallback_thresholds,
            "taker_fallback_price": best_ask if taker_fallback_eligible else None,
            "tradable_live": bool(
                self.settings.near_close_maker_live_enabled
                and live_distance_allowed
                and self.settings.near_close_entry_seconds_allowed(seconds_left)
            ),
            "requires_exit_order": False,
            "post_only": post_only,
            "order_type": order_type,
            "expiration_sec": expiration_sec,
            "gtd_safety_buffer_sec": self.settings.near_close_gtd_safety_buffer_sec,
            "max_market_exposure": self.settings.effective_near_close_max_market_exposure(),
            "max_total_exposure": self.settings.effective_near_close_max_total_exposure(),
            "soft_stop_price": round(entry_price - self.settings.near_close_soft_stop_offset, 6),
            "hard_stop_midpoint": round(entry_price - self.settings.near_close_hard_stop_offset, 6),
            "hard_stop_bid": self.settings.near_close_hard_stop_bid,
            "emergency_worst_price": round(emergency_worst_price, 6),
            "cancel_if": {
                "entry_window_min_seconds": self.settings.near_close_entry_window_seconds()[0],
                "entry_window_max_seconds": self.settings.near_close_entry_window_seconds()[1],
                "final_seconds_allow_entry": self.settings.near_close_final_seconds_allow_entry,
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
        if entry_execution_mode == "taker_fallback":
            summary = (
                f"Near-close taker FAK buy {entry_price:.3f} on {outcome_label}; "
                f"{minutes_left:.1f} minutes to close; strict crypto Up/Down fallback."
            )
            title_suffix = "near-close taker fallback"
            suggested_action = (
                f"Submit FAK taker buy {entry_price:.3f} on {outcome_label}; "
                "strict fallback is live-eligible only inside the 30-45 second window."
            )
        else:
            summary = (
                f"Near-close maker bid {entry_price:.3f} on {outcome_label}; "
                f"{minutes_left:.1f} minutes to close; post-only GTD entry only."
            )
            title_suffix = "near-close maker"
            suggested_action = (
                f"Paper observe post-only GTD bid {entry_price:.3f} on {outcome_label}; "
                "live remains gated until the near-close paper signal requirement is met."
            )
        return Opportunity(
            opportunity_id=self._make_id(market.slug, book.token_id),
            strategy_type=StrategyType.LATE_RESOLUTION,
            direction=SignalDirection.BUY_BASKET,
            title=f"{market.question} | {title_suffix} {outcome_label}",
            summary=summary,
            market_slugs=[market.slug],
            market_ids=[market.market_id],
            token_ids=[book.token_id],
            prices={
                "entry_bid": entry_price,
                "entry_ask": best_ask,
                "maker_entry_bid": maker_entry_bid,
                "taker_fallback_price": best_ask if taker_fallback_eligible else None,
                "current_bid": best_bid,
                "current_midpoint": midpoint,
                "target_exit_price": 1.0,
            },
            gross_edge=gross_edge,
            estimated_fees=self.settings.fees_bps / 10_000,
            slippage_estimate=self.settings.slippage_bps / 10_000,
            net_edge=net_edge,
            max_safe_size=max_safe_size,
            available_liquidity=execution_depth,
            confidence_score=confidence,
            timestamp=utc_now(),
            suggested_action=suggested_action,
            link_slugs=[market.slug],
            details=details,
        )

    def _crypto_updown_taker_fallback_thresholds(
        self,
        *,
        order_size: float,
        required_start_distance: float | None,
    ) -> dict[str, float | bool]:
        min_seconds = max(float(self.settings.near_close_crypto_updown_taker_fallback_min_seconds), 0.0)
        max_seconds = max(float(self.settings.near_close_crypto_updown_taker_fallback_max_seconds), min_seconds)
        min_start_ratio = max(
            float(self.settings.near_close_crypto_updown_taker_fallback_min_start_distance_ratio),
            0.0,
        )
        required_distance = max(float(required_start_distance or 0.0), 0.0)
        return {
            "enabled": bool(self.settings.near_close_crypto_updown_taker_fallback_enabled),
            "min_seconds": min_seconds,
            "max_seconds": max_seconds,
            "max_price": float(self.settings.near_close_crypto_updown_taker_fallback_max_price),
            "max_spread": float(self.settings.near_close_crypto_updown_taker_fallback_max_spread),
            "min_ask_depth": max(
                float(self.settings.near_close_crypto_updown_taker_fallback_min_ask_depth),
                float(order_size),
            ),
            "min_start_distance_ratio": min_start_ratio,
            "min_start_distance": required_distance * min_start_ratio,
        }

    def _crypto_updown_taker_fallback_reasons(
        self,
        *,
        seconds_left: float,
        best_ask: float,
        spread: float,
        ask_depth: float,
        order_size: float,
        crypto_start_distance: float | None,
        required_start_distance: float | None,
    ) -> list[str]:
        thresholds = self._crypto_updown_taker_fallback_thresholds(
            order_size=order_size,
            required_start_distance=required_start_distance,
        )
        reasons: list[str] = []
        if not thresholds["enabled"]:
            reasons.append("taker_fallback_disabled")
        if seconds_left < float(thresholds["min_seconds"]):
            reasons.append("taker_fallback_after_window")
        if seconds_left > float(thresholds["max_seconds"]):
            reasons.append("taker_fallback_before_window")
        if best_ask > float(thresholds["max_price"]):
            reasons.append("taker_fallback_ask_above_max")
        if spread > float(thresholds["max_spread"]):
            reasons.append("taker_fallback_spread_above_max")
        if ask_depth < float(thresholds["min_ask_depth"]):
            reasons.append("taker_fallback_ask_depth_below_min")
        required_distance = float(thresholds["min_start_distance"])
        observed_distance = float(crypto_start_distance or 0.0)
        if required_distance > 0 and observed_distance < required_distance:
            reasons.append("taker_fallback_start_distance_below_ratio")
        return reasons

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

    def _crypto_updown_required_start_distance(self, minutes_left: float) -> tuple[float, str]:
        static_threshold = float(self.settings.near_close_crypto_updown_min_start_distance)
        if not self.settings.near_close_crypto_updown_dynamic_start_distance_enabled:
            return self._apply_weekend_start_distance(static_threshold, "static")

        ladder = self._parse_start_distance_ladder(self.settings.near_close_crypto_updown_start_distance_ladder)
        for upper_minutes, threshold in ladder:
            if minutes_left <= upper_minutes:
                return self._apply_weekend_start_distance(threshold, f"<= {upper_minutes:g}m")
        upper_minutes, threshold = ladder[-1]
        return self._apply_weekend_start_distance(threshold, f"> {upper_minutes:g}m")

    def _apply_weekend_start_distance(self, threshold: float, rule: str) -> tuple[float, str]:
        effective = self.settings.effective_near_close_start_distance(threshold)
        if effective == threshold:
            return threshold, rule
        return effective, f"{rule} * weekend {self.settings.near_close_weekend_start_distance_multiplier:g}"

    @staticmethod
    def _parse_start_distance_ladder(raw_ladder: str) -> list[tuple[float, float]]:
        ladder: list[tuple[float, float]] = []
        for raw_item in str(raw_ladder or "").split(","):
            item = raw_item.strip()
            if not item:
                continue
            if ":" not in item:
                raise ValueError(f"Invalid start distance ladder item: {item!r}")
            raw_minutes, raw_threshold = item.split(":", 1)
            try:
                upper_minutes = float(raw_minutes.strip())
                threshold = float(raw_threshold.strip())
            except ValueError as exc:
                raise ValueError(f"Invalid start distance ladder item: {item!r}") from exc
            if upper_minutes <= 0 or threshold < 0:
                raise ValueError(f"Invalid start distance ladder item: {item!r}")
            ladder.append((upper_minutes, threshold))
        if not ladder:
            raise ValueError("NEAR_CLOSE_CRYPTO_UPDOWN_START_DISTANCE_LADDER must contain at least one item")
        return sorted(ladder, key=lambda pair: pair[0])

    def _time_window(self, variant: str) -> tuple[float, float]:
        if variant == "crypto_updown":
            return (
                self.settings.effective_crypto_updown_min_minutes_to_end(),
                self.settings.near_close_crypto_updown_max_minutes_to_end,
            )
        if variant == "crypto":
            return self.settings.near_close_crypto_min_minutes_to_end, self.settings.near_close_crypto_max_minutes_to_end
        return self.settings.near_close_min_minutes_to_end, self.settings.near_close_max_minutes_to_end
