from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import sleep, time
from typing import Any

import httpx

from app.alerts.console_alerts import ConsoleAlerts
from app.alerts.telegram_alerts import TelegramAlerts
from app.clients.clob_client import ClobClient
from app.clients.gamma_client import GammaClient
from app.clients.websocket_client import MarketWebSocketClient, OrderBookState
from app.config import Settings, get_settings
from app.models.runtime import TradingControls
from app.orchestration import (
    collect_previous_midpoints,
    execute_monitor_cycle,
    execute_scan_cycle,
    merge_timestamped_last_trade_observations,
    persist_monitor_cycle,
    persist_scan_cycle,
    shortlist_markets,
)
from app.scanners.liquidity_filter import LiquidityFilter
from app.services.preflight import PreflightReport, load_preflight_report
from app.services.redeemer import run_auto_redeem_once
from app.storage.backups import backup_sqlite_database
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.strategy.execution_planner import ExecutionPlanner, PaperTradeSimulator
from app.strategy.near_close_order_manager import NearCloseOrderManager
from app.strategy.near_close_stop_exit import execute_near_close_taker_exits, stop_exit_monitor_required
from app.strategy.polymarket_live_trading import PolymarketLiveTradingAdapter, resolve_funder_address
from app.strategy.post_fill_hedge import execute_post_fill_hedges
from app.strategy.post_fill_profit_take import execute_post_fill_profit_takes
from app.strategy.risk_manager import RiskManager
from app.utils.execution_utils import build_execution_claim_key
from app.utils.logging_utils import configure_logging, get_logger


logger = get_logger(__name__)
RUNTIME_LOG_DIR = Path(__file__).resolve().parent.parent / "runtime-logs"
WATCH_PID_FILE = RUNTIME_LOG_DIR / "watch.pid"
WATCH_LIVENESS_FILE = RUNTIME_LOG_DIR / "watch.liveness"
LIVE_FILL_ACTIVITY_LIMIT = 50
WATCH_AUXILIARY_TIMEOUT_SEC = 12.0
CRYPTO_UPDOWN_RESOLUTION_BUCKET_SEC = 300.0


def _read_pid(pid_file: Path) -> int | None:
    try:
        raw = pid_file.read_text(encoding="ascii").strip()
    except OSError:
        return None
    if not raw.isdigit():
        return None
    return int(raw)


def _pid_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


@contextlib.contextmanager
def _watch_pid_guard() -> None:
    RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    existing_pid = _read_pid(WATCH_PID_FILE)
    if existing_pid and existing_pid != current_pid and _pid_running(existing_pid):
        raise RuntimeError(f"watch is already running (pid {existing_pid}).")
    WATCH_PID_FILE.write_text(str(current_pid), encoding="ascii")
    try:
        yield
    finally:
        if _read_pid(WATCH_PID_FILE) == current_pid:
            with contextlib.suppress(OSError):
                WATCH_PID_FILE.unlink()


def _is_sqlite_lock_error(exc: Exception) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _touch_watch_liveness() -> None:
    with contextlib.suppress(OSError):
        RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        WATCH_LIVENESS_FILE.write_text(datetime.now(timezone.utc).isoformat(), encoding="ascii")


def _watch_heartbeat_details(settings: Settings, details: dict[str, Any] | None = None) -> dict[str, Any]:
    heartbeat_details = dict(details or {})
    heartbeat_details.setdefault("market_mode", settings.market_mode_payload())
    return heartbeat_details


def _monitored_markets_expired(monitored_markets: list[Any] | None, *, now: datetime | None = None) -> bool:
    if not monitored_markets:
        return False
    checked_at = now or datetime.now(timezone.utc)
    end_dates: list[datetime] = []
    for market in monitored_markets:
        end_date = getattr(market, "end_date", None)
        if not isinstance(end_date, datetime):
            return False
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        end_dates.append(end_date.astimezone(timezone.utc))
    return bool(end_dates) and all(end_date <= checked_at for end_date in end_dates)


def _crypto_updown_bucket_seconds_to_resolution(now: datetime | None = None) -> float:
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    timestamp = checked_at.astimezone(timezone.utc).timestamp()
    elapsed = timestamp % CRYPTO_UPDOWN_RESOLUTION_BUCKET_SEC
    return CRYPTO_UPDOWN_RESOLUTION_BUCKET_SEC - elapsed


def _watch_initial_scan_delay_sec(settings: Settings, *, now: datetime | None = None) -> float:
    if not (
        settings.near_close_maker_enabled
        and settings.near_close_scan_pool_enabled
        and settings.near_close_scan_crypto_updown_only
    ):
        return 0.0
    seconds_left = _crypto_updown_bucket_seconds_to_resolution(now)
    entry_min_seconds, entry_max_seconds = settings.near_close_entry_window_seconds()
    prewarm_seconds = max(float(settings.near_close_crypto_updown_prewarm_seconds), 0.0)
    prewarm_horizon = min(
        entry_max_seconds + prewarm_seconds,
        CRYPTO_UPDOWN_RESOLUTION_BUCKET_SEC,
    )
    if entry_min_seconds <= seconds_left <= prewarm_horizon:
        return 0.0
    if seconds_left > prewarm_horizon:
        return seconds_left - prewarm_horizon
    return seconds_left + CRYPTO_UPDOWN_RESOLUTION_BUCKET_SEC - prewarm_horizon


def _watch_delay_sec_for_near_close_pacing(
    settings: Settings,
    monitored_markets: list[Any] | None,
    *,
    now: datetime | None = None,
) -> float:
    base_delay = max(float(settings.scan_interval_sec), 0.0)
    if not (
        settings.near_close_maker_enabled
        and settings.near_close_scan_pool_enabled
        and settings.near_close_scan_crypto_updown_only
    ):
        return base_delay
    checked_at = now or datetime.now(timezone.utc)
    entry_min_seconds, entry_max_seconds = settings.near_close_entry_window_seconds()
    prewarm_seconds = max(float(settings.near_close_crypto_updown_prewarm_seconds), 0.0)
    fast_delay = max(float(settings.near_close_crypto_updown_fast_scan_sec), 0.5)
    if not monitored_markets:
        seconds_left = _crypto_updown_bucket_seconds_to_resolution(checked_at)
        prewarm_horizon = min(
            entry_max_seconds + prewarm_seconds,
            CRYPTO_UPDOWN_RESOLUTION_BUCKET_SEC,
        )
        if seconds_left > prewarm_horizon:
            return seconds_left - prewarm_horizon
        if seconds_left < entry_min_seconds:
            return seconds_left + CRYPTO_UPDOWN_RESOLUTION_BUCKET_SEC - prewarm_horizon
        next_boundary = entry_max_seconds if seconds_left > entry_max_seconds else entry_min_seconds
        return min(base_delay, max(seconds_left - next_boundary, 0.5))
    best_delay = base_delay
    for market in monitored_markets:
        end_date = getattr(market, "end_date", None)
        if not isinstance(end_date, datetime):
            continue
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        seconds_left = (end_date.astimezone(timezone.utc) - checked_at).total_seconds()
        if seconds_left <= 0 or seconds_left < entry_min_seconds:
            continue
        if entry_min_seconds <= seconds_left <= entry_max_seconds:
            best_delay = min(best_delay, fast_delay)
            continue
        if entry_max_seconds < seconds_left <= entry_max_seconds + prewarm_seconds:
            seconds_until_window = max(seconds_left - entry_max_seconds, 0.5)
            best_delay = min(best_delay, fast_delay, seconds_until_window)
    return best_delay


