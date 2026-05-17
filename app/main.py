from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from time import sleep, time
from typing import Any

from app.alerts.console_alerts import ConsoleAlerts
from app.alerts.telegram_alerts import TelegramAlerts
from app.clients.clob_client import ClobClient
from app.clients.websocket_client import MarketWebSocketClient, OrderBookState
from app.config import Settings, get_settings
from app.models.core import ExecutionLeg, ExecutionPlan
from app.models.runtime import TradingControls
from app.orchestration import (
    collect_previous_midpoints,
    execute_scan_cycle,
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
from app.strategy.polymarket_live_trading import PolymarketLiveTradingAdapter
from app.strategy.near_close_order_manager import NearCloseOrderManager
from app.strategy.risk_manager import RiskManager
from app.utils.execution_utils import build_execution_claim_key
from app.utils.logging_utils import configure_logging, get_logger


logger = get_logger(__name__)
RUNTIME_LOG_DIR = Path(__file__).resolve().parent.parent / "runtime-logs"
WATCH_PID_FILE = RUNTIME_LOG_DIR / "watch.pid"
WATCH_LIVENESS_FILE = RUNTIME_LOG_DIR / "watch.liveness"


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


def _save_watch_heartbeat(
    settings: Settings,
    *,
    state: str,
    message: str,
    latest_scan_at: datetime | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    for attempt in range(3):
        try:
            with closing(connect_db(settings)) as connection:
                ScannerRepository(connection).save_watch_heartbeat(
                    source="watch",
                    state=state,
                    latest_scan_at=latest_scan_at,
                    message=message,
                    details=details,
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
    message: str = "watch delay before next scan",
    details: dict[str, Any] | None = None,
) -> None:
    delay_started_at = datetime.now(timezone.utc)
    delay_until_ts = time() + settings.watch_timeout_retry_sec
    delay_until = datetime.fromtimestamp(delay_until_ts, tz=timezone.utc)
    _save_watch_heartbeat(
        settings,
        state="delay",
        message=message,
        latest_scan_at=None,
        details={
            "phase": "delay",
            "delay_started_at": delay_started_at.isoformat(),
            "delay_until": delay_until.isoformat(),
            "delay_sec": settings.watch_timeout_retry_sec,
            "scan_timeout_sec": settings.watch_scan_timeout_sec,
            **(details or {}),
        },
    )
    while True:
        _touch_watch_liveness()
        remaining = delay_until_ts - time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 5.0))


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


async def _execute_near_close_taker_exits(
    *,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
    settings: Settings,
    watch_books: dict[str, object],
) -> list[dict[str, object]]:
    manager = NearCloseOrderManager(settings)
    exits: list[dict[str, object]] = []
    for group in repository.near_close_stop_exit_groups(limit=50):
        if float(group.get("open_size") or 0.0) <= 1e-9:
            continue
        token_id = str(group.get("token_id") or "")
        market_slug = str(group.get("market_slug") or "")
        if "updown" not in market_slug:
            continue
        book = watch_books.get(token_id)
        if book is None or not manager.taker_exit_required(book=book):
            continue
        target_price = manager.taker_exit_price(book=book)
        if target_price is None or target_price <= 0:
            continue
        size = float(group.get("open_size") or 0.0)
        plan = ExecutionPlan(
            opportunity_id=f"stop-exit:{market_slug}:{token_id}",
            summary=f"Taker stop exit on {market_slug} at {target_price:.4f}",
            legs=[
                ExecutionLeg(
                    action="SELL",
                    token_id=token_id,
                    market_slug=market_slug,
                    outcome_label=str(group.get("outcome_label") or "Outcome"),
                    target_price=target_price,
                    size=size,
                    order_type="FAK",
                    post_only=False,
                    metadata={
                        "strategy_variant": "near_close_stop_exit",
                        "stop_trigger_price": settings.near_close_taker_exit_price,
                        "stop_reference_price": target_price,
                    },
                )
            ],
            max_slippage_bps=10.0,
            cancel_conditions=["Stop exit should take immediately available liquidity."],
            requires_manual_approval=False,
            live_trading_allowed=True,
            strategy_type="near_close_stop_exit",
            metadata={"market_slug": market_slug, "token_id": token_id},
        )
        live_result = await live_trader.execute(plan)
        repository.save_live_execution(live_result)
        repository.save_execution_event(
            source="watch",
            mode="live",
            opportunity_id=plan.opportunity_id,
            status=live_result.status,
            message=live_result.message,
            details={
                "stop_trigger_price": settings.near_close_taker_exit_price,
                "reference_price": target_price,
                "market_slug": market_slug,
                "token_id": token_id,
                "legs": [leg.model_dump() for leg in live_result.leg_results],
            },
        )
        exits.append(
            {
                "market_slug": market_slug,
                "token_id": token_id,
                "status": live_result.status,
                "reference_price": target_price,
                "size": size,
            }
        )
    return exits


