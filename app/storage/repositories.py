from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Any

from app.models.core import EventRecord, LiveExecutionResult, MarketRecord, Opportunity, OrderBookSnapshot, PaperTradeResult
from app.models.runtime import TradingControls
from app.storage.db import DatabaseSession
from app.utils.math_utils import to_isoformat


class ScannerRepository:
    """Persistence helpers for scanner state, runtime controls, and execution audit data."""

    NEAR_CLOSE_VARIANT_PATTERN = '%"strategy_variant": "near_close_maker"%'
    LIVE_JOURNAL_STATUSES = ("CONFIRMED", "MATCHED", "FILLED", "MINED", "REDEEMED", "SETTLED_LOST")
    NEAR_CLOSE_INACTIVE_ORDER_STATUSES = (
        "cancel_requested",
        "cancelled",
        "expired",
        "failed",
        "filled",
        "matched",
        "cancel_unconfirmed",
        "qualification_cancelled",
        "reprice_cancelled",
        "redeemed",
    )

    def __init__(self, connection: DatabaseSession) -> None:
        self.connection = connection

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _today_start_iso(cls) -> str:
        now = cls._now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return day_start.isoformat()

    @classmethod
    def _cutoff_iso(cls, *, hours: int) -> str:
        return (cls._now() - timedelta(hours=hours)).isoformat()

    def save_markets(self, events: Iterable[EventRecord], markets: Iterable[MarketRecord]) -> None:
        discovered_at = self._now().isoformat()
        _ = list(events)
        with self.connection.transaction():
            for market in markets:
                self.connection.execute(
                    """
                    INSERT INTO markets (
                        market_id, event_id, slug, question, end_date, outcome_labels_json, token_ids_json,
                        category, tags_json, active, closed, liquidity, volume, raw_json, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(market_id) DO UPDATE SET
                        event_id=excluded.event_id,
                        slug=excluded.slug,
                        question=excluded.question,
                        end_date=excluded.end_date,
                        outcome_labels_json=excluded.outcome_labels_json,
                        token_ids_json=excluded.token_ids_json,
                        category=excluded.category,
                        tags_json=excluded.tags_json,
                        active=excluded.active,
                        closed=excluded.closed,
                        liquidity=excluded.liquidity,
                        volume=excluded.volume,
                        raw_json=excluded.raw_json,
                        discovered_at=excluded.discovered_at
                    """,
                    (
                        market.market_id,
                        market.event_id,
                        market.slug,
                        market.question,
                        to_isoformat(market.end_date),
                        json.dumps(market.outcome_labels),
                        json.dumps(market.token_ids),
                        market.category,
                        json.dumps(market.tags),
                        bool(market.active),
                        bool(market.closed),
                        market.liquidity,
                        market.volume,
                        json.dumps(market.raw),
                        discovered_at,
                    ),
                )

    def save_orderbooks(self, books: Iterable[OrderBookSnapshot]) -> None:
        with self.connection.transaction():
            for book in books:
                self.connection.execute(
                    """
                    INSERT INTO orderbook_snapshots (
                        token_id, market_id, best_bid, best_ask, midpoint, spread, bids_json, asks_json, captured_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        book.token_id,
                        book.market_id,
                        book.best_bid,
                        book.best_ask,
                        book.midpoint,
                        book.spread,
                        json.dumps([level.model_dump() for level in book.bids]),
                        json.dumps([level.model_dump() for level in book.asks]),
                        to_isoformat(book.updated_at),
                    ),
                )

    def save_opportunities(self, opportunities: Iterable[Opportunity]) -> None:
        with self.connection.transaction():
            for opportunity in opportunities:
                details = dict(opportunity.details)
                details.setdefault("summary", opportunity.summary)
                details.setdefault("suggested_action", opportunity.suggested_action)
                self.connection.execute(
                    """
                    INSERT INTO opportunities (
                        opportunity_id, strategy_type, direction, title, market_slugs_json, gross_edge,
                        estimated_fees, slippage_estimate, net_edge, max_safe_size, available_liquidity,
                        confidence_score, prices_json, details_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(opportunity_id) DO UPDATE SET
                        title=excluded.title,
                        gross_edge=excluded.gross_edge,
                        estimated_fees=excluded.estimated_fees,
                        slippage_estimate=excluded.slippage_estimate,
                        net_edge=excluded.net_edge,
                        max_safe_size=excluded.max_safe_size,
                        available_liquidity=excluded.available_liquidity,
                        confidence_score=excluded.confidence_score,
                        prices_json=excluded.prices_json,
                        details_json=excluded.details_json,
                        created_at=excluded.created_at
                    """,
                    (
                        opportunity.opportunity_id,
                        opportunity.strategy_type.value,
                        opportunity.direction.value,
                        opportunity.title,
                        json.dumps(opportunity.market_slugs),
                        opportunity.gross_edge,
                        opportunity.estimated_fees,
                        opportunity.slippage_estimate,
                        opportunity.net_edge,
                        opportunity.max_safe_size,
                        opportunity.available_liquidity,
                        opportunity.confidence_score,
                        json.dumps(opportunity.prices),
                        json.dumps(details),
                        to_isoformat(opportunity.timestamp),
                    ),
                )

    def was_alerted_recently(self, opportunity_id: str, cooldown_sec: int) -> bool:
        row = self.connection.fetchone(
            """
            SELECT sent_at
            FROM alerts
            WHERE opportunity_id = ?
            ORDER BY sent_at DESC
            LIMIT 1
            """,
            (opportunity_id,),
        )
        if row is None:
            return False
        sent_at = datetime.fromisoformat(str(row["sent_at"]))
        return (self._now() - sent_at).total_seconds() < cooldown_sec

    def save_alert(self, opportunity_id: str, channel: str, message: str) -> None:
        with self.connection.transaction():
            self.connection.execute(
                "INSERT INTO alerts (opportunity_id, channel, message, sent_at) VALUES (?, ?, ?, ?)",
                (
                    opportunity_id,
                    channel,
                    message,
                    self._now().isoformat(),
                ),
            )

    def save_paper_trade(self, result: PaperTradeResult) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO paper_trades (
                    opportunity_id, filled, average_entry_price, filled_size, gross_notional,
                    estimated_fees_paid, expected_pnl, notes, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.opportunity_id,
                    bool(result.filled),
                    result.average_entry_price,
                    result.filled_size,
                    result.gross_notional,
                    result.estimated_fees_paid,
                    result.expected_pnl,
                    result.notes,
                    self._now().isoformat(),
                ),
            )

    def save_scan_cycle(
        self,
        *,
        executed_at: datetime,
        discovered_market_count: int,
        monitored_market_count: int,
        book_count: int,
        opportunity_count: int,
        actionable_count: int,
        candidate_count: int,
        watch_bucket_counts: dict[str, int] | None = None,
        shortlist_reason_counts: dict[str, int] | None = None,
        shortlisted_markets: list[dict[str, Any]] | None = None,
        excluded_long_tail_count: int = 0,
        excluded_family_cap_count: int = 0,
        positive_edge_candidates_24h: int = 0,
        near_close_funnel: list[dict[str, Any]] | None = None,
    ) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO scan_cycles (
                    executed_at, discovered_market_count, monitored_market_count, book_count,
                    opportunity_count, actionable_count, candidate_count, watch_bucket_counts_json,
                    shortlist_reason_counts_json, shortlist_markets_json, excluded_long_tail_count,
                    excluded_family_cap_count, positive_edge_candidates_24h, near_close_funnel_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    to_isoformat(executed_at),
                    discovered_market_count,
                    monitored_market_count,
                    book_count,
                    opportunity_count,
                    actionable_count,
                    candidate_count,
                    json.dumps(watch_bucket_counts or {}),
                    json.dumps(shortlist_reason_counts or {}),
                    json.dumps(shortlisted_markets or []),
                    excluded_long_tail_count,
                    excluded_family_cap_count,
                    positive_edge_candidates_24h,
                    json.dumps(near_close_funnel or []),
                ),
            )

    def recent_positive_edge_by_slug(self, *, hours: int, min_net_edge: float = 0.0) -> dict[str, int]:
        rows = self.connection.fetchall(
            """
            SELECT market_slugs_json
            FROM opportunities
            WHERE created_at >= ? AND net_edge > ?
            """,
            (self._cutoff_iso(hours=hours), min_net_edge),
        )
        counts: dict[str, int] = {}
        for row in rows:
            for slug in self._load_json(row.get("market_slugs_json"), []):
                slug_text = str(slug)
                counts[slug_text] = counts.get(slug_text, 0) + 1
        return counts

    def positive_edge_candidates_24h(self) -> int:
        row = self.connection.fetchone(
            """
            SELECT COUNT(*) AS positive_count
            FROM opportunities
            WHERE created_at >= ? AND net_edge > 0
            """,
            (self._cutoff_iso(hours=24),),
        )
        return int(row["positive_count"]) if row else 0

    def save_live_execution(self, result: LiveExecutionResult) -> None:
        if not result.leg_results:
            return
        with self.connection.transaction():
            for leg in result.leg_results:
                self.connection.execute(
                    """
                    INSERT INTO live_trades (
                        opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                        target_price, requested_size, order_id, status, response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result.opportunity_id,
                        leg.leg_index,
                        leg.action,
                        leg.token_id,
                        leg.market_slug,
                        leg.outcome_label,
                        leg.target_price,
                        leg.requested_size,
                        leg.order_id,
                        leg.status,
                        json.dumps(leg.response),
                        to_isoformat(result.created_at),
                    ),
                )

    def mark_live_orders_cancelled(self, order_ids: list[str], *, status: str = "cancelled") -> int:
        cleaned = [str(order_id).strip() for order_id in order_ids if str(order_id).strip()]
        if not cleaned:
            return 0
        placeholders = ",".join("?" for _ in cleaned)
        with self.connection.transaction():
            cursor = self.connection.execute(
                f"""
                UPDATE live_trades
                SET status = ?
                WHERE order_id IN ({placeholders})
                """,
                (status, *cleaned),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def active_live_order_ids(self, limit: int = 200) -> list[str]:
        rows = self.connection.fetchall(
            """
            SELECT order_id
            FROM live_trades
            WHERE order_id IS NOT NULL
              AND UPPER(status) IN ('SUBMITTED', 'PENDING', 'OPEN', 'CANCEL_REQUESTED')
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [str(row["order_id"]) for row in rows if str(row["order_id"] or "").strip()]

    def near_close_active_orders_for_market(
        self,
        *,
        market_slug: str | None = None,
        token_id: str | None = None,
    ) -> list[dict[str, Any]]:
        inactive_placeholders = ",".join("?" for _ in self.NEAR_CLOSE_INACTIVE_ORDER_STATUSES)
        query = f"""
            SELECT order_id, token_id, market_slug, target_price, requested_size, status, response_json, created_at
            FROM live_trades
            WHERE response_json LIKE ?
              AND status NOT IN ({inactive_placeholders})
        """
        params: list[Any] = [self.NEAR_CLOSE_VARIANT_PATTERN, *self.NEAR_CLOSE_INACTIVE_ORDER_STATUSES]
        if market_slug:
            query += " AND market_slug = ?"
            params.append(market_slug)
        if token_id:
            query += " AND token_id = ?"
            params.append(token_id)
        query += " ORDER BY created_at DESC"

        rows = self.connection.fetchall(query, params)
        now_ts = datetime.now(timezone.utc).timestamp()
        active_orders: list[dict[str, Any]] = []
        for row in rows:
            response = self._load_json(row.get("response_json"), {})
            expiration = response.get("expiration")
            if expiration is not None and float(expiration or 0) <= now_ts:
                continue
            active_orders.append(
                {
                    "order_id": row["order_id"],
                    "token_id": row["token_id"],
                    "market_slug": row["market_slug"],
                    "target_price": float(row["target_price"] or 0.0),
                    "requested_size": float(row["requested_size"] or 0.0),
                    "status": row["status"],
                    "response": response,
                    "created_at": row["created_at"],
                    "created_at_ts": self._parse_iso_timestamp(row["created_at"]),
                }
            )
        return active_orders

    def _market_for_token(self, token_id: str, outcome_label: str | None = None) -> tuple[str, str]:
        rows = self.connection.fetchall(
            """
            SELECT slug, outcome_labels_json, token_ids_json
            FROM markets
            ORDER BY discovered_at DESC
            LIMIT 2500
            """
        )
        for row in rows:
            token_ids = [str(value) for value in self._load_json(row.get("token_ids_json"), [])]
            if token_id not in token_ids:
                continue
            labels = [str(value) for value in self._load_json(row.get("outcome_labels_json"), [])]
            try:
                index = token_ids.index(token_id)
                label = labels[index] if index < len(labels) else outcome_label
            except ValueError:
                label = outcome_label
            return str(row["slug"]), str(label or outcome_label or "Unknown")
        return f"clob-market-{token_id[-8:]}", str(outcome_label or "Unknown")

    def save_clob_fills(self, fills: Iterable[dict[str, Any]], wallet_address: str | None = None) -> int:
        inserted = 0
        wallet = str(wallet_address or "").lower().strip()
        with self.connection.transaction():
            for fill in fills:
                fill_id = str(fill.get("id") or "").strip()
                user_fill = fill
                local_order = None
                selected_maker_order = False
                maker_orders = fill.get("maker_orders")
                if wallet and isinstance(maker_orders, list):
                    for maker_order in maker_orders:
                        if not isinstance(maker_order, dict):
                            continue
                        maker_address = str(maker_order.get("maker_address") or "").lower().strip()
                        if maker_address == wallet:
                            user_fill = {**fill, **maker_order}
                            selected_maker_order = True
                            break
                if user_fill is fill and isinstance(maker_orders, list):
                    for maker_order in maker_orders:
                        if not isinstance(maker_order, dict):
                            continue
                        maker_order_id = str(maker_order.get("order_id") or "").strip()
                        if not maker_order_id:
                            continue
                        local_order = self.connection.fetchone(
                            """
                            SELECT id, market_slug, outcome_label
                            FROM live_trades
                            WHERE order_id = ?
                            LIMIT 1
                            """,
                            (maker_order_id,),
                        )
                        if local_order:
                            user_fill = {**fill, **maker_order}
                            selected_maker_order = True
                            break
                if isinstance(maker_orders, list) and user_fill is fill:
                    fill_maker = str(fill.get("maker_address") or "").lower().strip()
                    if wallet and fill_maker != wallet:
                        continue
                    if not wallet:
                        continue
                order_id = str(user_fill.get("order_id") or user_fill.get("taker_order_id") or fill_id).strip()
                token_id = str(user_fill.get("asset_id") or user_fill.get("token_id") or "").strip()
                action = str(user_fill.get("side") or "").upper().strip()
                status = str(fill.get("status") or "CONFIRMED").upper().strip()
                if not fill_id or not order_id or not token_id or action not in {"BUY", "SELL"}:
                    continue
                exists = self.connection.fetchone(
                    """
                    SELECT id, opportunity_id, order_id, status
                    FROM live_trades
                    WHERE opportunity_id = ? OR order_id = ?
                    LIMIT 1
                    """,
                    (f"clob-fill:{fill_id}", order_id),
                )

                try:
                    price = float(user_fill.get("price") or 0.0)
                    size_source = (
                        user_fill.get("matched_amount")
                        if selected_maker_order and user_fill.get("matched_amount") is not None
                        else user_fill.get("size") or user_fill.get("matched_amount")
                    )
                    size = float(size_source or 0.0)
                except (TypeError, ValueError):
                    continue
                if price <= 0 or size <= 0:
                    continue

                market_slug, outcome_label = self._market_for_token(token_id, str(user_fill.get("outcome") or "Unknown"))
                match_time = fill.get("match_time") or fill.get("created_at")
                created_at = self._now().isoformat()
                if match_time is not None:
                    try:
                        created_at = datetime.fromtimestamp(float(match_time), tz=timezone.utc).isoformat()
                    except (TypeError, ValueError, OSError):
                        created_at = self._now().isoformat()

                if local_order:
                    market_slug = str(local_order["market_slug"])
                    outcome_label = str(local_order["outcome_label"])
                if exists:
                    existing_status = str(exists["status"] or "").upper()
                    if existing_status in {"REDEEMED", "SETTLED_LOST", "MISATTRIBUTED_FILL_IGNORED"}:
                        continue
                    if str(exists["order_id"] or "") == order_id:
                        self.connection.execute(
                            """
                            UPDATE live_trades
                            SET action = ?,
                                token_id = ?,
                                market_slug = ?,
                                outcome_label = ?,
                                target_price = ?,
                                requested_size = ?,
                                status = ?,
                                response_json = ?,
                                created_at = ?
                            WHERE id = ?
                            """,
                            (
                                action,
                                token_id,
                                market_slug,
                                outcome_label,
                                price,
                                size,
                                status,
                                json.dumps(fill),
                                created_at,
                                exists["id"],
                            ),
                        )
                        inserted += 1
                    continue

                self.connection.execute(
                    """
                    INSERT INTO live_trades (
                        opportunity_id, leg_index, action, token_id, market_slug, outcome_label,
                        target_price, requested_size, order_id, status, response_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"clob-fill:{fill_id}",
                        1,
                        action,
                        token_id,
                        market_slug,
                        outcome_label,
                        price,
                        size,
                        order_id,
                        status,
                        json.dumps(fill),
                        created_at,
                    ),
                )
                inserted += 1
        return inserted

    def redeem_candidate_live_trades(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.connection.fetchall(
            """
            SELECT id,
                   opportunity_id,
                   action,
                   token_id,
                   market_slug,
                   outcome_label,
                   target_price,
                   requested_size,
                   order_id,
                   status,
                   created_at
            FROM live_trades
            WHERE order_id IS NOT NULL
              AND UPPER(action) = 'BUY'
              AND UPPER(status) IN ('CONFIRMED', 'MATCHED', 'FILLED', 'MINED', 'CANCEL_UNCONFIRMED')
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        if not rows:
            return []
        market_rows = self.connection.fetchall(
            """
            SELECT market_id, slug, question, end_date, outcome_labels_json, token_ids_json, raw_json
            FROM markets
            ORDER BY discovered_at DESC
            """
        )
        markets: list[dict[str, Any]] = []
        for row in market_rows:
            tokens = [str(value) for value in self._load_json(row.get("token_ids_json"), [])]
            labels = [str(value) for value in self._load_json(row.get("outcome_labels_json"), [])]
            markets.append(
                {
                    "market_id": row["market_id"],
                    "slug": row["slug"],
                    "question": row["question"],
                    "end_date": row["end_date"],
                    "token_ids": tokens,
                    "outcome_labels": labels,
                    "raw": self._load_json(row.get("raw_json"), {}),
                }
            )

        candidates: list[dict[str, Any]] = []
        by_token: dict[str, dict[str, Any]] = {}
        for row in rows:
            token_id = str(row["token_id"] or "")
            if not token_id:
                continue
            existing = by_token.get(token_id)
            if existing is not None:
                existing["trade_ids"].append(int(row["id"]))
                continue
            market = next((item for item in markets if token_id in item["token_ids"]), None)
            if market is None:
                continue
            try:
                outcome_index = market["token_ids"].index(token_id)
            except ValueError:
                continue
            candidate = {
                **dict(row),
                "market": market,
                "outcome_index": outcome_index,
                "trade_ids": [int(row["id"])],
            }
            by_token[token_id] = candidate
            candidates.append(candidate)
        return candidates

    def mark_live_trade_ids_status(self, trade_ids: Iterable[int], status: str) -> int:
        cleaned = [int(value) for value in trade_ids]
        if not cleaned:
            return 0
        placeholders = ",".join("?" for _ in cleaned)
        with self.connection.transaction():
            cursor = self.connection.execute(
                f"""
                UPDATE live_trades
                SET status = ?
                WHERE id IN ({placeholders})
                """,
                (status, *cleaned),
            )
        return int(getattr(cursor, "rowcount", 0) or 0)

    def get_trading_controls(self, defaults: TradingControls) -> TradingControls:
        row = self.connection.fetchone(
            """
            SELECT live_trading_enabled, auto_execute_enabled, kill_switch_enabled
            FROM runtime_controls
            WHERE id = 1
            """
        )
        if row is None:
            self.save_trading_controls(defaults)
            return defaults
        return TradingControls(
            live_trading_enabled=bool(row["live_trading_enabled"]),
            auto_execute_enabled=bool(row["auto_execute_enabled"]),
            kill_switch_enabled=bool(row["kill_switch_enabled"]),
        )

    def save_trading_controls(self, controls: TradingControls) -> TradingControls:
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO runtime_controls (
                    id, live_trading_enabled, auto_execute_enabled, kill_switch_enabled, updated_at
                ) VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    live_trading_enabled=excluded.live_trading_enabled,
                    auto_execute_enabled=excluded.auto_execute_enabled,
                    kill_switch_enabled=excluded.kill_switch_enabled,
                    updated_at=excluded.updated_at
                """,
                (
                    bool(controls.live_trading_enabled),
                    bool(controls.auto_execute_enabled),
                    bool(controls.kill_switch_enabled),
                    self._now().isoformat(),
                ),
            )
        return controls

    def claim_execution(
        self,
        *,
        claim_key: str,
        opportunity_id: str,
        source: str,
        mode: str,
        status: str = "claimed",
        message: str = "",
    ) -> bool:
        timestamp = self._now().isoformat()
        with self.connection.transaction():
            cursor = self.connection.execute(
                """
                INSERT INTO execution_claims (
                    claim_key, opportunity_id, source, mode, status, message, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(claim_key) DO NOTHING
                """,
                (
                    claim_key,
                    opportunity_id,
                    source,
                    mode,
                    status,
                    message,
                    timestamp,
                    timestamp,
                ),
            )
        return bool(getattr(cursor, "rowcount", 0))

    def update_execution_claim(self, *, claim_key: str, status: str, message: str) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """
                UPDATE execution_claims
                SET status = ?, message = ?, updated_at = ?
                WHERE claim_key = ?
                """,
                (status, message, self._now().isoformat(), claim_key),
            )

    def save_execution_event(
        self,
        *,
        source: str,
        mode: str,
        opportunity_id: str | None,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
        claim_key: str | None = None,
    ) -> None:
        with self.connection.transaction():
            self.connection.execute(
                """
                INSERT INTO execution_audit_log (
                    claim_key, opportunity_id, source, mode, status, message, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim_key,
                    opportunity_id,
                    source,
                    mode,
                    status,
                    message,
                    json.dumps(details or {}),
                    self._now().isoformat(),
                ),
            )

    def recent_execution_events(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.connection.fetchall(
            """
            SELECT claim_key, opportunity_id, source, mode, status, message, details_json, created_at
            FROM execution_audit_log
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                **row,
                "details": self._load_json(row.get("details_json"), {}),
            }
            for row in rows
        ]

    def recent_live_positions(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.connection.fetchall(
            """
            SELECT opportunity_id,
                   token_id,
                   action,
                   market_slug,
                   outcome_label,
                   target_price,
                   requested_size,
                   order_id,
                   status,
                   created_at
            FROM live_trades
            WHERE order_id IS NOT NULL
              AND UPPER(status) IN ('CONFIRMED', 'MATCHED', 'FILLED')
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [
            {
                **row,
                "notional": float(row["requested_size"]) * float(row["target_price"]),
            }
            for row in rows
        ]

    def _estimated_live_trade_summary(
        self,
        *,
        since_iso: str | None = None,
    ) -> dict[str, Any]:
        status_placeholders = ",".join("?" for _ in self.LIVE_JOURNAL_STATUSES)
        query = """
            SELECT token_id,
                   action,
                   market_slug,
                   outcome_label,
                   target_price,
                   requested_size,
                   order_id,
                   status,
                   created_at
            FROM live_trades
            WHERE order_id IS NOT NULL
              AND UPPER(status) IN ({status_placeholders})
        """
        query = query.format(status_placeholders=status_placeholders)
        params: list[Any] = list(self.LIVE_JOURNAL_STATUSES)
        if since_iso is not None:
            query += " AND created_at >= ?"
            params.append(since_iso)
        query += " ORDER BY created_at ASC, id ASC"
        rows = self.connection.fetchall(query, params)

        open_lots: dict[str, list[dict[str, float]]] = {}
        realized_pnl = 0.0
        matched_size = 0.0
        matched_trade_count = 0

        for row in rows:
            action = str(row["action"]).upper()
            size = float(row["requested_size"] or 0.0)
            price = float(row["target_price"] or 0.0)
            if size <= 0 or price <= 0:
                continue

            key = f'{row["token_id"]}:{row["market_slug"]}:{row["outcome_label"]}'
            status = str(row["status"]).upper()
            if action == "BUY" and status == "REDEEMED":
                realized_pnl += (1.0 - price) * size
                matched_size += size
                matched_trade_count += 1
                continue
            if action == "BUY" and status == "SETTLED_LOST":
                realized_pnl -= price * size
                matched_size += size
                matched_trade_count += 1
                continue

            lots = open_lots.setdefault(key, [])
            if action == "BUY":
                lots.append({"size": size, "price": price})
                continue
            if action != "SELL":
                continue

            sell_matched = 0.0
            remaining = size
            while remaining > 1e-9 and lots:
                lot = lots[0]
                matched = min(remaining, float(lot["size"]))
                realized_pnl += (price - float(lot["price"])) * matched
                matched_size += matched
                sell_matched += matched
                remaining -= matched
                lot["size"] = float(lot["size"]) - matched
                if lot["size"] <= 1e-9:
                    lots.pop(0)
            if sell_matched > 0:
                matched_trade_count += 1

        open_size_total = sum(
            float(lot["size"])
            for lots in open_lots.values()
            for lot in lots
            if float(lot["size"]) > 1e-9
        )
        open_cost_basis = sum(
            float(lot["size"]) * float(lot["price"])
            for lots in open_lots.values()
            for lot in lots
            if float(lot["size"]) > 1e-9
        )
        return {
            "trade_count": len(rows),
            "matched_trade_count": matched_trade_count,
            "matched_size": matched_size,
            "estimated_realized_pnl": realized_pnl,
            "open_size_total": open_size_total,
            "open_cost_basis": open_cost_basis,
        }

    def live_trade_groups(self, limit: int = 8) -> list[dict[str, Any]]:
        status_placeholders = ",".join("?" for _ in self.LIVE_JOURNAL_STATUSES)
        rows = self.connection.fetchall(
            f"""
            SELECT token_id,
                   action,
                   market_slug,
                   outcome_label,
                   target_price,
                   requested_size,
                   order_id,
                   status,
                   created_at
            FROM live_trades
            WHERE order_id IS NOT NULL
              AND UPPER(status) IN ({status_placeholders})
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (*self.LIVE_JOURNAL_STATUSES, max(limit * 8, limit)),
        )

        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            key = f'{row["market_slug"]}:{row["token_id"]}:{row["outcome_label"]}'
            group = groups.setdefault(
                key,
                {
                    "market_slug": row["market_slug"],
                    "outcome_label": row["outcome_label"],
                    "token_id": row["token_id"],
                    "latest_at": row["created_at"],
                    "latest_status": row["status"],
                    "buy_size": 0.0,
                    "sell_size": 0.0,
                    "redeemed_size": 0.0,
                    "open_size": 0.0,
                    "open_cost_basis": 0.0,
                    "estimated_realized_pnl": 0.0,
                    "trades": [],
                },
            )
            row_time = str(row["created_at"] or "")
            if row_time > str(group["latest_at"] or ""):
                group["latest_at"] = row["created_at"]
                group["latest_status"] = row["status"]
            price = float(row["target_price"] or 0.0)
            size = float(row["requested_size"] or 0.0)
            action = str(row["action"]).upper()
            status = str(row["status"]).upper()
            group["trades"].append(
                {
                    "action": action,
                    "status": row["status"],
                    "price": price,
                    "size": size,
                    "notional": price * size,
                    "order_id": row["order_id"],
                    "created_at": row["created_at"],
                }
            )
            if action == "BUY":
                group["buy_size"] += size
                if status == "REDEEMED":
                    group["redeemed_size"] += size
                    group["estimated_realized_pnl"] += (1.0 - price) * size
            elif action == "SELL":
                group["sell_size"] += size

        for group in groups.values():
            open_lots: list[dict[str, float]] = []
            realized_pnl = 0.0
            redeemed_size = 0.0
            entry_notional = 0.0
            exit_notional = 0.0
            for trade in sorted(group["trades"], key=lambda item: str(item.get("created_at") or "")):
                action = str(trade["action"]).upper()
                status = str(trade["status"]).upper()
                size = float(trade["size"] or 0.0)
                price = float(trade["price"] or 0.0)
                if size <= 0 or price <= 0:
                    continue
                if action == "BUY" and status == "REDEEMED":
                    entry_notional += price * size
                    exit_notional += size
                    realized_pnl += (1.0 - price) * size
                    redeemed_size += size
                    continue
                if action == "BUY" and status == "SETTLED_LOST":
                    entry_notional += price * size
                    realized_pnl -= price * size
                    continue
                if action == "BUY":
                    entry_notional += price * size
                    open_lots.append({"size": size, "price": price})
                    continue
                if action != "SELL":
                    continue
                exit_notional += price * size
                remaining = size
                while remaining > 1e-9 and open_lots:
                    lot = open_lots[0]
                    matched = min(remaining, float(lot["size"]))
                    realized_pnl += (price - float(lot["price"])) * matched
                    remaining -= matched
                    lot["size"] = float(lot["size"]) - matched
                    if lot["size"] <= 1e-9:
                        open_lots.pop(0)
            group["redeemed_size"] = redeemed_size
            group["open_size"] = sum(float(lot["size"]) for lot in open_lots)
            group["open_cost_basis"] = sum(float(lot["size"]) * float(lot["price"]) for lot in open_lots)
            group["estimated_realized_pnl"] = realized_pnl
            group["entry_notional"] = entry_notional
            group["exit_notional"] = exit_notional
            group["trades"] = sorted(group["trades"], key=lambda item: str(item.get("created_at") or ""), reverse=True)

        token_ids = [str(group["token_id"]) for group in groups.values() if str(group.get("token_id") or "")]
        latest_books: dict[str, dict[str, Any]] = {}
        if token_ids:
            placeholders = ",".join("?" for _ in token_ids)
            rows = self.connection.fetchall(
                f"""
                SELECT obs.token_id,
                       obs.best_bid,
                       obs.best_ask,
                       obs.midpoint,
                       obs.captured_at
                FROM orderbook_snapshots obs
                INNER JOIN (
                    SELECT token_id, MAX(captured_at) AS captured_at
                    FROM orderbook_snapshots
                    WHERE token_id IN ({placeholders})
                    GROUP BY token_id
                ) latest
                  ON latest.token_id = obs.token_id
                 AND latest.captured_at = obs.captured_at
                """,
                tuple(token_ids),
            )
            latest_books = {str(row["token_id"]): row for row in rows}

        for group in groups.values():
            book = latest_books.get(str(group.get("token_id") or ""))
            current_price = None
            current_price_source = "missing"
            if book:
                best_bid = book.get("best_bid")
                midpoint = book.get("midpoint")
                best_ask = book.get("best_ask")
                if best_bid is not None:
                    current_price = float(best_bid)
                    current_price_source = "best_bid"
                elif midpoint is not None:
                    current_price = float(midpoint)
                    current_price_source = "midpoint"
                elif best_ask is not None:
                    current_price = float(best_ask)
                    current_price_source = "best_ask"
            open_size = float(group.get("open_size") or 0.0)
            open_cost_basis = float(group.get("open_cost_basis") or 0.0)
            current_value = open_size * current_price if current_price is not None else None
            unrealized_pnl = (current_value - open_cost_basis) if current_value is not None else None
            total_pnl = float(group.get("estimated_realized_pnl") or 0.0) + (
                unrealized_pnl if unrealized_pnl is not None else 0.0
            )
            group["current_price"] = current_price
            group["current_price_source"] = current_price_source
            group["current_price_at"] = book.get("captured_at") if book else None
            group["current_value"] = current_value
            group["unrealized_pnl"] = unrealized_pnl
            group["total_pnl"] = total_pnl
            group["position_status"] = "open" if open_size > 1e-9 else "closed"

        return sorted(groups.values(), key=lambda item: str(item.get("latest_at") or ""), reverse=True)[:limit]

    def open_live_positions(self, limit: int = 12) -> list[dict[str, Any]]:
        groups = [group for group in self.live_trade_groups(limit=50) if float(group.get("open_size") or 0.0) > 1e-9]
        if not groups:
            return []

        slugs = sorted({str(group["market_slug"]) for group in groups if str(group.get("market_slug") or "")})
        market_lookup: dict[str, dict[str, Any]] = {}
        if slugs:
            placeholders = ",".join("?" for _ in slugs)
            rows = self.connection.fetchall(
                f"""
                SELECT slug, question, end_date, active, closed
                FROM markets
                WHERE slug IN ({placeholders})
                """,
                tuple(slugs),
            )
            market_lookup = {str(row["slug"]): row for row in rows}

        now_ts = self._now().timestamp()
        positions: list[dict[str, Any]] = []
        for group in groups:
            market = market_lookup.get(str(group["market_slug"]), {})
            end_date = market.get("end_date") if market else None
            expires_in_sec = None
            if end_date:
                end_ts = self._parse_iso_timestamp(str(end_date))
                if end_ts is not None:
                    expires_in_sec = end_ts - now_ts
            positions.append(
                {
                    "market_slug": group["market_slug"],
                    "market_title": market.get("question") or group["market_slug"],
                    "outcome_label": group["outcome_label"],
                    "token_id": group["token_id"],
                    "open_size": group["open_size"],
                    "open_cost_basis": group["open_cost_basis"],
                    "average_entry_price": (
                        float(group["open_cost_basis"]) / float(group["open_size"])
                        if float(group["open_size"] or 0.0) > 1e-9
                        else 0.0
                    ),
                    "latest_at": group["latest_at"],
                    "latest_status": group["latest_status"],
                    "end_date": end_date,
                    "expires_in_sec": expires_in_sec,
                    "active": market.get("active") if market else None,
                    "closed": market.get("closed") if market else None,
                }
            )

        return sorted(
            positions,
            key=lambda item: (
                item["expires_in_sec"] is None,
                item["expires_in_sec"] if item["expires_in_sec"] is not None else float("inf"),
                str(item.get("latest_at") or ""),
            ),
        )[:limit]

    def live_trade_journal_summary(self) -> dict[str, Any]:
        today = self._estimated_live_trade_summary(since_iso=self._today_start_iso())
        total = self._estimated_live_trade_summary()
        return {
            "estimated_realized_pnl_today": today["estimated_realized_pnl"],
            "estimated_realized_pnl_total": total["estimated_realized_pnl"],
            "trade_count_today": today["trade_count"],
            "trade_count_total": total["trade_count"],
            "open_size_total": total["open_size_total"],
            "open_cost_basis": total["open_cost_basis"],
            "note": "Live 損益依 CLOB confirmed fills 估算，仍需定期與錢包餘額對帳。",
        }

    def settled_pnl_summary(self) -> dict[str, Any]:
        row = self.connection.fetchone(
            """
            SELECT COUNT(*) AS paper_settled_count,
                   COALESCE(SUM(expected_pnl), 0.0) AS paper_realized_pnl_today,
                   COALESCE(SUM(estimated_fees_paid), 0.0) AS paper_realized_fees_today
            FROM paper_trades
            WHERE filled = TRUE AND created_at >= ?
            """,
            (self._today_start_iso(),),
        )
        paper_summary = row or {
            "paper_settled_count": 0,
            "paper_realized_pnl_today": 0.0,
            "paper_realized_fees_today": 0.0,
        }
        return {
            **paper_summary,
            "live_settled_pnl_today": self.live_trade_journal_summary()["estimated_realized_pnl_today"],
            "live_settled_supported": True,
            "note": "Live 損益依 CLOB confirmed fills 估算，尚未包含 gas。",
        }

    def top_opportunities_today(self, limit: int = 10) -> list[dict[str, Any]]:
        return self.connection.fetchall(
            """
            SELECT title, strategy_type, net_edge, available_liquidity, confidence_score, created_at
            FROM opportunities
            WHERE created_at >= ?
            ORDER BY net_edge DESC, confidence_score DESC
            LIMIT ?
            """,
            (self._today_start_iso(), limit),
        )

    def strategy_hit_rate(self) -> list[dict[str, Any]]:
        return self.connection.fetchall(
            """
            SELECT o.strategy_type,
                   COUNT(pt.id) AS total_paper_trades,
                   COALESCE(SUM(CASE WHEN pt.filled = TRUE THEN 1 ELSE 0 END), 0) AS filled_count,
                   CASE
                       WHEN COUNT(pt.id) = 0 THEN 0.0
                       ELSE (SUM(CASE WHEN pt.filled = TRUE THEN 1 ELSE 0 END) * 1.0) / COUNT(pt.id)
                   END AS hit_rate
            FROM opportunities o
            LEFT JOIN paper_trades pt ON pt.opportunity_id = o.opportunity_id
            GROUP BY o.strategy_type
            ORDER BY hit_rate DESC, total_paper_trades DESC
            """
        )

    def average_realized_pnl(self) -> float | None:
        row = self.connection.fetchone(
            """
            SELECT AVG(expected_pnl) AS avg_pnl
            FROM paper_trades
            WHERE filled = TRUE
            """
        )
        return row["avg_pnl"] if row else None

    def alert_to_fill_latency(self) -> float | None:
        filled_trades = self.connection.fetchall(
            """
            SELECT opportunity_id, created_at
            FROM paper_trades
            WHERE filled = TRUE
            """
        )
        if not filled_trades:
            return None
        alert_rows = self.connection.fetchall(
            """
            SELECT opportunity_id, sent_at
            FROM alerts
            ORDER BY sent_at DESC
            """
        )
        latest_alert_by_opportunity: dict[str, datetime] = {}
        for row in alert_rows:
            opportunity_id = str(row["opportunity_id"])
            latest_alert_by_opportunity.setdefault(opportunity_id, datetime.fromisoformat(str(row["sent_at"])))
        latencies: list[float] = []
        for trade in filled_trades:
            opportunity_id = str(trade["opportunity_id"])
            if opportunity_id not in latest_alert_by_opportunity:
                continue
            fill_time = datetime.fromisoformat(str(trade["created_at"]))
            latency = (fill_time - latest_alert_by_opportunity[opportunity_id]).total_seconds()
            latencies.append(latency)
        if not latencies:
            return None
        return sum(latencies) / len(latencies)

    def latest_scan_cycle(self) -> dict[str, Any]:
        row = self.connection.fetchone(
            """
            SELECT executed_at,
                   discovered_market_count,
                   monitored_market_count,
                   book_count,
                   opportunity_count,
                   actionable_count,
                   candidate_count,
                   watch_bucket_counts_json,
                   shortlist_reason_counts_json,
                   shortlist_markets_json,
                   excluded_long_tail_count,
                   excluded_family_cap_count,
                   positive_edge_candidates_24h,
                   near_close_funnel_json
            FROM scan_cycles
            ORDER BY executed_at DESC, id DESC
            LIMIT 1
            """
        ) or {}
        if not row:
            return {}
        row["watch_bucket_counts"] = self._load_json(row.get("watch_bucket_counts_json"), {})
        row["shortlist_reason_counts"] = self._load_json(row.get("shortlist_reason_counts_json"), {})
        row["shortlist_markets"] = self._load_json(row.get("shortlist_markets_json"), [])
        row["near_close_funnel"] = self._load_json(row.get("near_close_funnel_json"), [])
        return row

    def paper_risk_summary(self) -> dict[str, Any]:
        row = self.connection.fetchone(
            """
            SELECT COUNT(*) AS paper_trades_today,
                   COALESCE(SUM(CASE WHEN filled = TRUE THEN gross_notional ELSE 0 END), 0.0) AS paper_notional_today,
                   COALESCE(SUM(CASE WHEN filled = TRUE THEN estimated_fees_paid ELSE 0 END), 0.0) AS paper_fees_today
            FROM paper_trades
            WHERE created_at >= ?
            """,
            (self._today_start_iso(),),
        )
        return row or {
            "paper_trades_today": 0,
            "paper_notional_today": 0.0,
            "paper_fees_today": 0.0,
        }

    def live_risk_summary(self) -> dict[str, Any]:
        live_statuses = (
            "SUBMITTED",
            "CONFIRMED",
            "MATCHED",
            "FILLED",
            "MINED",
            "REDEEMED",
            "SETTLED_LOST",
        )
        placeholders = ",".join("?" for _ in live_statuses)
        row = self.connection.fetchone(
            f"""
            SELECT COUNT(*) AS live_orders_today,
                   COALESCE(SUM(requested_size * target_price), 0.0) AS live_notional_today
            FROM live_trades
            WHERE created_at >= ?
              AND UPPER(status) IN ({placeholders})
            """,
            (self._today_start_iso(), *live_statuses),
        )
        return row or {
            "live_orders_today": 0,
            "live_notional_today": 0.0,
        }

    def near_close_signal_count(self) -> int:
        row = self.connection.fetchone(
            """
            SELECT COUNT(*) AS signal_count
            FROM opportunities
            WHERE strategy_type = 'late_resolution'
              AND details_json LIKE '%"strategy_variant": "near_close_maker"%'
            """
        )
        return int(row["signal_count"]) if row else 0

    def near_close_live_exposure(self) -> dict[str, Any]:
        inactive_placeholders = ",".join("?" for _ in self.NEAR_CLOSE_INACTIVE_ORDER_STATUSES)
        rows = self.connection.fetchall(
            f"""
            SELECT market_slug, action, target_price, requested_size, status, response_json
            FROM live_trades
            WHERE response_json LIKE '%"strategy_variant": "near_close_maker"%'
              AND status NOT IN ({inactive_placeholders})
            """,
            self.NEAR_CLOSE_INACTIVE_ORDER_STATUSES,
        )
        by_market: dict[str, float] = {}
        total = 0.0
        active_orders = 0
        now_ts = datetime.now(timezone.utc).timestamp()
        for row in rows:
            response = self._load_json(row.get("response_json"), {})
            expiration = response.get("expiration")
            if expiration is not None and float(expiration or 0) <= now_ts:
                continue
            active_orders += 1
            notional = float(row["target_price"] or 0.0) * float(row["requested_size"] or 0.0)
            if str(row["action"]).upper() == "SELL":
                notional = -notional
            market_slug = str(row["market_slug"])
            by_market[market_slug] = by_market.get(market_slug, 0.0) + notional
            total += notional
        return {"total": max(total, 0.0), "by_market": by_market, "active_orders": active_orders}

    def near_close_dashboard_summary(self) -> dict[str, Any]:
        signal_count = self.near_close_signal_count()
        exposure = self.near_close_live_exposure()
        return {
            "mode": "paper" if signal_count < 100 else "paper_gate_met",
            "signal_count": signal_count,
            "paper_required": 100,
            "live_exposure": exposure["total"],
            "active_orders": exposure["active_orders"],
        }

    def trading_risk_summary(self) -> dict[str, Any]:
        return {
            **self.paper_risk_summary(),
            **self.live_risk_summary(),
        }

    @staticmethod
    def _load_json(value: str | None, fallback: Any) -> Any:
        if not value:
            return fallback
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return fallback

    @staticmethod
    def _parse_iso_timestamp(value: str | None) -> float:
        if not value:
            return 0.0
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return 0.0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()

    def dashboard_summary(self) -> dict[str, Any]:
        market_row = self.connection.fetchone(
            """
            SELECT COUNT(*) AS total_markets,
                   COALESCE(SUM(CASE WHEN active = TRUE AND closed = FALSE THEN 1 ELSE 0 END), 0) AS open_markets,
                   MAX(discovered_at) AS latest_discovered_at
            FROM markets
            """
        )
        opportunity_row = self.connection.fetchone(
            """
            SELECT COUNT(*) AS total_opportunities,
                   COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS opportunities_24h,
                   MAX(created_at) AS latest_opportunity_at,
                   MAX(net_edge) AS best_net_edge
            FROM opportunities
            """,
            (self._cutoff_iso(hours=24),),
        )
        alert_row = self.connection.fetchone(
            """
            SELECT COALESCE(SUM(CASE WHEN sent_at >= ? THEN 1 ELSE 0 END), 0) AS alerts_24h,
                   MAX(sent_at) AS latest_alert_at
            FROM alerts
            """,
            (self._cutoff_iso(hours=24),),
        )
        scan_row = self.latest_scan_cycle()
        audit_row = self.connection.fetchone(
            """
            SELECT COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0) AS execution_events_24h,
                   MAX(created_at) AS latest_execution_event_at
            FROM execution_audit_log
            """,
            (self._cutoff_iso(hours=24),),
        )
        risk_summary = self.trading_risk_summary()

        return {
            "total_markets": market_row["total_markets"] if market_row else 0,
            "open_markets": market_row["open_markets"] if market_row else 0,
            "latest_discovered_at": market_row["latest_discovered_at"] if market_row else None,
            "latest_scan_at": scan_row.get("executed_at"),
            "latest_discovered_market_count": scan_row.get("discovered_market_count", 0),
            "latest_monitored_markets": scan_row.get("monitored_market_count", 0),
            "latest_book_count": scan_row.get("book_count", 0),
            "latest_scan_opportunities": scan_row.get("opportunity_count", 0),
            "latest_actionable_count": scan_row.get("actionable_count", 0),
            "latest_candidate_count": scan_row.get("candidate_count", 0),
            "watch_bucket_counts": scan_row.get("watch_bucket_counts", {}),
            "shortlist_reason_counts": scan_row.get("shortlist_reason_counts", {}),
            "near_close_funnel": scan_row.get("near_close_funnel", []),
            "excluded_long_tail_count": scan_row.get("excluded_long_tail_count", 0),
            "excluded_family_cap_count": scan_row.get("excluded_family_cap_count", 0),
            "positive_edge_candidates_24h": scan_row.get(
                "positive_edge_candidates_24h",
                self.positive_edge_candidates_24h(),
            ),
            "total_opportunities": opportunity_row["total_opportunities"] if opportunity_row else 0,
            "opportunities_24h": opportunity_row["opportunities_24h"] if opportunity_row else 0,
            "latest_opportunity_at": opportunity_row["latest_opportunity_at"] if opportunity_row else None,
            "best_net_edge": opportunity_row["best_net_edge"] if opportunity_row else None,
            "alerts_24h": alert_row["alerts_24h"] if alert_row else 0,
            "latest_alert_at": alert_row["latest_alert_at"] if alert_row else None,
            "latest_snapshot_at": scan_row.get("executed_at"),
            "average_paper_pnl": self.average_realized_pnl(),
            "paper_trades_today": risk_summary["paper_trades_today"],
            "paper_notional_today": risk_summary["paper_notional_today"],
            "paper_fees_today": risk_summary["paper_fees_today"],
            "live_orders_today": risk_summary["live_orders_today"],
            "live_notional_today": risk_summary["live_notional_today"],
            "execution_events_24h": audit_row["execution_events_24h"] if audit_row else 0,
            "latest_execution_event_at": audit_row["latest_execution_event_at"] if audit_row else None,
        }

    def latest_opportunities(self, limit: int = 18, strategy_variant: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT opportunity_id, strategy_type, direction, title, market_slugs_json, gross_edge,
                   net_edge, max_safe_size, available_liquidity, confidence_score, prices_json,
                   details_json, created_at
            FROM opportunities
        """
        params: list[Any] = []
        if strategy_variant == "near_close_maker":
            query += """
            WHERE strategy_type = 'late_resolution'
              AND details_json LIKE ?
            """
            params.append(self.NEAR_CLOSE_VARIANT_PATTERN)
        query += """
            ORDER BY created_at DESC, net_edge DESC
            LIMIT ?
            """
        params.append(limit)
        rows = self.connection.fetchall(query, params)
        opportunities: list[dict[str, Any]] = []
        for row in rows:
            details = self._load_json(row["details_json"], {})
            qualification_tier = details.get("qualification_tier", "actionable")
            qualification_label = details.get(
                "qualification_label",
                "可直接警示" if qualification_tier == "actionable" else "候選觀察",
            ) or ("可直接警示" if qualification_tier == "actionable" else "候選觀察")
            suggested_action = (
                details.get("suggested_action")
                or details.get("action")
                or "請先人工覆核執行計畫，再決定是否下單。"
            )
            qualification_label = details.get("qualification_label") or (
                "可直接警示" if qualification_tier == "actionable" else "候選觀察"
            )
            suggested_action = (
                details.get("suggested_action")
                or details.get("action")
                or "請先人工覆核執行細節，再決定是否下單。"
            )
            opportunities.append(
                {
                    "opportunity_id": row["opportunity_id"],
                    "strategy_type": row["strategy_type"],
                    "direction": row["direction"],
                    "title": f"[{qualification_label}] {row['title']}",
                    "summary": details.get("summary") or details.get("note") or row["title"],
                    "market_slugs": self._load_json(row["market_slugs_json"], []),
                    "gross_edge": row["gross_edge"],
                    "net_edge": row["net_edge"],
                    "max_safe_size": row["max_safe_size"],
                    "available_liquidity": row["available_liquidity"],
                    "confidence_score": row["confidence_score"],
                    "prices": self._load_json(row["prices_json"], {}),
                    "details": details,
                    "suggested_action": f"{qualification_label}：{suggested_action}",
                    "created_at": row["created_at"],
                    "qualification_tier": qualification_tier,
                    "qualification_label": qualification_label,
                    "alert_eligible": bool(details.get("alert_eligible", qualification_tier == "actionable")),
                    "ranking_score": details.get("ranking_score", 0.0),
                }
            )
        return sorted(
            opportunities,
            key=lambda item: (
                item["alert_eligible"],
                item["ranking_score"],
                item["net_edge"],
                item["confidence_score"],
            ),
            reverse=True,
        )

    def recent_alerts(self, limit: int = 8) -> list[dict[str, Any]]:
        return self.connection.fetchall(
            """
            SELECT opportunity_id, channel, message, sent_at
            FROM alerts
            ORDER BY sent_at DESC
            LIMIT ?
            """,
            (limit,),
        )

    def strategy_summary(self, strategy_variant: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT strategy_type,
                   COUNT(*) AS count,
                   AVG(net_edge) AS avg_net_edge,
                   MAX(net_edge) AS best_net_edge
            FROM opportunities
        """
        params: list[Any] = []
        if strategy_variant == "near_close_maker":
            query += """
            WHERE strategy_type = 'late_resolution'
              AND details_json LIKE ?
            """
            params.append(self.NEAR_CLOSE_VARIANT_PATTERN)
        query += """
            GROUP BY strategy_type
            ORDER BY count DESC, best_net_edge DESC
            """
        return self.connection.fetchall(query, params)

    def top_markets(self, limit: int = 10, shortlist_only: bool = False) -> list[dict[str, Any]]:
        latest_scan = self.latest_scan_cycle()
        shortlist = latest_scan.get("shortlist_markets", [])
        if shortlist:
            return shortlist[:limit]
        if shortlist_only:
            return []
        return self.connection.fetchall(
            """
            SELECT question, slug, liquidity, discovered_at
            FROM markets
            WHERE active = TRUE AND closed = FALSE
            ORDER BY liquidity DESC, discovered_at DESC
            LIMIT ?
            """,
            (limit,),
        )