def _save_watch_heartbeat(
    settings: Settings,
    *,
    state: str,
    message: str,
    latest_scan_at: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    heartbeat_details = _watch_heartbeat_details(settings, details)
    for attempt in range(3):
        try:
            with closing(connect_db(settings)) as connection:
                ScannerRepository(connection).save_watch_heartbeat(
                    source="watch",
                    state=state,
                    latest_scan_at=latest_scan_at,
                    message=message,
                    details=heartbeat_details,
                )
            return
        except Exception as exc:
            if not _is_sqlite_lock_error(exc) or attempt == 2:
                logger.debug("watch heartbeat write skipped", context={"state": state, "error": str(exc)})
                return
            sleep(0.1 * (attempt + 1))


async def _watch_delay(
    settings: Settings,
    *,
    delay_sec: float | None = None,
    message: str = "watch delay before next scan",
    details: dict[str, Any] | None = None,
    monitor_callback: Any | None = None,
) -> None:
    actual_delay_sec = max(float(settings.scan_interval_sec if delay_sec is None else delay_sec), 0.0)
    delay_started_at = datetime.now(timezone.utc)
    delay_until = delay_started_at + timedelta(seconds=actual_delay_sec)
    loop = asyncio.get_running_loop()
    delay_deadline = loop.time() + actual_delay_sec
    _save_watch_heartbeat(
        settings,
        state="delay",
        message=message,
        latest_scan_at=None,
        details={
            "phase": "delay",
            "delay_started_at": delay_started_at.isoformat(),
            "delay_until": delay_until.isoformat(),
            "delay_sec": actual_delay_sec,
            "scan_timeout_sec": settings.watch_scan_timeout_sec,
            **(details or {}),
        },
    )
    while True:
        _touch_watch_liveness()
        remaining = delay_deadline - loop.time()
        if remaining <= 0:
            return
        monitor_interval = max(float(settings.near_close_open_position_monitor_sec), 0.5)
        if monitor_callback is not None:
            monitor_timeout = min(max(monitor_interval, 1.0), 3.0)
            monitor_task = asyncio.create_task(monitor_callback())
            try:
                done, _pending = await asyncio.wait({monitor_task}, timeout=monitor_timeout)
                if not done:
                    monitor_task.cancel()
                    raise TimeoutError
            except TimeoutError:
                logger.warning(
                    "Fast open-position monitor timed out during watch delay.",
                    context={"timeout_sec": monitor_timeout},
                )
            except Exception as exc:
                logger.warning(
                    "Fast open-position monitor failed during watch delay.",
                    context={"error": str(exc)},
                )
            finally:
                if not monitor_task.done():
                    monitor_task.cancel()
        sleep_for = min(remaining, monitor_interval)
        await asyncio.sleep(sleep_for)


def _is_near_close_opportunity(opportunity: object) -> bool:
    details = getattr(opportunity, "details", {})
    return isinstance(details, dict) and details.get("strategy_variant") == "near_close_maker"


def _split_cancel_response(order_ids: list[str], response: object) -> tuple[list[str], list[str]]:
    if not isinstance(response, dict):
        return order_ids, []
    canceled = response.get("canceled")
    if isinstance(canceled, list):
        canceled_ids = [str(order_id).strip() for order_id in canceled if str(order_id).strip()]
    else:
        canceled_ids = []
    not_canceled = response.get("not_canceled")
    if isinstance(not_canceled, dict):
        uncertain_ids = [str(order_id).strip() for order_id in not_canceled if str(order_id).strip()]
    else:
        uncertain_ids = []
    if not canceled_ids and not uncertain_ids:
        return order_ids, []
    return canceled_ids, uncertain_ids


def _slug_seconds_to_resolution(market_slug: str, *, at: datetime | None = None) -> float | None:
    end_ts = ScannerRepository._parse_slug_end_timestamp(str(market_slug or ""))
    if end_ts is None:
        return None
    checked_at = at or datetime.now(timezone.utc)
    return float(end_ts) - checked_at.timestamp()


def _near_close_order_cancel_reason(
    *,
    order: dict[str, Any],
    books: dict[str, Any],
    qualified_pairs: set[tuple[str, str]],
    settings: Settings,
    trigger: str,
) -> dict[str, Any]:
    market_slug = str(order.get("market_slug") or "")
    token_id = str(order.get("token_id") or "")
    response = order.get("response") if isinstance(order.get("response"), dict) else {}
    variant = str(response.get("near_close_variant") or response.get("variant") or "official")
    if response.get("market_filter_reason") == "crypto_updown_proxy_price_ready":
        variant = "crypto_updown"
    book = books.get(token_id)
    checked_at = datetime.now(timezone.utc)
    seconds_left = _slug_seconds_to_resolution(market_slug, at=checked_at)
    reasons: list[str] = []
    entry_price = float(order.get("target_price") or response.get("entry_bid") or response.get("entry_price") or 0.0)
    try:
        crypto_strike_distance = float(response.get("crypto_strike_distance"))
    except (TypeError, ValueError):
        crypto_strike_distance = None
    context: dict[str, Any] = {
        "trigger": trigger,
        "market_slug": market_slug,
        "token_id": token_id,
        "order_id": str(order.get("order_id") or ""),
        "entry_price": entry_price,
        "near_close_variant": variant,
        "cancel_reason_checked_at": checked_at.isoformat(),
        "time_to_resolution_sec": seconds_left,
        "qualified_pair": (market_slug, token_id) in qualified_pairs,
    }
    hard_cancel_seconds = max(float(settings.near_close_existing_order_hard_cancel_seconds), 0.0)
    context["existing_order_hard_cancel_seconds"] = hard_cancel_seconds
    if seconds_left is not None and seconds_left <= hard_cancel_seconds:
        reasons.append("existing_order_hard_cancel")
    if book is None:
        reasons.append("missing_orderbook")
    else:
        manager = NearCloseOrderManager(settings)
        reasons.extend(
            manager.entry_cancel_reasons(
                book=book,
                minutes_to_end=(seconds_left / 60.0) if seconds_left is not None else None,
                entry_price=entry_price,
                variant=variant,
                crypto_strike_distance=crypto_strike_distance,
            )
        )
        best_bid = book.best_bid
        best_ask = book.best_ask
        bid_depth = book.depth_for_side("bid", best_bid) if best_bid is not None else None
        ask_depth = book.depth_for_side("ask", best_ask) if best_ask is not None else None
        min_depth = settings.near_close_crypto_updown_min_depth if variant == "crypto_updown" else settings.near_close_min_depth
        if bid_depth is not None and bid_depth < min_depth:
            reasons.append("bid_depth_below_min")
        if variant == "crypto_updown" and best_bid is not None and best_bid >= settings.near_close_crypto_updown_skip_bid_at_or_above:
            reasons.append("bid_at_or_above_skip")
        context.update(
            {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "spread": book.spread,
                "midpoint": book.midpoint,
                "bid_depth_at_best": bid_depth,
                "ask_depth_at_best": ask_depth,
                "min_depth": min_depth,
            }
        )
    clean_reasons = list(dict.fromkeys(reason for reason in reasons if reason))
    if not clean_reasons:
        clean_reasons = ["scanner_criteria_not_passed"]
    return {
        "not_open_reason": clean_reasons[0],
        "not_open_reasons": clean_reasons,
        "cancel_reason_context": context,
    }


async def _hard_cancel_expiring_near_close_orders(
    *,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
    at: datetime | None = None,
) -> int:
    checked_at = at or datetime.now(timezone.utc)
    hard_cancel_seconds = max(float(settings.near_close_existing_order_hard_cancel_seconds), 0.0)
    due_orders: list[dict[str, Any]] = []
    for order in repository.near_close_active_orders_for_market():
        seconds_left = _slug_seconds_to_resolution(str(order.get("market_slug") or ""), at=checked_at)
        if seconds_left is None or seconds_left > hard_cancel_seconds:
            continue
        if str(order.get("order_id") or "").strip():
            due_orders.append(order)
    if not due_orders:
        return 0

    order_ids = [str(order["order_id"]).strip() for order in due_orders]
    cancel_reason_by_order: dict[str, dict[str, Any]] = {}
    for order in due_orders:
        order_id = str(order["order_id"]).strip()
        seconds_left = _slug_seconds_to_resolution(str(order.get("market_slug") or ""), at=checked_at)
        cancel_reason_by_order[order_id] = {
            "not_open_reason": "existing_order_hard_cancel",
            "not_open_reasons": ["existing_order_hard_cancel"],
            "cancel_reason_context": {
                "trigger": "independent_hard_cancel",
                "market_slug": str(order.get("market_slug") or ""),
                "token_id": str(order.get("token_id") or ""),
                "order_id": order_id,
                "time_to_resolution_sec": seconds_left,
                "existing_order_hard_cancel_seconds": hard_cancel_seconds,
                "cancel_reason_checked_at": checked_at.isoformat(),
            },
        }

    try:
        cancel_response = await live_trader.cancel_orders(order_ids)
    except Exception as exc:
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=None,
            status="hard_cancel_failed",
            message=str(exc),
            details={
                "order_ids": order_ids,
                "hard_cancel_seconds": hard_cancel_seconds,
                "checked_at": checked_at.isoformat(),
            },
        )
        raise

    canceled_ids, uncertain_ids = _split_cancel_response(order_ids, cancel_response)
    updated = repository.mark_live_orders_cancelled(
        canceled_ids,
        status="qualification_cancelled",
        cancel_response=cancel_response,
        cancel_reason_by_order=cancel_reason_by_order,
    )
    uncertain_updated = repository.mark_live_orders_cancelled(
        uncertain_ids,
        status="cancel_unconfirmed",
        cancel_response=cancel_response,
        cancel_reason_by_order=cancel_reason_by_order,
    )
    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id=None,
        status="hard_cancelled",
        message="Independent T-12 monitor cancelled expiring near-close maker orders.",
        details={
            "order_ids": order_ids,
            "canceled_order_ids": canceled_ids,
            "unconfirmed_order_ids": uncertain_ids,
            "updated_rows": updated,
            "unconfirmed_updated_rows": uncertain_updated,
            "hard_cancel_seconds": hard_cancel_seconds,
            "checked_at": checked_at.isoformat(),
            "cancel_response": cancel_response,
        },
    )
    return updated + uncertain_updated


async def _sync_live_fills_to_db(
    *,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
) -> int:
    funder_address = str(settings.polymarket_funder_address or "").strip()
    if not funder_address and (settings.polymarket_private_key or "").strip():
        funder_address = str(resolve_funder_address(settings, settings.polymarket_private_key or "") or "").strip()
    if not funder_address:
        return 0
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(
                f"{settings.polymarket_data_api_base_url.rstrip('/')}/activity",
                params={"user": funder_address, "limit": LIVE_FILL_ACTIVITY_LIMIT},
            )
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=None,
            status="fill_sync_failed",
            message=str(exc),
            details={"trigger": "watch_activity_fill_sync"},
        )
        return 0
    activities = [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []
    if not activities:
        return 0
    sync_task = asyncio.create_task(
        asyncio.to_thread(
            repository.save_polymarket_activity_trades,
            activities,
            wallet_address=funder_address,
            progress_callback=_touch_watch_liveness,
        )
    )
    while not sync_task.done():
        _touch_watch_liveness()
        await asyncio.wait({sync_task}, timeout=5.0)
    inserted = sync_task.result()
    if inserted:
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=None,
            status="live_fills_synced",
            message=f"Synced {inserted} public activity fill(s) before stop-exit checks.",
            details={"fill_count": inserted},
        )
    return inserted


_execute_near_close_taker_exits = execute_near_close_taker_exits


def _open_position_token_ids(repository: ScannerRepository, settings: Settings) -> list[str]:
    token_ids: list[str] = []
    for group in repository.near_close_stop_exit_groups(limit=50):
        if float(group.get("open_size") or 0.0) <= 1e-9:
            continue
        if not stop_exit_monitor_required(str(group.get("market_slug") or ""), settings):
            continue
        token_id = str(group.get("token_id") or "").strip()
        if token_id:
            token_ids.append(token_id)
    token_ids.extend(repository.near_close_hedge_watch_token_ids(limit=50))
    token_ids.extend(repository.near_close_profit_take_watch_token_ids(limit=50))
    return list(dict.fromkeys(token_ids))