def _open_position_token_ids(repository: ScannerRepository) -> list[str]:
    token_ids: list[str] = []
    for group in repository.near_close_stop_exit_groups(limit=50):
        if float(group.get("open_size") or 0.0) <= 1e-9:
            continue
        token_id = str(group.get("token_id") or "").strip()
        if token_id:
            token_ids.append(token_id)
    return list(dict.fromkeys(token_ids))


async def _fetch_open_position_books(
    *,
    settings: Settings,
    repository: ScannerRepository,
) -> dict[str, object]:
    token_ids = _open_position_token_ids(repository)
    if not token_ids:
        return {}
    clob = ClobClient(
        settings.clob_base_url,
        concurrency=min(max(len(token_ids), 1), settings.book_fetch_concurrency),
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
    updated = repository.mark_live_orders_cancelled(canceled_ids, status="reprice_cancelled")
    uncertain_updated = repository.mark_live_orders_cancelled(
        uncertain_ids,
        status="cancel_unconfirmed",
        cancel_response=cancel_response,
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
        },
    )
    return bool(held_orders)


async def _cancel_unqualified_near_close_orders(
    *,
    cycle: object,
    repository: ScannerRepository,
    live_trader: PolymarketLiveTradingAdapter,
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
        cancel_ids.append(order_id)

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
    updated = repository.mark_live_orders_cancelled(canceled_ids, status="qualification_cancelled")
    uncertain_updated = repository.mark_live_orders_cancelled(
        uncertain_ids,
        status="cancel_unconfirmed",
        cancel_response=cancel_response,
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
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket mispricing scanner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("discover", "scan", "watch", "backfill", "report", "serve", "maintain-db", "backup-db"):
        subparser = subparsers.add_parser(command, help=f"Run {command} command")
        if command in {"discover", "scan", "watch", "backfill"}:
            subparser.add_argument("--limit", type=int, default=None, help="Max events to process")
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

    async def wait_with_scan_budget(awaitable: Any, loop_started_at: float) -> Any:
        remaining = settings.watch_scan_timeout_sec - (asyncio.get_running_loop().time() - loop_started_at)
        if remaining <= 0:
            raise TimeoutError
        return await asyncio.wait_for(awaitable, timeout=remaining)

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
                "delay_sec": settings.watch_timeout_retry_sec,
            },
        )
        try:
            with closing(connect_db(settings)) as connection:
                repository = ScannerRepository(connection)
                initial = await wait_with_scan_budget(
                    execute_scan_cycle(settings, limit=args.limit, repository=repository),
                    loop_started_at,
                )
                for snapshot in initial.books.values():
                    book_state.upsert_snapshot(snapshot)
                open_position_books = await wait_with_scan_budget(
                    _fetch_open_position_books(settings=settings, repository=repository),
                    loop_started_at,
                )
                for snapshot in open_position_books.values():
                    book_state.upsert_snapshot(snapshot)
                repository.get_trading_controls(default_controls)
                try:
                    persist_scan_cycle(repository, initial, settings)
                    repository.save_watch_heartbeat(
                        source="watch",
                        state="running",
                        latest_scan_at=initial.executed_at,
                        message="watch initial scan completed",
                        details={
                            "phase": "completed",
                            "scan_started_at": scan_started_at.isoformat(),
                            "scan_completed_at": initial.executed_at.isoformat(),
                            "delay_sec": settings.watch_timeout_retry_sec,
                            "scan_timeout_sec": settings.watch_scan_timeout_sec,
                            "monitored_markets": len(initial.shortlisted_markets),
                            "book_count": len(initial.books),
                            "opportunity_count": len(initial.opportunities),
                        },
                    )
                except Exception as exc:
                    if not _is_sqlite_lock_error(exc):
                        raise
                    logger.warning("watch initial persistence skipped because SQLite is locked: %s", exc)
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
                        details={
                            "phase": "timeout",
                            "scan_started_at": scan_started_at.isoformat(),
                            "timeout_sec": settings.watch_scan_timeout_sec,
                            "delay_sec": settings.watch_timeout_retry_sec,
                        },
                    )
            await _watch_delay(
                settings,
                message="watch initial scan timed out; delaying before the next scan",
                details={"previous_phase": "timeout", "scan_started_at": scan_started_at.isoformat()},
            )
    console.show_discovery_summary(initial.events, initial.markets)
    console.show_opportunities(initial.opportunities[:10])
    await ensure_websocket(list(book_state.books.keys()))

    try:
        previous_midpoints = collect_previous_midpoints(book_state.books)
        while True:
            await _watch_delay(settings)
            _touch_watch_liveness()
            scan_started_at = datetime.now(timezone.utc)
            loop_started_at = asyncio.get_running_loop().time()
            loop_time = loop_started_at
            _save_watch_heartbeat(
                settings,
                state="scanning",
                message="watch scan started",
                details={
                    "phase": "scanning",
                    "scan_started_at": scan_started_at.isoformat(),
                    "timeout_sec": settings.watch_scan_timeout_sec,
                    "delay_sec": settings.watch_timeout_retry_sec,
                },
            )
            with closing(connect_db(settings)) as connection:
                repository = ScannerRepository(connection)
                try:
                    cycle = await wait_with_scan_budget(
                        execute_scan_cycle(
                            settings,
                            limit=args.limit,
                            previous_midpoints=previous_midpoints,
                            repository=repository,
                        ),
                        loop_started_at,
                    )
                except TimeoutError:
                    repository.save_watch_heartbeat(
                        source="watch",
                        state="timeout",
                        message=(
                            f"watch scan exceeded {settings.watch_scan_timeout_sec:.0f}s; "
                            f"abandoned and delaying {settings.watch_timeout_retry_sec:.0f}s before the next scan."
                        ),
                        details={
                            "phase": "timeout",
                            "scan_started_at": scan_started_at.isoformat(),
                            "timeout_sec": settings.watch_scan_timeout_sec,
                            "delay_sec": settings.watch_timeout_retry_sec,
                        },
                    )
                    await _watch_delay(
                        settings,
                        message="watch scan timed out; delaying before the next scan",
                        details={"previous_phase": "timeout", "scan_started_at": scan_started_at.isoformat()},
                    )
                    continue
                try:
                    persist_scan_cycle(repository, cycle, settings)
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
                        details={
                            "phase": "completed",
                            "scan_started_at": scan_started_at.isoformat(),
                            "scan_completed_at": cycle.executed_at.isoformat(),
                            "delay_sec": settings.watch_timeout_retry_sec,
                            "scan_timeout_sec": settings.watch_scan_timeout_sec,
                            "refresh_discovery": True,
                            "monitored_markets": len(cycle.shortlisted_markets),
                            "book_count": len(cycle.books),
                            "opportunity_count": len(cycle.opportunities),
                        },
                    )
                except Exception as exc:
                    if not _is_sqlite_lock_error(exc):
                        raise
                    logger.warning("watch heartbeat skipped because SQLite is locked: %s", exc)

                # Refresh the monitored universe every cycle so watch pool follows the latest shortlist.
                book_state.books = {}
                for snapshot in cycle.books.values():
                    book_state.upsert_snapshot(snapshot)

                controls = repository.get_trading_controls(default_controls)
                runtime_settings = controls.apply(settings)
                try:
                    open_position_books = await wait_with_scan_budget(
                        _fetch_open_position_books(settings=runtime_settings, repository=repository),
                        loop_started_at,
                    )
                except TimeoutError:
                    repository.save_watch_heartbeat(
                        source="watch",
                        state="timeout",
                        message=(
                            f"watch open-position scan exceeded {settings.watch_scan_timeout_sec:.0f}s; "
                            f"abandoned and delaying {settings.watch_timeout_retry_sec:.0f}s before the next scan."
                        ),
                        details={
                            "phase": "timeout",
                            "scan_started_at": scan_started_at.isoformat(),
                            "timeout_sec": settings.watch_scan_timeout_sec,
                            "delay_sec": settings.watch_timeout_retry_sec,
                        },
                    )
                    await _watch_delay(
                        settings,
                        message="watch open-position scan timed out; delaying before the next scan",
                        details={"previous_phase": "timeout", "scan_started_at": scan_started_at.isoformat()},
                    )
                    continue
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
                if controls.armed and (preflight is None or preflight.ready):
                    await _cancel_unqualified_near_close_orders(
                        cycle=cycle,
                        repository=repository,
                        live_trader=live_trader,
                    )
                    try:
                        await _execute_near_close_taker_exits(
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

                    try:
                        live_result = await live_trader.execute(plan)
                    except Exception as exc:
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
                    repository.save_live_execution(live_result)
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

        avg_pnl = repository.average_realized_pnl()
        latency = repository.alert_to_fill_latency()
        console.print(f"Average paper PnL: {avg_pnl if avg_pnl is not None else 'N/A'}")
        console.print(f"Alert to fill latency: {latency if latency is not None else 'N/A'} sec")


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