async def _fetch_open_position_books(
    *,
    settings: Settings,
    repository: ScannerRepository,
) -> dict[str, object]:
    def load_token_ids() -> list[str]:
        with closing(connect_db(settings)) as connection:
            return _open_position_token_ids(ScannerRepository(connection), settings)

    token_ids = await asyncio.to_thread(load_token_ids)
    if not token_ids:
        return {}
    clob = ClobClient(
        settings.clob_base_url,
        timeout=settings.book_fetch_timeout_sec,
        concurrency=min(max(len(token_ids), 1), settings.book_fetch_concurrency),
        retries=settings.book_fetch_retries,
    )
    try:
        return await clob.get_order_books(token_ids)
    finally:
        await clob.close()


async def _manage_near_close_reprice(
    *,
    opportunity: object,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
) -> bool:
    """Return True when an existing active order should block a new submission."""

    if not _is_near_close_opportunity(opportunity):
        return False

    market_slugs = list(getattr(opportunity, "market_slugs", []) or [])
    token_ids = list(getattr(opportunity, "token_ids", []) or [])
    if not market_slugs or not token_ids:
        return False

    active_orders = repository.near_close_active_orders_for_market(
        market_slug=str(market_slugs[0]),
        token_id=str(token_ids[0]),
    )
    if not active_orders:
        return False

    prices = getattr(opportunity, "prices", {}) or {}
    details = getattr(opportunity, "details", {}) or {}
    try:
        target_price = float(prices.get("entry_bid") or details.get("entry_bid") or 0.0)
    except (TypeError, ValueError):
        return True
    if target_price <= 0:
        return True

    now_ts = time()
    stale_order_ids: list[str] = []
    held_orders: list[dict[str, object]] = []
    cancel_reason_by_order: dict[str, dict[str, Any]] = {}
    for order in active_orders:
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            held_orders.append(order)
            continue
        order_price = float(order.get("target_price") or 0.0)
        price_delta = abs(target_price - order_price)
        created_at_ts = float(order.get("created_at_ts") or 0.0)
        age_sec = now_ts - created_at_ts if created_at_ts > 0 else settings.near_close_reprice_cooldown_sec
        if price_delta < settings.near_close_reprice_threshold:
            held_orders.append(order)
            continue
        if age_sec < settings.near_close_reprice_cooldown_sec:
            held_orders.append(order)
            continue
        stale_order_ids.append(order_id)
        cancel_reason_by_order[order_id] = {
            "not_open_reason": "reprice_target_changed",
            "not_open_reasons": ["reprice_target_changed"],
            "cancel_reason_context": {
                "trigger": "reprice_cancel",
                "order_id": order_id,
                "market_slug": str(order.get("market_slug") or ""),
                "token_id": str(order.get("token_id") or ""),
                "old_entry_price": order_price,
                "new_entry_price": target_price,
                "price_delta": price_delta,
                "age_sec": age_sec,
            },
        }

    if not stale_order_ids:
        return True

    try:
        cancel_response = await live_trader.cancel_orders(stale_order_ids)
    except Exception as exc:
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=getattr(opportunity, "opportunity_id", None),
            status="reprice_cancel_failed",
            message=str(exc),
            details={
                "order_ids": stale_order_ids,
                "target_price": target_price,
            },
        )
        logger.warning(
            "Near-close reprice cancellation failed",
            context={"order_ids": stale_order_ids, "error": str(exc)},
        )
        return True

    canceled_ids, uncertain_ids = _split_cancel_response(stale_order_ids, cancel_response)
    updated = repository.mark_live_orders_cancelled(
        canceled_ids,
        status="reprice_cancelled",
        cancel_response=cancel_response,
        cancel_reason_by_order=cancel_reason_by_order,
    )
    uncertain_updated = repository.mark_live_orders_cancelled(
        uncertain_ids,
        status="cancel_unconfirmed",
        cancel_response=cancel_response,
        cancel_reason_by_order=cancel_reason_by_order,
    )
    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id=getattr(opportunity, "opportunity_id", None),
        status="reprice_cancelled",
        message="Cancelled stale near-close maker order before reprice.",
        details={
            "order_ids": stale_order_ids,
            "canceled_order_ids": canceled_ids,
            "unconfirmed_order_ids": uncertain_ids,
            "updated_rows": updated,
            "unconfirmed_updated_rows": uncertain_updated,
            "target_price": target_price,
            "cancel_response": cancel_response,
            "cancel_reason_by_order": cancel_reason_by_order,
        },
    )
    return bool(held_orders)


async def _cancel_unqualified_near_close_orders(
    *,
    cycle: object,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
) -> None:
    active_orders = repository.near_close_active_orders_for_market()
    if not active_orders:
        return

    qualified_pairs: set[tuple[str, str]] = set()
    for opportunity in getattr(cycle, "opportunities", []) or []:
        if not _is_near_close_opportunity(opportunity):
            continue
        market_slugs = list(getattr(opportunity, "market_slugs", []) or [])
        token_ids = list(getattr(opportunity, "token_ids", []) or [])
        if market_slugs and token_ids:
            qualified_pairs.add((str(market_slugs[0]), str(token_ids[0])))

    books = getattr(cycle, "books", {}) or {}
    cancel_ids: list[str] = []
    cancel_reason_by_order: dict[str, dict[str, Any]] = {}
    for order in active_orders:
        market_slug = str(order.get("market_slug") or "")
        token_id = str(order.get("token_id") or "")
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            continue
        if token_id not in books:
            continue
        if (market_slug, token_id) in qualified_pairs:
            continue
        cancel_reason = _near_close_order_cancel_reason(
            order=order,
            books=books,
            qualified_pairs=qualified_pairs,
            settings=settings,
            trigger="qualification_cancel",
        )
        reasons = set(cancel_reason.get("not_open_reasons") or [])
        seconds_left = cancel_reason.get("cancel_reason_context", {}).get("time_to_resolution_sec")
        hard_cancel_seconds = max(float(settings.near_close_existing_order_hard_cancel_seconds), 0.0)
        if (
            reasons == {"entry_after_window"}
            and seconds_left is not None
            and hard_cancel_seconds < float(seconds_left) < float(settings.near_close_entry_min_seconds)
        ):
            continue
        cancel_ids.append(order_id)
        cancel_reason_by_order[order_id] = cancel_reason

    if not cancel_ids:
        return

    try:
        cancel_response = await live_trader.cancel_orders(cancel_ids)
    except Exception as exc:
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=None,
            status="qualification_cancel_failed",
            message=str(exc),
            details={"order_ids": cancel_ids},
        )
        logger.warning(
            "Near-close qualification cancellation failed",
            context={"order_ids": cancel_ids, "error": str(exc)},
        )
        return

    canceled_ids, uncertain_ids = _split_cancel_response(cancel_ids, cancel_response)
    updated = repository.mark_live_orders_cancelled(
        canceled_ids,
        status="qualification_cancelled",
        cancel_response=cancel_response,
        cancel_reason_by_order=cancel_reason_by_order,
    )
    uncertain_updated = repository.mark_live_orders_cancelled(
        uncertain_ids,
        status="cancel_unconfirmed",
        cancel_response=cancel_response,
        cancel_reason_by_order=cancel_reason_by_order,
    )
    repository.save_execution_event(
        source="watch",
        mode="live",
        opportunity_id=None,
        status="qualification_cancelled",
        message="Cancelled near-close maker orders that no longer pass scanner criteria.",
        details={
            "order_ids": cancel_ids,
            "canceled_order_ids": canceled_ids,
            "unconfirmed_order_ids": uncertain_ids,
            "updated_rows": updated,
            "unconfirmed_updated_rows": uncertain_updated,
            "cancel_response": cancel_response,
            "cancel_reason_by_order": cancel_reason_by_order,
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket mispricing scanner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in (
        "discover",
        "scan",
        "watch",
        "backfill",
        "report",
        "report-trade-autopsy",
        "report-cancel-autopsy",
        "research-near-close",
        "serve",
        "maintain-db",
        "backup-db",
    ):
        subparser = subparsers.add_parser(command, help=f"Run {command} command")
        if command in {"discover", "scan", "watch", "backfill"}:
            subparser.add_argument("--limit", type=int, default=None, help="Max events to process")
        if command == "research-near-close":
            subparser.add_argument(
                "--all-signals",
                action="store_true",
                help="Do not dedupe repeated scans for the same market/token/time bucket",
            )
            subparser.add_argument(
                "--no-refresh-settlements",
                action="store_true",
                help="Skip Gamma refresh for current settlement/outcomePrices before reporting",
            )
        if command == "serve":
            subparser.add_argument("--reload", action="store_true", help="Enable local auto reload")
        if command == "maintain-db":
            subparser.add_argument("--vacuum", action="store_true", help="Force SQLite VACUUM")
        if command == "backup-db":
            subparser.add_argument("--label", default="manual", help="Backup filename label")
    return parser


async def cmd_discover(settings: Settings, args: argparse.Namespace) -> None:
    console = ConsoleAlerts()
    with closing(connect_db(settings)) as connection:
        repository = ScannerRepository(connection)
        result = await execute_scan_cycle(settings, limit=args.limit, repository=repository)
        repository.save_markets(result.events, result.markets)
    console.show_discovery_summary(result.events, result.markets)
    console.show_markets(result.shortlisted_markets[:10])


async def cmd_scan(settings: Settings, args: argparse.Namespace) -> None:
    console = ConsoleAlerts()
    with closing(connect_db(settings)) as connection:
        repository = ScannerRepository(connection)
        result = await execute_scan_cycle(settings, limit=args.limit, repository=repository)
        persist_scan_cycle(repository, result, settings)
        try:
            redeem_results = run_auto_redeem_once(settings, repository)
        except Exception as exc:
            repository.save_execution_event(
                source="auto-redeem",
                mode="live",
                opportunity_id=None,
                status="failed",
                message=str(exc),
                details={"trigger": "scan"},
            )
            logger.warning("Auto redeem failed during scan", context={"error": str(exc)})
        else:
            for redeem_result in redeem_results:
                if redeem_result.status == "redeemed":
                    console.print_message(
                        f"Redeemed {redeem_result.market_slug} / {redeem_result.outcome_label}: "
                        f"{redeem_result.redeemed_size:.4f} shares."
                    )
    console.show_discovery_summary(result.events, result.markets)
    console.show_opportunities(result.opportunities)


async def cmd_watch(settings: Settings, args: argparse.Namespace) -> None:
    with _watch_pid_guard():
        await _cmd_watch_impl(settings, args)


async def _cmd_watch_impl(settings: Settings, args: argparse.Namespace) -> None:
    _touch_watch_liveness()
    console = ConsoleAlerts()
    telegram = TelegramAlerts(settings.telegram_bot_token, settings.telegram_chat_id)
    paper = PaperTradeSimulator(settings.fees_bps)
    live_trader = PolymarketLiveTradingAdapter(settings)
    default_controls = TradingControls.from_settings(settings)
    preflight_cache: PreflightReport | None = None
    preflight_cache_at = 0.0
    websocket_client: MarketWebSocketClient | None = None
    websocket_task: asyncio.Task[None] | None = None
    subscribed_asset_ids: list[str] = []
    book_state = OrderBookState()
    fast_monitor_lock = Lock()
    last_redeem_loop_time = 0.0

    async def get_preflight(*, force: bool = False) -> PreflightReport:
        nonlocal preflight_cache, preflight_cache_at
        loop_time = asyncio.get_running_loop().time()
        if not force and preflight_cache is not None and (loop_time - preflight_cache_at) < settings.preflight_cache_sec:
            return preflight_cache
        preflight_cache = await load_preflight_report(settings, verify_clob_credentials=force)
        preflight_cache_at = loop_time
        return preflight_cache

    async def stop_websocket() -> None:
        nonlocal websocket_client, websocket_task
        if websocket_client is not None:
            await websocket_client.stop()
        if websocket_task is not None:
            websocket_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await websocket_task
        websocket_client = None
        websocket_task = None

    async def ensure_websocket(asset_ids: list[str]) -> None:
        nonlocal websocket_client, websocket_task, subscribed_asset_ids
        normalized_asset_ids = list(asset_ids)
        if normalized_asset_ids == subscribed_asset_ids:
            return
        await stop_websocket()
        subscribed_asset_ids = normalized_asset_ids
        if not normalized_asset_ids:
            return
        websocket_client = MarketWebSocketClient(settings.ws_market_url, book_state.handle_message)
        websocket_task = asyncio.create_task(websocket_client.subscribe_forever(normalized_asset_ids))

    async def run_fast_monitor_worker(
        last_trade_observations: dict[str, tuple[float | None, datetime | None, str | None]],
    ) -> None:
        with closing(connect_db(settings)) as connection:
            repository = ScannerRepository(connection)
            controls = repository.get_trading_controls(default_controls)
            if not controls.armed:
                return
            runtime_settings = controls.apply(settings)
            token_ids = _open_position_token_ids(repository, runtime_settings)
        if not token_ids:
            return

        clob = ClobClient(
            runtime_settings.clob_base_url,
            timeout=runtime_settings.book_fetch_timeout_sec,
            concurrency=min(max(len(token_ids), 1), runtime_settings.book_fetch_concurrency),
            retries=runtime_settings.book_fetch_retries,
        )
        try:
            open_position_books = await clob.get_order_books(token_ids)
        finally:
            await clob.close()
        if not open_position_books:
            return
        merge_timestamped_last_trade_observations(open_position_books, last_trade_observations)

        worker_live_trader = PolymarketLiveTradingAdapter(runtime_settings)
        with closing(connect_db(settings)) as connection:
            repository = ScannerRepository(connection)
            await execute_near_close_taker_exits(
                repository=repository,
                live_trader=worker_live_trader,
                settings=runtime_settings,
                watch_books=open_position_books,
            )

    def run_fast_monitor_worker_sync(
        last_trade_observations: dict[str, tuple[float | None, datetime | None, str | None]],
    ) -> None:
        if not fast_monitor_lock.acquire(blocking=False):
            return
        try:
            asyncio.run(run_fast_monitor_worker(last_trade_observations))
        except Exception as exc:
            logger.warning("Fast open-position monitor failed", context={"error": str(exc)})
            with contextlib.suppress(Exception):
                with closing(connect_db(settings)) as connection:
                    ScannerRepository(connection).save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="fast_position_monitor_failed",
                        message=str(exc),
                        details={
                            "monitor_interval_sec": settings.near_close_open_position_monitor_sec,
                            "trigger_price": settings.near_close_taker_exit_price,
                        },
                    )
        finally:
            fast_monitor_lock.release()

    async def monitor_open_positions_once() -> None:
        _touch_watch_liveness()
        monitor_interval = max(float(settings.near_close_open_position_monitor_sec), 0.5)
        monitor_timeout = min(max(monitor_interval, 1.0), 3.0)
        last_trade_observations = {
            token_id: (snapshot.last_trade_price, snapshot.last_trade_at, snapshot.last_trade_side)
            for token_id, snapshot in book_state.books.items()
            if snapshot.last_trade_at is not None
        }
        task = asyncio.create_task(
            asyncio.to_thread(run_fast_monitor_worker_sync, last_trade_observations)
        )
        done, _pending = await asyncio.wait({task}, timeout=monitor_timeout)
        if not done:
            raise TimeoutError
        try:
            task.result()
        except Exception as exc:
            logger.warning("Fast open-position monitor failed", context={"error": str(exc)})
            with contextlib.suppress(Exception):
                with closing(connect_db(settings)) as connection:
                    ScannerRepository(connection).save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="fast_position_monitor_failed",
                        message=str(exc),
                        details={
                            "monitor_interval_sec": settings.near_close_open_position_monitor_sec,
                            "trigger_price": settings.near_close_taker_exit_price,
                        },
                    )

    async def monitor_open_positions_with_budget(phase: str) -> None:
        monitor_interval = max(float(settings.near_close_open_position_monitor_sec), 0.5)
        monitor_timeout = min(max(monitor_interval, 1.0), 3.0)
        try:
            await asyncio.wait_for(monitor_open_positions_once(), timeout=monitor_timeout)
        except TimeoutError:
            logger.warning(
                "Fast open-position monitor timed out",
                context={"phase": phase, "timeout_sec": monitor_timeout},
            )
            with contextlib.suppress(Exception):
                with closing(connect_db(settings)) as connection:
                    ScannerRepository(connection).save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="fast_position_monitor_timeout",
                        message=f"Fast open-position monitor exceeded {monitor_timeout:.1f}s during {phase}.",
                        details={
                            "phase": phase,
                            "monitor_interval_sec": settings.near_close_open_position_monitor_sec,
                            "timeout_sec": monitor_timeout,
                            "trigger_price": settings.near_close_taker_exit_price,
                        },
                    )

    async def run_independent_hard_cancel_loop() -> None:
        poll_sec = min(max(float(settings.near_close_open_position_monitor_sec), 0.5), 1.0)
        hard_cancel_trader = PolymarketLiveTradingAdapter(settings)
        while True:
            _touch_watch_liveness()
            try:
                with closing(connect_db(settings)) as connection:
                    repository = ScannerRepository(connection)
                    controls = repository.get_trading_controls(default_controls)
                    hard_cancel_trader.settings = controls.apply(settings)
                    await _hard_cancel_expiring_near_close_orders(
                        repository=repository,
                        live_trader=hard_cancel_trader,
                        settings=hard_cancel_trader.settings,
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Independent near-close hard cancel failed", context={"error": str(exc)})
            await asyncio.sleep(poll_sec)

    async def run_scan_cycle_with_budget(
        *,
        loop_started_at: float,
        previous_midpoints: dict[str, float] | None = None,
        monitored_markets: list[Any] | None = None,
        shortlist_diagnostics: dict[str, object] | None = None,
    ) -> Any:
        async def run_scan() -> Any:
            last_trade_observations = {
                token_id: (snapshot.last_trade_price, snapshot.last_trade_at, snapshot.last_trade_side)
                for token_id, snapshot in book_state.books.items()
                if snapshot.last_trade_at is not None
            }
            with closing(connect_db(settings)) as connection:
                repository = ScannerRepository(connection)
                if monitored_markets is not None:
                    return await execute_monitor_cycle(
                        settings,
                        monitored_markets,
                        previous_midpoints=previous_midpoints,
                        shortlist_diagnostics=shortlist_diagnostics,
                        last_trade_observations=last_trade_observations,
                    )
                return await execute_scan_cycle(
                    settings,
                    limit=args.limit,
                    previous_midpoints=previous_midpoints,
                    repository=repository,
                    last_trade_observations=last_trade_observations,
                )

        task = asyncio.create_task(run_scan())
        try:
            while not task.done():
                _touch_watch_liveness()
                now = asyncio.get_running_loop().time()
                remaining = settings.watch_scan_timeout_sec - (now - loop_started_at)
                if remaining <= 0:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                        await asyncio.wait_for(task, timeout=2.0)
                    raise TimeoutError
                done, _pending = await asyncio.wait({task}, timeout=min(1.0, remaining))
                if done:
                    break
            return task.result()
        finally:
            if not task.done():
                task.cancel()

    async def wait_for_watch_auxiliary(awaitable: Any) -> Any:
        task = asyncio.create_task(awaitable)
        try:
            deadline = asyncio.get_running_loop().time() + WATCH_AUXILIARY_TIMEOUT_SEC
            while not task.done():
                _touch_watch_liveness()
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                        await asyncio.wait_for(task, timeout=2.0)
                    raise TimeoutError
                done, _pending = await asyncio.wait({task}, timeout=min(5.0, remaining))
                if done:
                    break
            return task.result()
        finally:
            if not task.done():
                task.cancel()

    hard_cancel_task = asyncio.create_task(run_independent_hard_cancel_loop())
    initial_schedule_delay_sec = _watch_initial_scan_delay_sec(settings)
    if initial_schedule_delay_sec > 0:
        await _watch_delay(
            settings,
            delay_sec=initial_schedule_delay_sec,
            message="watch waiting for next crypto Up/Down bucket prewarm",
            details={
                "phase": "bucket_idle",
                "near_close_bucket_aligned": True,
                "next_scan_delay_sec": initial_schedule_delay_sec,
            },
            monitor_callback=monitor_open_positions_once,
        )

    while True:
        _touch_watch_liveness()
        scan_started_at = datetime.now(timezone.utc)
        loop_started_at = asyncio.get_running_loop().time()
        _save_watch_heartbeat(
            settings,
            state="scanning",
            message="watch initial scan started",
            details={
                "phase": "scanning",
                "scan_started_at": scan_started_at.isoformat(),
                "timeout_sec": settings.watch_scan_timeout_sec,
                "delay_sec": settings.scan_interval_sec,
            },
        )
        try:
            initial = await run_scan_cycle_with_budget(loop_started_at=loop_started_at)
            with closing(connect_db(settings)) as connection:
                repository = ScannerRepository(connection)
                for snapshot in initial.books.values():
                    book_state.upsert_snapshot(snapshot)
                try:
                    repository.save_watch_heartbeat(
                        source="watch",
                        state="running",
                        latest_scan_at=initial.executed_at,
                        message="watch initial scan completed",
                        details=_watch_heartbeat_details(settings, {
                            "phase": "completed",
                            "scan_started_at": scan_started_at.isoformat(),
                            "scan_completed_at": initial.executed_at.isoformat(),
                            "delay_sec": settings.scan_interval_sec,
                            "scan_timeout_sec": settings.watch_scan_timeout_sec,
                            "monitored_markets": len(initial.shortlisted_markets),
                            "book_count": len(initial.books),
                            "opportunity_count": len(initial.opportunities),
                        }),
                    )
                    persist_monitor_cycle(
                        repository,
                        initial,
                        discovered_market_count=len(initial.markets),
                    )
                except Exception as exc:
                    if not _is_sqlite_lock_error(exc):
                        raise
                    logger.warning("watch initial persistence skipped because SQLite is locked: %s", exc)
                if settings.watch_live_fill_sync_enabled:
                    try:
                        await wait_for_watch_auxiliary(
                            _sync_live_fills_to_db(repository=repository, live_trader=live_trader, settings=settings)
                        )
                    except TimeoutError:
                        repository.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=None,
                            status="live_fill_sync_timeout",
                            message=(
                                f"Live fill sync exceeded {WATCH_AUXILIARY_TIMEOUT_SEC:.0f}s after scan; "
                                "continuing watch loop."
                            ),
                            details={"timeout_sec": WATCH_AUXILIARY_TIMEOUT_SEC},
                        )
                try:
                    open_position_books = await wait_for_watch_auxiliary(
                        _fetch_open_position_books(settings=settings, repository=repository)
                    )
                except TimeoutError:
                    open_position_books = {}
                    repository.save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="open_position_scan_timeout",
                        message=(
                            f"Open-position book refresh exceeded {WATCH_AUXILIARY_TIMEOUT_SEC:.0f}s after scan; "
                            "continuing watch loop."
                        ),
                        details={"timeout_sec": WATCH_AUXILIARY_TIMEOUT_SEC},
                    )
                for snapshot in open_position_books.values():
                    book_state.upsert_snapshot(snapshot)
                repository.get_trading_controls(default_controls)
            break
        except TimeoutError:
            with contextlib.suppress(Exception):
                with closing(connect_db(settings)) as connection:
                    ScannerRepository(connection).save_watch_heartbeat(
                        source="watch",
                        state="timeout",
                        message=(
                            f"watch initial scan exceeded {settings.watch_scan_timeout_sec:.0f}s; "
                            f"abandoned and delaying {settings.watch_timeout_retry_sec:.0f}s before the next scan."
                        ),
                        details=_watch_heartbeat_details(settings, {
                            "phase": "timeout",
                            "scan_started_at": scan_started_at.isoformat(),
                            "timeout_sec": settings.watch_scan_timeout_sec,
                            "delay_sec": settings.watch_timeout_retry_sec,
                        }),
                    )
            await _watch_delay(
                settings,
                delay_sec=settings.watch_timeout_retry_sec,
                message="watch initial scan timed out; delaying before the next scan",
                details={"previous_phase": "timeout", "scan_started_at": scan_started_at.isoformat()},
            )
        except Exception as exc:
            logger.warning("watch initial scan failed", context={"error": str(exc)})
            with contextlib.suppress(Exception):
                with closing(connect_db(settings)) as connection:
                    ScannerRepository(connection).save_watch_heartbeat(
                        source="watch",
                        state="error",
                        message="watch initial scan failed; delaying before the next scan.",
                        details=_watch_heartbeat_details(settings, {
                            "phase": "error",
                            "scan_started_at": scan_started_at.isoformat(),
                            "error": str(exc),
                            "delay_sec": settings.watch_timeout_retry_sec,
                        }),
                    )
            await _watch_delay(
                settings,
                delay_sec=settings.watch_timeout_retry_sec,
                message="watch initial scan failed; delaying before the next scan",
                details={"previous_phase": "error", "scan_started_at": scan_started_at.isoformat()},
            )
    console.show_discovery_summary(initial.events, initial.markets)
    console.show_opportunities(initial.opportunities[:10])
    await ensure_websocket(list(book_state.books.keys()))

    try:
        previous_midpoints = collect_previous_midpoints(book_state.books)
        monitored_markets = list(initial.shortlisted_markets)
        monitored_shortlist_diagnostics = dict(initial.shortlist_diagnostics)
        while True:
            current_delay_sec = _watch_delay_sec_for_near_close_pacing(settings, monitored_markets)
            await _watch_delay(
                settings,
                delay_sec=current_delay_sec,
                details={
                    "near_close_pacing_delay_sec": current_delay_sec,
                    "near_close_bucket_aligned": settings.near_close_scan_crypto_updown_only,
                },
                monitor_callback=monitor_open_positions_once,
            )
            _touch_watch_liveness()
            scan_started_at = datetime.now(timezone.utc)
            loop_started_at = asyncio.get_running_loop().time()
            loop_time = loop_started_at
            refresh_monitored_markets = _monitored_markets_expired(monitored_markets)
            _save_watch_heartbeat(
                settings,
                state="scanning",
                message="watch scan started",
                details={
                    "phase": "scanning",
                    "scan_started_at": scan_started_at.isoformat(),
                    "timeout_sec": settings.watch_scan_timeout_sec,
                    "delay_sec": current_delay_sec,
                    "refresh_discovery": refresh_monitored_markets,
                },
            )
            with closing(connect_db(settings)) as connection:
                repository = ScannerRepository(connection)
                try:
                    cycle = await run_scan_cycle_with_budget(
                        loop_started_at=loop_started_at,
                        previous_midpoints=previous_midpoints,
                        monitored_markets=None if refresh_monitored_markets else monitored_markets or None,
                        shortlist_diagnostics=(
                            None
                            if refresh_monitored_markets
                            else monitored_shortlist_diagnostics if monitored_markets else None
                        ),
                    )
                except TimeoutError:
                    with contextlib.suppress(Exception):
                        repository.save_watch_heartbeat(
                            source="watch",
                            state="timeout",
                            message=(
                                f"watch scan exceeded {settings.watch_scan_timeout_sec:.0f}s; "
                                f"abandoned and delaying {settings.watch_timeout_retry_sec:.0f}s before the next scan."
                            ),
                            details=_watch_heartbeat_details(settings, {
                                "phase": "timeout",
                                "scan_started_at": scan_started_at.isoformat(),
                                "timeout_sec": settings.watch_scan_timeout_sec,
                                "delay_sec": settings.watch_timeout_retry_sec,
                            }),
                        )
                    await _watch_delay(
                        settings,
                        delay_sec=settings.watch_timeout_retry_sec,
                        message="watch scan timed out; delaying before the next scan",
                        details={"previous_phase": "timeout", "scan_started_at": scan_started_at.isoformat()},
                    )
                    continue
                except Exception as exc:
                    logger.warning("watch scan failed", context={"error": str(exc)})
                    with contextlib.suppress(Exception):
                        repository.save_watch_heartbeat(
                            source="watch",
                            state="error",
                            message="watch scan failed; delaying before the next scan.",
                            details=_watch_heartbeat_details(settings, {
                                "phase": "error",
                                "scan_started_at": scan_started_at.isoformat(),
                                "error": str(exc),
                                "delay_sec": settings.watch_timeout_retry_sec,
                            }),
                        )
                    await _watch_delay(
                        settings,
                        delay_sec=settings.watch_timeout_retry_sec,
                        message="watch scan failed; delaying before the next scan",
                        details={"previous_phase": "error", "scan_started_at": scan_started_at.isoformat()},
                    )
                    continue
                try:
                    persist_monitor_cycle(
                        repository,
                        cycle,
                        discovered_market_count=len(cycle.markets),
                    )
                except Exception as exc:
                    if not _is_sqlite_lock_error(exc):
                        raise
                    logger.warning("watch persistence skipped because SQLite is locked: %s", exc)
                console.show_discovery_summary(cycle.events, cycle.markets)
                try:
                    repository.save_watch_heartbeat(
                        source="watch",
                        state="running",
                        latest_scan_at=scan_started_at,
                        message="watch scan completed",
                        details=_watch_heartbeat_details(settings, {
                            "phase": "completed",
                            "scan_started_at": scan_started_at.isoformat(),
                            "scan_completed_at": cycle.executed_at.isoformat(),
                            "delay_sec": current_delay_sec,
                            "scan_timeout_sec": settings.watch_scan_timeout_sec,
                            "refresh_discovery": refresh_monitored_markets,
                            "monitored_markets": len(cycle.shortlisted_markets),
                            "book_count": len(cycle.books),
                            "opportunity_count": len(cycle.opportunities),
                        }),
                    )
                except Exception as exc:
                    if not _is_sqlite_lock_error(exc):
                        raise
                    logger.warning("watch heartbeat skipped because SQLite is locked: %s", exc)

                last_trade_observations = {
                    token_id: (snapshot.last_trade_price, snapshot.last_trade_at, snapshot.last_trade_side)
                    for token_id, snapshot in book_state.books.items()
                    if snapshot.last_trade_at is not None
                }
                # Refresh the monitored universe while preserving timestamped trade evidence.
                book_state.books = {}
                for snapshot in cycle.books.values():
                    book_state.upsert_snapshot(snapshot)
                merge_timestamped_last_trade_observations(book_state.books, last_trade_observations)
                if cycle.books:
                    monitored_markets = list(cycle.shortlisted_markets)
                    monitored_shortlist_diagnostics = dict(cycle.shortlist_diagnostics)
                else:
                    monitored_markets = []
                    monitored_shortlist_diagnostics = {}

                controls = repository.get_trading_controls(default_controls)
                runtime_settings = controls.apply(settings)
                if runtime_settings.watch_live_fill_sync_enabled:
                    try:
                        await wait_for_watch_auxiliary(
                            _sync_live_fills_to_db(
                                repository=repository,
                                live_trader=live_trader,
                                settings=runtime_settings,
                            )
                        )
                    except TimeoutError:
                        repository.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=None,
                            status="live_fill_sync_timeout",
                            message=(
                                f"Live fill sync exceeded {WATCH_AUXILIARY_TIMEOUT_SEC:.0f}s after scan; "
                                "continuing to opportunity execution."
                            ),
                            details={
                                "timeout_sec": WATCH_AUXILIARY_TIMEOUT_SEC,
                                "scan_started_at": scan_started_at.isoformat(),
                            },
                        )
                expired_submissions = repository.expire_stale_live_submissions(older_than_sec=300.0)
                if expired_submissions:
                    repository.save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="submission_reconciliation_failed",
                        message=(
                            f"Marked {expired_submissions} unresolved submission(s) failed after market end."
                        ),
                        details={"expired_count": expired_submissions},
                    )
                unresolved_submissions = repository.unresolved_live_submissions(older_than_sec=30.0)
                if unresolved_submissions and controls.auto_execute_enabled:
                    controls = repository.save_trading_controls(
                        TradingControls(
                            live_trading_enabled=controls.live_trading_enabled,
                            auto_execute_enabled=False,
                            kill_switch_enabled=controls.kill_switch_enabled,
                        )
                    )
                    runtime_settings = controls.apply(settings)
                    repository.save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="submission_reconciliation_required",
                        message="Unresolved live submission detected; auto execution was disabled.",
                        details={
                            "pending_count": len(unresolved_submissions),
                            "opportunity_ids": [
                                str(item.get("opportunity_id") or "") for item in unresolved_submissions
                            ],
                        },
                    )
                try:
                    open_position_books = await wait_for_watch_auxiliary(
                        _fetch_open_position_books(settings=runtime_settings, repository=repository)
                    )
                except TimeoutError:
                    open_position_books = {}
                    repository.save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="open_position_scan_timeout",
                        message=(
                            f"Open-position book refresh exceeded {WATCH_AUXILIARY_TIMEOUT_SEC:.0f}s after scan; "
                            "continuing to opportunity execution."
                        ),
                        details={
                            "timeout_sec": WATCH_AUXILIARY_TIMEOUT_SEC,
                            "scan_started_at": scan_started_at.isoformat(),
                        },
                    )
                for snapshot in open_position_books.values():
                    book_state.upsert_snapshot(snapshot)
                await ensure_websocket(list(book_state.books.keys()))
                previous_midpoints = collect_previous_midpoints(book_state.books)
                planner = ExecutionPlanner(max_leg_size=runtime_settings.live_max_order_size)
                risk_manager = RiskManager(runtime_settings)
                liquidity_filter = LiquidityFilter(runtime_settings)
                console.show_opportunities(cycle.opportunities[:10])

                preflight: PreflightReport | None = None
                if controls.armed:
                    preflight = await get_preflight(force=False)
                    if runtime_settings.require_live_preflight and not preflight.ready:
                        controls = repository.save_trading_controls(
                            TradingControls(
                                live_trading_enabled=controls.live_trading_enabled,
                                auto_execute_enabled=False,
                                kill_switch_enabled=controls.kill_switch_enabled,
                            )
                        )
                        repository.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=None,
                            status="preflight_blocked",
                            message="Live 交易前置檢查未通過，已暫停自動下單。",
                            details={"blocking_reasons": preflight.blocking_reasons},
                        )
                        console.print_message("Live 交易前置檢查未通過，已暫停自動下單。")
                        runtime_settings = controls.apply(settings)
                        risk_manager = RiskManager(runtime_settings)
                        liquidity_filter = LiquidityFilter(runtime_settings)

                live_trader.settings = runtime_settings
                if (
                    runtime_settings.auto_redeem_enabled
                    and (loop_time - last_redeem_loop_time) >= runtime_settings.auto_redeem_refresh_sec
                ):
                    last_redeem_loop_time = loop_time
                    try:
                        redeem_results = run_auto_redeem_once(runtime_settings, repository)
                    except Exception as exc:
                        repository.save_execution_event(
                            source="auto-redeem",
                            mode="live",
                            opportunity_id=None,
                            status="failed",
                            message=str(exc),
                            details={},
                        )
                        logger.warning("Auto redeem failed", context={"error": str(exc)})
                    else:
                        for redeem_result in redeem_results:
                            if redeem_result.status == "redeemed":
                                console.print_message(
                                    f"Redeemed {redeem_result.market_slug} / {redeem_result.outcome_label}: "
                                    f"{redeem_result.redeemed_size:.4f} shares."
                                )
                try:
                    await execute_post_fill_hedges(
                        repository=repository,
                        live_trader=live_trader,
                        settings=runtime_settings,
                        controls=controls,
                        watch_books=book_state.books,
                        source="watch",
                    )
                except Exception as exc:
                    repository.save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="hedge_loop_failed",
                        message=str(exc),
                        details={},
                    )
                    logger.warning("Post-fill hedge loop failed", context={"error": str(exc)})
                if controls.armed and (preflight is None or preflight.ready):
                    await _cancel_unqualified_near_close_orders(
                        cycle=cycle,
                        repository=repository,
                        live_trader=live_trader,
                        settings=runtime_settings,
                    )
                    try:
                        await execute_near_close_taker_exits(
                            repository=repository,
                            live_trader=live_trader,
                            settings=runtime_settings,
                            watch_books=book_state.books,
                        )
                    except Exception as exc:
                        repository.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=None,
                            status="stop_exit_failed",
                            message=str(exc),
                            details={"trigger_price": runtime_settings.near_close_taker_exit_price},
                        )
                        logger.warning("Near-close taker exit failed", context={"error": str(exc)})
                    try:
                        await execute_post_fill_profit_takes(
                            repository=repository,
                            live_trader=live_trader,
                            settings=runtime_settings,
                            controls=controls,
                            watch_books=book_state.books,
                            source="watch",
                        )
                    except Exception as exc:
                        repository.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=None,
                            status="profit_take_loop_failed",
                            message=str(exc),
                            details={},
                        )
                        logger.warning("Post-fill profit-take loop failed", context={"error": str(exc)})

                for opportunity in cycle.opportunities:
                    if not liquidity_filter.is_alert_eligible(opportunity):
                        continue
                    is_near_close = _is_near_close_opportunity(opportunity)
                    alerted_recently = repository.was_alerted_recently(
                        opportunity.opportunity_id,
                        runtime_settings.alert_cooldown_sec,
                    )
                    if alerted_recently and not is_near_close:
                        continue

                    plan = planner.build_plan(opportunity)
                    if not alerted_recently:
                        console.print_alert(opportunity)
                        repository.save_alert(opportunity.opportunity_id, "console", opportunity.summary)

                    if telegram.enabled and not alerted_recently:
                        try:
                            await telegram.send(opportunity)
                            repository.save_alert(opportunity.opportunity_id, "telegram", opportunity.summary)
                        except Exception:
                            logger.warning("Failed to send Telegram alert", context={"opportunity_id": opportunity.opportunity_id})

                    if settings.enable_paper_trading and not alerted_recently:
                        paper_risk = risk_manager.assess(plan, repository, mode="paper")
                        if paper_risk.allowed:
                            result = paper.simulate(
                                plan,
                                book_state.books,
                                opportunity.details.get("locked_profit_per_share", opportunity.net_edge),
                            )
                            repository.save_paper_trade(result)
                        else:
                            repository.save_execution_event(
                                source="watch",
                                mode="paper",
                                opportunity_id=opportunity.opportunity_id,
                                status="risk_blocked",
                                message=paper_risk.reason,
                                details={
                                    "estimated_notional": paper_risk.estimated_notional,
                                    "projected_daily_notional": paper_risk.projected_daily_notional,
                                    "projected_daily_orders": paper_risk.projected_daily_orders,
                                },
                            )
                            logger.info(
                                "Paper execution skipped by risk manager",
                                context={
                                    "opportunity_id": opportunity.opportunity_id,
                                    "reason": paper_risk.reason,
                                    "estimated_notional": paper_risk.estimated_notional,
                                },
                            )

                    if not controls.armed or (preflight is not None and not preflight.ready):
                        continue
                    if not plan.live_trading_allowed:
                        continue
                    if is_near_close:
                        active_order_blocks_submission = await _manage_near_close_reprice(
                            opportunity=opportunity,
                            repository=repository,
                            live_trader=live_trader,
                            settings=runtime_settings,
                        )
                        if active_order_blocks_submission:
                            continue

                    live_risk = risk_manager.assess(plan, repository, mode="live")
                    if not live_risk.allowed:
                        repository.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=opportunity.opportunity_id,
                            status="risk_blocked",
                            message=live_risk.reason,
                            details={
                                "estimated_notional": live_risk.estimated_notional,
                                "projected_daily_notional": live_risk.projected_daily_notional,
                                "projected_daily_orders": live_risk.projected_daily_orders,
                            },
                        )
                        logger.warning(
                            "Live execution blocked by risk manager",
                            context={
                                "opportunity_id": opportunity.opportunity_id,
                                "reason": live_risk.reason,
                                "estimated_notional": live_risk.estimated_notional,
                            },
                        )
                        continue

                    claim_key = build_execution_claim_key(opportunity, mode="live")
                    claimed = repository.claim_execution(
                        claim_key=claim_key,
                        opportunity_id=opportunity.opportunity_id,
                        source="watch",
                        mode="live",
                        message="Execution claimed by watch loop.",
                    )
                    if not claimed:
                        repository.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=opportunity.opportunity_id,
                            status="duplicate_claim",
                            message="相同機會已被其他 worker 接手執行。",
                            details={"claim_key": claim_key},
                            claim_key=claim_key,
                        )
                        continue

                    repository.save_live_submission_pending(
                        plan,
                        claim_key=claim_key,
                        source="watch",
                    )
                    try:
                        live_result = await live_trader.execute(plan)
                    except Exception as exc:
                        repository.mark_live_submission_failed(opportunity.opportunity_id, str(exc))
                        repository.update_execution_claim(claim_key=claim_key, status="failed", message=str(exc))
                        repository.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=opportunity.opportunity_id,
                            status="failed",
                            message=str(exc),
                            details={"claim_key": claim_key},
                            claim_key=claim_key,
                        )
                        logger.warning(
                            "Live trading execution failed",
                            context={
                                "opportunity_id": opportunity.opportunity_id,
                                "error": str(exc),
                            },
                        )
                        continue

                    repository.update_execution_claim(
                        claim_key=claim_key,
                        status=live_result.status,
                        message=live_result.message,
                    )
                    if live_result.leg_results:
                        repository.save_live_execution(live_result)
                    else:
                        repository.mark_live_submission_failed(opportunity.opportunity_id, live_result.message)
                    repository.save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=opportunity.opportunity_id,
                        status=live_result.status,
                        message=live_result.message,
                        details={
                            "claim_key": claim_key,
                            "legs": [leg.model_dump() for leg in live_result.leg_results],
                        },
                        claim_key=claim_key,
                    )

                    logger.info(
                        "Live trading execution finished",
                        context={
                            "opportunity_id": opportunity.opportunity_id,
                            "status": live_result.status,
                            "legs": len(live_result.leg_results),
                        },
                    )

                    if live_result.status == "partial_failure":
                        controls = repository.save_trading_controls(
                            TradingControls(
                                live_trading_enabled=False,
                                auto_execute_enabled=False,
                                kill_switch_enabled=True,
                            )
                        )
                        incident_message = (
                            f"Live partial failure: {opportunity.opportunity_id} "
                            "已觸發 kill switch，請立刻人工檢查未成交 / 已送出委託。"
                        )
                        console.print_message(incident_message)
                        if telegram.enabled:
                            with contextlib.suppress(Exception):
                                await telegram.send_text(incident_message)
                        break
    finally:
        hard_cancel_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hard_cancel_task
        await stop_websocket()


async def cmd_backfill(settings: Settings, args: argparse.Namespace) -> None:
    console = ConsoleAlerts()
    with closing(connect_db(settings)) as connection:
        repository = ScannerRepository(connection)
        result = await execute_scan_cycle(settings, limit=args.limit, repository=repository)
        repository.save_markets(result.events, result.markets)
        repository.save_orderbooks(result.books.values())
    console.show_discovery_summary(result.events, result.markets)


def cmd_report(settings: Settings) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    with closing(connect_db(settings)) as connection:
        repository = ScannerRepository(connection)

        top_rows = repository.top_opportunities_today()
        top_table = Table(title="Today's Best Opportunities")
        top_table.add_column("Title")
        top_table.add_column("Strategy")
        top_table.add_column("Net Edge", justify="right")
        top_table.add_column("Liquidity", justify="right")
        for row in top_rows:
            top_table.add_row(row["title"], row["strategy_type"], f"{row['net_edge']:.3%}", f"{row['available_liquidity']:,.0f}")
        console.print(top_table)

        hit_table = Table(title="Strategy Hit Rate")
        hit_table.add_column("Strategy")
        hit_table.add_column("Paper Trades", justify="right")
        hit_table.add_column("Hit Rate", justify="right")
        for row in repository.strategy_hit_rate():
            hit_table.add_row(row["strategy_type"], str(row["total_paper_trades"]), f"{row['hit_rate']:.0%}")
        console.print(hit_table)

        latest_scan = repository.latest_scan_cycle()
        if latest_scan:
            scan_table = Table(title="Latest Scan Coverage")
            scan_table.add_column("Executed At")
            scan_table.add_column("Discovered", justify="right")
            scan_table.add_column("Monitored", justify="right")
            scan_table.add_column("Books", justify="right")
            scan_table.add_column("Opportunities", justify="right")
            scan_table.add_column("Actionable", justify="right")
            scan_table.add_column("Candidate", justify="right")
            scan_table.add_row(
                latest_scan["executed_at"],
                str(latest_scan["discovered_market_count"]),
                str(latest_scan["monitored_market_count"]),
                str(latest_scan["book_count"]),
                str(latest_scan["opportunity_count"]),
                str(latest_scan["actionable_count"]),
                str(latest_scan["candidate_count"]),
            )
            console.print(scan_table)

            bucket_table = Table(title="Watch Pool Composition")
            bucket_table.add_column("Bucket")
            bucket_table.add_column("Count", justify="right")
            for bucket_name, count in latest_scan.get("watch_bucket_counts", {}).items():
                bucket_table.add_row(bucket_name, str(count))
            console.print(bucket_table)

            reason_table = Table(title="Shortlist Diagnostics")
            reason_table.add_column("Metric")
            reason_table.add_column("Value", justify="right")
            reason_table.add_row("Excluded long-tail", str(latest_scan.get("excluded_long_tail_count", 0)))
            reason_table.add_row("Excluded family cap", str(latest_scan.get("excluded_family_cap_count", 0)))
            reason_table.add_row(
                "Positive-edge candidates (24h)",
                str(latest_scan.get("positive_edge_candidates_24h", 0)),
            )
            console.print(reason_table)

        risk_summary = repository.trading_risk_summary()
        risk_table = Table(title="Risk Summary (Today)")
        risk_table.add_column("Mode")
        risk_table.add_column("Count", justify="right")
        risk_table.add_column("Notional", justify="right")
        risk_table.add_column("Fees", justify="right")
        risk_table.add_row(
            "Paper",
            str(risk_summary["paper_trades_today"]),
            f"{risk_summary['paper_notional_today']:.2f}",
            f"{risk_summary['paper_fees_today']:.2f}",
        )
        risk_table.add_row(
            "Live",
            str(risk_summary["live_orders_today"]),
            f"{risk_summary['live_notional_today']:.2f}",
            "-",
        )
        console.print(risk_table)

        entry_bucket_table = Table(title="Near-close Entry Bucket Performance (Live)")
        entry_bucket_table.add_column("Bucket")
        entry_bucket_table.add_column("Trades", justify="right")
        entry_bucket_table.add_column("Realized", justify="right")
        entry_bucket_table.add_column("Win Rate", justify="right")
        entry_bucket_table.add_column("Avg Entry", justify="right")
        entry_bucket_table.add_column("Avg PnL", justify="right")
        entry_bucket_table.add_column("Max Loss", justify="right")
        entry_bucket_table.add_column("Avg Spread", justify="right")
        entry_bucket_table.add_column("Avg Start Dist", justify="right")

        def fmt_value(value: object, *, precision: int = 4, percent: bool = False) -> str:
            if value is None:
                return "N/A"
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return "N/A"
            if percent:
                return f"{numeric:.0%}"
            return f"{numeric:.{precision}f}"

        for row in repository.near_close_entry_bucket_report():
            entry_bucket_table.add_row(
                str(row["bucket"]),
                str(row["trade_count"]),
                str(row["realized_trade_count"]),
                fmt_value(row["win_rate"], percent=True),
                fmt_value(row["average_entry_price"]),
                fmt_value(row["average_realized_pnl"]),
                fmt_value(row["max_loss"]),
                fmt_value(row["average_spread"]),
                fmt_value(row["average_crypto_start_distance"], precision=6),
            )
        console.print(entry_bucket_table)

        avg_pnl = repository.average_realized_pnl()
        latency = repository.alert_to_fill_latency()
        console.print(f"Average paper PnL: {avg_pnl if avg_pnl is not None else 'N/A'}")
        console.print(f"Alert to fill latency: {latency if latency is not None else 'N/A'} sec")


def cmd_report_trade_autopsy(settings: Settings) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    with closing(connect_db(settings)) as connection:
        rows = ScannerRepository(connection).trade_autopsy_report(limit=20)

    table = Table(title="Trade Autopsy Report")
    table.add_column("Autopsy ID")
    table.add_column("Market")
    table.add_column("Outcome")
    table.add_column("Entry", justify="right")
    table.add_column("Fill", justify="right")
    table.add_column("Stop Checks", justify="right")
    table.add_column("Last Stop")
    table.add_column("Exit")
    table.add_column("Win")
    table.add_column("PnL", justify="right")
    for row in rows:
        entry = row.get("entry_price")
        fill = row.get("actual_fill_price")
        pnl = row.get("realized_pnl")
        table.add_row(
            str(row.get("trade_autopsy_id") or ""),
            str(row.get("market_slug") or ""),
            str(row.get("outcome") or ""),
            "N/A" if entry is None else f"{float(entry):.4f}",
            "N/A" if fill is None else f"{float(fill):.4f}",
            str(row.get("stop_check_count") or 0),
            str(row.get("last_stop_reason") or "N/A"),
            str(row.get("exit_status") or ("attempted" if row.get("exit_attempted") else "N/A")),
            "N/A" if row.get("did_bought_outcome_win") is None else ("yes" if row.get("did_bought_outcome_win") else "no"),
            "N/A" if pnl is None else f"{float(pnl):.4f}",
        )
    console.print(table)


def cmd_report_cancel_autopsy(settings: Settings) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    with closing(connect_db(settings)) as connection:
        report = ScannerRepository(connection).cancel_autopsy_report(limit=30)

    rows = report.get("rows") if isinstance(report, dict) else []
    table = Table(title="Cancel Autopsy Report")
    table.add_column("Cancel ID")
    table.add_column("Market")
    table.add_column("Outcome")
    table.add_column("Reason")
    table.add_column("Fillability")
    table.add_column("Entry", justify="right")
    table.add_column("Size", justify="right")
    table.add_column("Win")
    table.add_column("Hold PnL", justify="right")
    table.add_column("Quality")
    for row in rows if isinstance(rows, list) else []:
        entry = row.get("entry_price")
        size = row.get("size")
        pnl = row.get("hypothetical_hold_pnl")
        table.add_row(
            str(row.get("cancel_autopsy_id") or ""),
            str(row.get("market_slug") or ""),
            str(row.get("outcome") or ""),
            str(row.get("cancel_reason") or ""),
            str(row.get("fillability") or "unknown"),
            "N/A" if entry is None else f"{float(entry):.4f}",
            "N/A" if size is None else f"{float(size):.2f}",
            "N/A"
            if row.get("did_bought_outcome_win") is None
            else ("yes" if row.get("did_bought_outcome_win") else "no"),
            "N/A" if pnl is None else f"{float(pnl):.4f}",
            str(row.get("cancel_quality") or ""),
        )
    console.print(table)

    summary = report.get("by_reason") if isinstance(report, dict) else []
    summary_table = Table(title="Cancel Autopsy by Reason")
    summary_table.add_column("Reason")
    summary_table.add_column("Count", justify="right")
    summary_table.add_column("Settled", justify="right")
    summary_table.add_column("Good", justify="right")
    summary_table.add_column("Bad", justify="right")
    summary_table.add_column("Likely Fill", justify="right")
    summary_table.add_column("Hold PnL", justify="right")
    summary_table.add_column("Weighted PnL", justify="right")
    for row in summary if isinstance(summary, list) else []:
        summary_table.add_row(
            str(row.get("cancel_reason") or ""),
            str(row.get("count") or 0),
            str(row.get("settled_count") or 0),
            str(row.get("good_cancel_count") or 0),
            str(row.get("bad_cancel_count") or 0),
            str(row.get("likely_fill_count") or 0),
            f"{float(row.get('hypothetical_hold_pnl_total') or 0.0):.4f}",
            f"{float(row.get('fillability_weighted_hold_pnl_total') or 0.0):.4f}",
        )
    console.print(summary_table)


async def _refresh_near_close_research_markets(
    settings: Settings,
    repository: ScannerRepository,
    *,
    console: Any,
    limit: int = 300,
) -> int:
    slugs = repository.near_close_signal_market_slugs(limit=limit)
    if not slugs:
        return 0
    gamma = GammaClient(
        settings.gamma_base_url,
        timeout=settings.gamma_timeout_sec,
        retries=settings.gamma_retries,
    )
    refreshed = 0
    try:
        for slug in slugs:
            event = None
            try:
                payload = await gamma._get("/markets", params={"slug": slug})
            except Exception:
                payload = None
            market_payload: dict[str, Any] | None = None
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                market_payload = payload[0]
            elif isinstance(payload, dict):
                market_payload = payload
            if not market_payload:
                try:
                    event_payload = await gamma._get("/events", params={"slug": slug})
                except Exception:
                    event_payload = None
                if isinstance(event_payload, list) and event_payload and isinstance(event_payload[0], dict):
                    event = gamma.normalise_event(event_payload[0])
                    markets = event_payload[0].get("markets") or []
                    for candidate in markets:
                        if isinstance(candidate, dict) and str(candidate.get("slug") or "") == slug:
                            market_payload = candidate
                            break
            if not market_payload:
                continue
            market = gamma.normalise_market(market_payload, event=event)
            repository.save_markets([event] if event is not None else [], [market])
            refreshed += 1
    finally:
        await gamma.close()
    console.print(f"Refreshed {refreshed}/{len(slugs)} near-close market settlement snapshots from Gamma.")
    return refreshed


async def cmd_research_near_close(settings: Settings, args: argparse.Namespace) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()

    def fmt_value(value: object, *, precision: int = 4, percent: bool = False) -> str:
        if value is None:
            return "N/A"
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return "N/A"
        if percent:
            return f"{numeric:.0%}"
        return f"{numeric:.{precision}f}"

    def print_table(title: str, rows: list[dict[str, object]]) -> None:
        table = Table(title=title)
        table.add_column("Group")
        table.add_column("Signals", justify="right")
        table.add_column("Resolved", justify="right")
        table.add_column("Unresolved", justify="right")
        table.add_column("Win Rate", justify="right")
        table.add_column("Avg Entry", justify="right")
        table.add_column("EV / Share", justify="right")
        table.add_column("Max Loss / Share", justify="right")
        table.add_column("Avg Spread", justify="right")
        table.add_column("Avg Start Dist", justify="right")
        table.add_column("Avg Depth", justify="right")
        for row in rows:
            table.add_row(
                str(row["group"]),
                str(row["sample_count"]),
                str(row["resolved_count"]),
                str(row["unresolved_count"]),
                fmt_value(row["win_rate"], percent=True),
                fmt_value(row["average_entry_price"]),
                fmt_value(row["average_ev_per_share"]),
                fmt_value(row["max_loss_per_share"]),
                fmt_value(row["average_spread"]),
                fmt_value(row["average_crypto_start_distance"], precision=6),
                fmt_value(row["average_depth"], precision=2),
            )
        console.print(table)

    with closing(connect_db(settings)) as connection:
        repository = ScannerRepository(connection)
        if not args.no_refresh_settlements:
            await _refresh_near_close_research_markets(settings, repository, console=console)
        rows = repository.near_close_signal_replay_report(dedupe=not args.all_signals)

    console.print(
        "[bold]Near-close Crypto Up/Down Signal Replay[/bold] "
        f"({'all repeated scan signals' if args.all_signals else 'deduped by market/token/time bucket'})"
    )
    console.print(
        "This is a research replay from stored opportunities and settled market outcomes; "
        "it does not prove historical fillability or panic-exit execution."
    )
    for group_type, title in (
        ("overall", "Overall"),
        ("time_bucket", "By Time To Resolution"),
        ("asset", "By Asset"),
        ("entry_price", "By Entry Price"),
        ("start_distance", "By Crypto Start Distance"),
        ("spread", "By Spread"),
        ("depth", "By Bid Depth At Best"),
    ):
        group_rows = [row for row in rows if row["group_type"] == group_type]
        if group_rows:
            print_table(title, group_rows)


def cmd_maintain_db(settings: Settings, args: argparse.Namespace) -> None:
    from rich.console import Console

    console = Console()
    with closing(connect_db(settings)) as connection:
        repository = ScannerRepository(connection)
        result = repository.run_database_maintenance(
            raw_retention_days=settings.db_raw_retention_days,
            snapshot_retention_days=settings.db_snapshot_retention_days,
            maintenance_interval_sec=settings.db_maintenance_interval_sec,
            vacuum_interval_sec=settings.db_vacuum_interval_sec,
            force=True,
            force_vacuum=bool(args.vacuum),
        )
    console.print(result)


def cmd_backup_db(settings: Settings, args: argparse.Namespace) -> None:
    from rich.console import Console

    result = backup_sqlite_database(settings, label=args.label)
    Console().print(result)


def cmd_serve(settings: Settings, args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run(
        "app.web:app",
        host=settings.web_host,
        port=settings.port,
        reload=args.reload,
    )


async def async_main(args: argparse.Namespace, settings: Settings) -> None:
    if args.command == "discover":
        await cmd_discover(settings, args)
    elif args.command == "scan":
        await cmd_scan(settings, args)
    elif args.command == "watch":
        await cmd_watch(settings, args)
    elif args.command == "backfill":
        await cmd_backfill(settings, args)
    elif args.command == "report":
        cmd_report(settings)
    elif args.command == "report-trade-autopsy":
        cmd_report_trade_autopsy(settings)
    elif args.command == "report-cancel-autopsy":
        cmd_report_cancel_autopsy(settings)
    elif args.command == "research-near-close":
        await cmd_research_near_close(settings, args)
    elif args.command == "maintain-db":
        cmd_maintain_db(settings, args)
    elif args.command == "backup-db":
        cmd_backup_db(settings, args)


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level)
    logger.info("Starting application", context={"command": args.command})
    if args.command == "serve":
        cmd_serve(settings, args)
        return
    asyncio.run(async_main(args, settings))


if __name__ == "__main__":
    main()
