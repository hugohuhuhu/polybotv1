from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.models.runtime import TradingControls
from app.orchestration import execute_scan_cycle, persist_scan_cycle
from app.scanners.liquidity_filter import LiquidityFilter
from app.services.preflight import load_preflight_report
from app.services.wallet_status import load_wallet_status
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository
from app.strategy.execution_planner import ExecutionPlanner
from app.strategy.polymarket_live_trading import PolymarketLiveTradingAdapter, create_authenticated_clob_v2_client
from app.strategy.risk_manager import RiskManager
from app.utils.execution_utils import build_execution_claim_key


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_LOG_DIR = BASE_DIR.parent / "runtime-logs"
WATCH_PID_FILE = RUNTIME_LOG_DIR / "watch.pid"
WATCH_SUPERVISOR_PID_FILE = RUNTIME_LOG_DIR / "watch-supervisor.pid"
WATCH_SCRIPT_PATH = BASE_DIR.parent / "scripts" / "watch-supervisor.ps1"
DASHBOARD_COMPONENT_TIMEOUT_SEC = 2.5
DASHBOARD_DB_TIMEOUT_SEC = 2.5
LIVE_FILL_SYNC_INTERVAL_SEC = 60.0


def _dashboard_strategy_variant(settings: Settings) -> str | None:
    if settings.near_close_maker_enabled and settings.near_close_scan_pool_enabled:
        return "near_close_maker"
    return None


def _wallet_balance(wallet: dict[str, Any], symbol: str) -> float | None:
    for item in wallet.get("balances") or []:
        if str(item.get("symbol") or "") == symbol:
            try:
                return float(item.get("amount") or 0.0)
            except (TypeError, ValueError):
                return None
    return None


def _trade_journal_payload(
    repository: ScannerRepository,
    settings: Settings,
    wallet: dict[str, Any],
) -> dict[str, Any]:
    payload = repository.live_trade_journal_summary()
    pusd_balance = _wallet_balance(wallet, "pUSD")
    if settings.pusd_pnl_baseline is not None and pusd_balance is not None:
        baseline = float(settings.pusd_pnl_baseline)
        payload["pusd_balance"] = pusd_balance
        payload["pusd_pnl_baseline"] = baseline
        payload["pusd_balance_delta"] = pusd_balance - baseline
    return payload


def _near_close_dashboard_payload(repository: ScannerRepository, settings: Settings) -> dict[str, Any]:
    payload = repository.near_close_dashboard_summary()
    signal_count = int(payload.get("signal_count") or 0)
    required = int(settings.near_close_min_paper_signals_for_live)
    live_enabled = bool(settings.near_close_maker_live_enabled)
    if live_enabled and signal_count >= required:
        mode = "live"
    elif live_enabled:
        mode = "live_waiting_paper"
    elif signal_count >= required:
        mode = "paper_gate_met"
    else:
        mode = "paper"
    return {
        **payload,
        "mode": mode,
        "paper_required": required,
        "live_enabled": live_enabled,
        "max_total_exposure": settings.near_close_max_total_exposure,
    }


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


def _iter_watch_processes() -> list[int]:
    watch_pids: list[int] = []
    for pid_file in (WATCH_PID_FILE, WATCH_SUPERVISOR_PID_FILE):
        pid = _read_pid(pid_file)
        if pid:
            watch_pids.append(pid)
    return watch_pids


def _start_watch_process(settings: Settings) -> bool:
    if _pid_running(_read_pid(WATCH_PID_FILE)):
        return False
    RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stdout_log = RUNTIME_LOG_DIR / "watch.stdout.log"
    stderr_log = RUNTIME_LOG_DIR / "watch.stderr.log"
    with stdout_log.open("ab") as stdout, stderr_log.open("ab") as stderr:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.main",
                "watch",
                "--limit",
                str(settings.watch_market_limit),
            ],
            cwd=str(BASE_DIR.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdout=stdout,
            stderr=stderr,
        )
    WATCH_PID_FILE.write_text(str(process.pid), encoding="ascii")
    return True


def _stop_watch_process() -> bool:
    stopped = False
    pids = set(_iter_watch_processes())
    for pid_file in (WATCH_PID_FILE, WATCH_SUPERVISOR_PID_FILE):
        pid = _read_pid(pid_file)
        if pid:
            pids.add(pid)
    for pid in pids:
        if not _pid_running(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped = True
        except OSError:
            continue
    for pid_file in (WATCH_PID_FILE, WATCH_SUPERVISOR_PID_FILE):
        try:
            pid_file.unlink()
        except OSError:
            pass
    return stopped


def _parse_utc_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    base = text
    if "+" in text[10:]:
        base = text.split("+", 1)[0]
    elif text.count("-") > 2 and "-" in text[10:]:
        base = text.rsplit("-", 1)[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(base, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _file_mtime_utc(path: Path) -> datetime | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)


def build_watch_status(settings: Settings, summary: dict[str, Any]) -> dict[str, Any]:
    supervisor_pid = _read_pid(WATCH_SUPERVISOR_PID_FILE)
    watch_pid = _read_pid(WATCH_PID_FILE)
    supervisor_running = _pid_running(supervisor_pid)
    watch_running = _pid_running(watch_pid)
    latest_scan_at = summary.get("latest_scan_at")
    latest_scan_dt = _parse_utc_timestamp(latest_scan_at)
    now = datetime.now(timezone.utc)
    max_scan_lag_sec = max(settings.scan_interval_sec * 4, settings.dashboard_refresh_sec * 2, 30)
    last_scan_age_sec = None
    scan_recent = False
    if latest_scan_dt is not None:
        last_scan_age_sec = max((now - latest_scan_dt).total_seconds(), 0.0)
        scan_recent = last_scan_age_sec <= max_scan_lag_sec
    pid_updated_at = _file_mtime_utc(WATCH_PID_FILE) or _file_mtime_utc(WATCH_SUPERVISOR_PID_FILE)
    startup_age_sec = None
    if pid_updated_at is not None:
        startup_age_sec = max((now - pid_updated_at).total_seconds(), 0.0)
    startup_grace_sec = max(settings.scan_interval_sec * 8, 45)

    running = watch_running and scan_recent
    if running:
        state = "running"
        message = "watch 背景監看正常運行中。"
    elif watch_running and startup_age_sec is not None and startup_age_sec <= startup_grace_sec:
        state = "starting"
        message = "watch 已啟動，正在建立首輪掃描。"
    elif watch_running:
        state = "stale"
        message = "watch 程序仍在，但最近掃描已停滯。"
    elif supervisor_running:
        state = "stopped"
        message = "watch supervisor 存活，但監看子程序未運行。"
    else:
        state = "stopped"
        message = "watch 背景監看未運行。"

    return {
        "state": state,
        "running": running,
        "supervisor_running": supervisor_running,
        "watch_running": watch_running,
        "scan_interval_sec": settings.scan_interval_sec,
        "dashboard_refresh_sec": settings.dashboard_refresh_sec,
        "supervisor_pid": supervisor_pid,
        "watch_pid": watch_pid,
        "latest_scan_at": latest_scan_at,
        "last_scan_age_sec": round(last_scan_age_sec, 1) if last_scan_age_sec is not None else None,
        "startup_age_sec": round(startup_age_sec, 1) if startup_age_sec is not None else None,
        "startup_grace_sec": startup_grace_sec,
        "max_scan_lag_sec": max_scan_lag_sec,
        "message": message,
    }


async def build_dashboard_payload(
    repository: ScannerRepository,
    settings: Settings,
    controls: TradingControls,
    preflight: dict[str, Any] | None = None,
    wallet: dict[str, Any] | None = None,
) -> dict[str, Any]:
    persistence_warning = settings.persistence_backend == "sqlite" and bool(os.getenv("K_SERVICE"))
    summary = repository.dashboard_summary()
    strategy_variant = _dashboard_strategy_variant(settings)
    return {
        "summary": summary,
        "strategies": repository.strategy_summary(strategy_variant=strategy_variant),
        "opportunities": repository.latest_opportunities(
            limit=settings.dashboard_page_size,
            strategy_variant=strategy_variant,
        ),
        "alerts": repository.recent_alerts(limit=8),
        "markets": repository.top_markets(limit=10, shortlist_only=strategy_variant == "near_close_maker"),
        "execution_events": repository.recent_execution_events(limit=10),
        "positions": repository.recent_live_positions(limit=10),
        "trade_groups": repository.live_trade_groups(limit=8),
        "open_positions": repository.open_live_positions(limit=12),
        "pnl": repository.settled_pnl_summary(),
        "trade_journal": _trade_journal_payload(repository, settings, wallet or {}),
        "refresh_sec": settings.dashboard_refresh_sec,
        "trading": controls.as_payload(),
        "risk": {
            **repository.trading_risk_summary(),
            "kill_switch": controls.kill_switch_enabled,
            "max_notional_per_plan": settings.max_notional_per_plan,
            "max_daily_live_notional": settings.max_daily_live_notional,
            "max_daily_live_orders": settings.max_daily_live_orders,
            "max_daily_paper_notional": settings.max_daily_paper_notional,
            "max_daily_paper_trades": settings.max_daily_paper_trades,
            "near_close": _near_close_dashboard_payload(repository, settings),
        },
        "wallet": wallet if wallet is not None else await load_wallet_status(settings),
        "preflight": preflight,
        "watch": build_watch_status(settings, summary),
        "persistence": {
            "backend": settings.persistence_backend,
            "cloud_warning": persistence_warning,
        },
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or get_settings()
    app = FastAPI(title="Polymarket Mispricing Dashboard")
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    app.state.settings = current_settings
    app.state.templates = templates
    app.state.scan_lock = asyncio.Lock()
    app.state.preflight_cache = None
    app.state.preflight_cache_at = 0.0
    app.state.wallet_cache = None
    app.state.wallet_cache_at = 0.0
    app.state.dashboard_db_task = None
    app.state.watch_task = None
    app.state.watch_stop_event = None
    app.state.watch_action_lock = asyncio.Lock()
    app.state.watch_started_at = None
    app.state.watch_latest_scan_at = None
    app.state.watch_last_error = None
    app.state.default_controls = TradingControls.from_settings(current_settings)
    app.state.controls_override = None
    app.state.controls_sync_task = None
    app.state.live_fill_sync_at = 0.0
    app.state.live_fill_sync_error = None
    app.state.live_trader = PolymarketLiveTradingAdapter(current_settings)

    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @contextmanager
    def repository_scope() -> Any:
        session = connect_db(current_settings)
        try:
            yield ScannerRepository(session)
        finally:
            try:
                session.close()
            except Exception:
                pass

    async def live_preflight(*, force: bool = False) -> dict[str, Any]:
        loop_time = asyncio.get_running_loop().time()
        cached = app.state.preflight_cache
        cache_age = loop_time - float(app.state.preflight_cache_at)
        if not force and cached is not None and cache_age < current_settings.preflight_cache_sec:
            return cached

        report = await load_preflight_report(current_settings, verify_clob_credentials=force)
        payload = report.as_payload()
        app.state.preflight_cache = payload
        app.state.preflight_cache_at = loop_time
        return payload

    def timed_out_preflight_payload(cached: dict[str, Any] | None = None) -> dict[str, Any]:
        if cached is not None:
            return {
                **cached,
                "stale": True,
                "warning": "dashboard_preflight_timeout",
            }
        return {
            "ready": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "address": None,
            "funder_address": None,
            "collateral_symbol": "pUSD",
            "blocking_reasons": ["Dashboard preflight refresh timed out; action buttons will re-check before arming."],
            "checks": [
                {
                    "id": "dashboard_preflight_timeout",
                    "label": "Dashboard preflight",
                    "status": "warning",
                    "message": "Dashboard preflight refresh timed out; action buttons will re-check before arming.",
                    "required": False,
                    "value": None,
                    "threshold": None,
                }
            ],
            "stale": True,
            "warning": "dashboard_preflight_timeout",
        }

    async def dashboard_preflight() -> dict[str, Any]:
        cached = app.state.preflight_cache
        try:
            return await asyncio.wait_for(live_preflight(force=False), timeout=DASHBOARD_COMPONENT_TIMEOUT_SEC)
        except TimeoutError:
            return timed_out_preflight_payload(cached)

    def timed_out_wallet_payload(cached: dict[str, Any] | None = None) -> dict[str, Any]:
        if cached is not None:
            return {**cached, "stale": True, "warning": "dashboard_wallet_timeout"}
        return {
            "configured": False,
            "address": None,
            "status": "dashboard_wallet_timeout",
            "message": "Dashboard wallet refresh timed out; the next refresh will retry.",
            "balances": [],
            "stale": True,
            "warning": "dashboard_wallet_timeout",
        }

    async def dashboard_wallet_status() -> dict[str, Any]:
        loop_time = asyncio.get_running_loop().time()
        cached = app.state.wallet_cache
        cache_age = loop_time - float(app.state.wallet_cache_at)
        if cached is not None and cache_age < current_settings.preflight_cache_sec:
            return cached
        try:
            payload = await asyncio.wait_for(load_wallet_status(current_settings), timeout=DASHBOARD_COMPONENT_TIMEOUT_SEC)
        except TimeoutError:
            return timed_out_wallet_payload(cached)
        app.state.wallet_cache = payload
        app.state.wallet_cache_at = loop_time
        return payload

    def preflight_failed_response(report: dict[str, Any]) -> HTTPException:
        return HTTPException(
            status_code=409,
            detail={
                "code": "preflight_failed",
                "reasons": report.get("blocking_reasons", []),
            },
        )

    def load_controls(repo: ScannerRepository) -> TradingControls:
        if app.state.controls_override is not None:
            return app.state.controls_override
        controls = repo.get_trading_controls(app.state.default_controls)
        app.state.controls_override = controls
        return controls

    def save_controls(repo: ScannerRepository, controls: TradingControls) -> TradingControls:
        app.state.controls_override = controls
        return repo.save_trading_controls(controls)

    def sync_live_fills_to_db() -> int:
        if not (current_settings.polymarket_private_key or "").strip():
            return 0
        client = create_authenticated_clob_v2_client(current_settings)
        fills = client.get_trades()
        if not isinstance(fills, list):
            return 0
        with repository_scope() as repo:
            return repo.save_clob_fills(fills, wallet_address=current_settings.polymarket_funder_address)

    async def maybe_sync_live_fills() -> None:
        loop_time = asyncio.get_running_loop().time()
        if loop_time - float(app.state.live_fill_sync_at) < LIVE_FILL_SYNC_INTERVAL_SEC:
            return
        app.state.live_fill_sync_at = loop_time
        try:
            inserted = await asyncio.wait_for(
                asyncio.to_thread(sync_live_fills_to_db),
                timeout=DASHBOARD_COMPONENT_TIMEOUT_SEC,
            )
        except Exception as exc:
            app.state.live_fill_sync_error = str(exc)
            return
        app.state.live_fill_sync_error = None
        if inserted:
            app.state.wallet_cache_at = 0.0

    def persist_controls_to_db(controls: TradingControls) -> None:
        with repository_scope() as repo:
            repo.save_trading_controls(controls)

    def set_runtime_controls(controls: TradingControls) -> TradingControls:
        app.state.controls_override = controls
        sync_task = app.state.controls_sync_task
        if sync_task is None or sync_task.done():
            app.state.controls_sync_task = asyncio.create_task(asyncio.to_thread(persist_controls_to_db, controls))
        return controls

    async def set_runtime_controls_quick(controls: TradingControls) -> TradingControls:
        app.state.controls_override = controls
        task = asyncio.create_task(asyncio.to_thread(persist_controls_to_db, controls))
        app.state.controls_sync_task = task
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=DASHBOARD_DB_TIMEOUT_SEC)
        except TimeoutError:
            pass
        return controls

    def current_runtime_controls() -> TradingControls:
        return app.state.controls_override or app.state.default_controls

    async def live_preflight_quick() -> dict[str, Any]:
        try:
            return await asyncio.wait_for(live_preflight(force=True), timeout=DASHBOARD_COMPONENT_TIMEOUT_SEC)
        except TimeoutError:
            return timed_out_preflight_payload(app.state.preflight_cache)

    def action_payload(reason: str, preflight: dict[str, Any] | None = None) -> dict[str, Any]:
        return fallback_dashboard_payload(
            reason=reason,
            preflight=preflight or app.state.preflight_cache or timed_out_preflight_payload(),
            wallet=app.state.wallet_cache or timed_out_wallet_payload(),
        )

    def watch_task_running() -> bool:
        task = app.state.watch_task
        return task is not None and not task.done()

    def watch_status_payload(summary: dict[str, Any] | None = None) -> dict[str, Any]:
        summary = dict(summary or {})
        external_status = build_watch_status(current_settings, summary)
        task = app.state.watch_task
        running_task = task is not None and not task.done()
        latest_scan_at = summary.get("latest_scan_at") or app.state.watch_latest_scan_at
        latest_scan_dt = _parse_utc_timestamp(latest_scan_at)
        started_at = app.state.watch_started_at
        now = datetime.now(timezone.utc)
        max_scan_lag_sec = max(current_settings.scan_interval_sec * 4, current_settings.dashboard_refresh_sec * 2, 30)
        startup_grace_sec = max(current_settings.scan_interval_sec * 8, 45)
        last_scan_age_sec = None
        scan_recent = False
        if latest_scan_dt is not None:
            last_scan_age_sec = max((now - latest_scan_dt).total_seconds(), 0.0)
            scan_recent = last_scan_age_sec <= max_scan_lag_sec
        startup_age_sec = None
        if started_at is not None:
            startup_age_sec = max((now - started_at).total_seconds(), 0.0)

        if running_task and scan_recent:
            state = "running"
            message = "watch 背景監看正常運行中。"
            running = True
        elif running_task and startup_age_sec is not None and startup_age_sec <= startup_grace_sec:
            state = "starting"
            message = "watch 已啟動，正在建立首輪掃描。"
            running = False
        elif running_task:
            state = "stale"
            message = app.state.watch_last_error or "watch 程序仍在，但最近掃描已停滯。"
            running = False
        else:
            state = "stopped"
            message = app.state.watch_last_error or "watch 背景監看未運行。"
            running = False

        if (
            not running_task
            and (external_status["watch_running"] or external_status["supervisor_running"])
        ):
            return external_status

        return {
            "state": state,
            "running": running,
            "supervisor_running": external_status["supervisor_running"],
            "watch_running": running_task or external_status["watch_running"],
            "scan_interval_sec": current_settings.scan_interval_sec,
            "dashboard_refresh_sec": current_settings.dashboard_refresh_sec,
            "supervisor_pid": external_status["supervisor_pid"],
            "watch_pid": external_status["watch_pid"],
            "latest_scan_at": latest_scan_at,
            "last_scan_age_sec": round(last_scan_age_sec, 1) if last_scan_age_sec is not None else None,
            "startup_age_sec": round(startup_age_sec, 1) if startup_age_sec is not None else None,
            "startup_grace_sec": startup_grace_sec,
            "max_scan_lag_sec": max_scan_lag_sec,
            "message": message,
        }

    def run_watch_cycle_once() -> datetime:
        with repository_scope() as repo:
            result = asyncio.run(
                execute_scan_cycle(
                    current_settings,
                    limit=current_settings.watch_market_limit,
                    repository=repo,
                )
            )
            persist_scan_cycle(repo, result)
        return getattr(result, "executed_at", datetime.now(timezone.utc))

    async def embedded_watch_loop(stop_event: asyncio.Event) -> None:
        app.state.watch_started_at = datetime.now(timezone.utc)
        app.state.watch_last_error = None
        try:
            while not stop_event.is_set():
                try:
                    executed_at = await asyncio.to_thread(run_watch_cycle_once)
                    app.state.watch_latest_scan_at = executed_at.isoformat()
                    app.state.watch_last_error = None
                except Exception as exc:
                    app.state.watch_last_error = f"watch 掃描失敗：{exc}"
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=current_settings.scan_interval_sec)
                except TimeoutError:
                    pass
        finally:
            app.state.watch_stop_event = None

    def fallback_dashboard_payload(
        *,
        reason: str,
        preflight: dict[str, Any] | None = None,
        wallet: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        controls = app.state.controls_override or app.state.default_controls
        return {
            "summary": {
                "total_markets": 0,
                "open_markets": 0,
                "latest_discovered_at": None,
                "latest_scan_at": None,
                "latest_discovered_market_count": 0,
                "latest_monitored_markets": 0,
                "latest_book_count": 0,
                "latest_scan_opportunities": 0,
                "latest_actionable_count": 0,
                "latest_candidate_count": 0,
                "watch_bucket_counts": {},
                "shortlist_reason_counts": {},
                "excluded_long_tail_count": 0,
                "excluded_family_cap_count": 0,
                "positive_edge_candidates_24h": 0,
                "total_opportunities": 0,
                "opportunities_24h": 0,
                "latest_opportunity_at": None,
                "best_net_edge": None,
                "alerts_24h": 0,
                "latest_alert_at": None,
                "latest_snapshot_at": None,
                "average_paper_pnl": None,
                "paper_trades_today": 0,
                "paper_notional_today": 0.0,
                "paper_fees_today": 0.0,
                "live_orders_today": 0,
                "live_notional_today": 0.0,
                "execution_events_24h": 0,
                "latest_execution_event_at": None,
                "warning": reason,
            },
            "strategies": [],
            "opportunities": [],
            "alerts": [],
            "markets": [],
            "execution_events": [],
            "positions": [],
            "trade_groups": [],
            "open_positions": [],
            "pnl": {
                "paper_settled_count": 0,
                "paper_realized_pnl_today": 0.0,
                "paper_realized_fees_today": 0.0,
                "live_settled_pnl_today": None,
                "live_settled_supported": False,
                "note": reason,
            },
            "trade_journal": {
                "estimated_realized_pnl_today": 0.0,
                "estimated_realized_pnl_total": 0.0,
                "trade_count_today": 0,
                "trade_count_total": 0,
                "open_size_total": 0.0,
                "open_cost_basis": 0.0,
                "note": reason,
            },
            "refresh_sec": current_settings.dashboard_refresh_sec,
            "trading": controls.as_payload(),
            "risk": {
                "paper_trades_today": 0,
                "paper_notional_today": 0.0,
                "paper_fees_today": 0.0,
                "live_orders_today": 0,
                "live_notional_today": 0.0,
                "kill_switch": controls.kill_switch_enabled,
                "max_notional_per_plan": current_settings.max_notional_per_plan,
                "max_daily_live_notional": current_settings.max_daily_live_notional,
                "max_daily_live_orders": current_settings.max_daily_live_orders,
                "max_daily_paper_notional": current_settings.max_daily_paper_notional,
                "max_daily_paper_trades": current_settings.max_daily_paper_trades,
                "near_close": {
                    "mode": "live" if current_settings.near_close_maker_live_enabled else "paper",
                    "signal_count": 0,
                    "paper_required": current_settings.near_close_min_paper_signals_for_live,
                    "live_enabled": current_settings.near_close_maker_live_enabled,
                    "live_exposure": 0.0,
                    "active_orders": 0,
                    "max_total_exposure": current_settings.near_close_max_total_exposure,
                },
            },
            "wallet": wallet if wallet is not None else timed_out_wallet_payload(),
            "preflight": preflight if preflight is not None else timed_out_preflight_payload(),
            "watch": watch_status_payload({}),
            "persistence": {
                "backend": current_settings.persistence_backend,
                "cloud_warning": current_settings.persistence_backend == "sqlite",
                "warning": reason,
            },
            "scan_in_progress": app.state.scan_lock.locked(),
        }

    def load_dashboard_payload_from_db(preflight: dict[str, Any], wallet: dict[str, Any]) -> dict[str, Any]:
        with repository_scope() as repo:
            controls = load_controls(repo)
            if (
                current_settings.require_live_preflight
                and not preflight.get("stale")
                and not preflight.get("ready")
                and controls.auto_execute_enabled
            ):
                controls = save_controls(
                    repo,
                    TradingControls(
                        live_trading_enabled=controls.live_trading_enabled,
                        auto_execute_enabled=False,
                        kill_switch_enabled=controls.kill_switch_enabled,
                    ),
                )
            persistence_warning = current_settings.persistence_backend == "sqlite" and bool(os.getenv("K_SERVICE"))
            summary = repo.dashboard_summary()
            strategy_variant = _dashboard_strategy_variant(current_settings)
            return {
                "summary": summary,
                "strategies": repo.strategy_summary(strategy_variant=strategy_variant),
                "opportunities": repo.latest_opportunities(
                    limit=current_settings.dashboard_page_size,
                    strategy_variant=strategy_variant,
                ),
                "alerts": repo.recent_alerts(limit=8),
                "markets": repo.top_markets(limit=10, shortlist_only=strategy_variant == "near_close_maker"),
                "execution_events": repo.recent_execution_events(limit=10),
                "positions": repo.recent_live_positions(limit=10),
                "trade_groups": repo.live_trade_groups(limit=8),
                "open_positions": repo.open_live_positions(limit=12),
                "pnl": repo.settled_pnl_summary(),
                "trade_journal": _trade_journal_payload(repo, current_settings, wallet),
                "refresh_sec": current_settings.dashboard_refresh_sec,
                "trading": controls.as_payload(),
                "risk": {
                    **repo.trading_risk_summary(),
                    "kill_switch": controls.kill_switch_enabled,
                    "max_notional_per_plan": current_settings.max_notional_per_plan,
                    "max_daily_live_notional": current_settings.max_daily_live_notional,
                    "max_daily_live_orders": current_settings.max_daily_live_orders,
                    "max_daily_paper_notional": current_settings.max_daily_paper_notional,
                    "max_daily_paper_trades": current_settings.max_daily_paper_trades,
                    "near_close": _near_close_dashboard_payload(repo, current_settings),
                },
                "wallet": wallet,
                "preflight": preflight,
                "watch": watch_status_payload(summary),
                "persistence": {
                    "backend": current_settings.persistence_backend,
                    "cloud_warning": persistence_warning,
                },
            }

    async def dashboard_payload() -> dict[str, Any]:
        preflight, wallet = await asyncio.gather(dashboard_preflight(), dashboard_wallet_status())
        await maybe_sync_live_fills()
        running_task = app.state.dashboard_db_task
        if running_task is not None and not running_task.done():
            return fallback_dashboard_payload(
                reason="Dashboard database is still warming up; showing a lightweight startup view.",
                preflight=preflight,
                wallet=wallet,
            )

        task = asyncio.create_task(asyncio.to_thread(load_dashboard_payload_from_db, preflight, wallet))
        app.state.dashboard_db_task = task
        try:
            payload = await asyncio.wait_for(asyncio.shield(task), timeout=DASHBOARD_DB_TIMEOUT_SEC)
        except TimeoutError:
            return fallback_dashboard_payload(
                reason="Dashboard database response timed out; showing a lightweight startup view.",
                preflight=preflight,
                wallet=wallet,
            )
        payload["scan_in_progress"] = app.state.scan_lock.locked()
        return payload

    async def execute_live_from_scan(
        repo: ScannerRepository,
        *,
        opportunities: list[Any],
        controls: TradingControls,
    ) -> tuple[dict[str, Any], TradingControls]:
        if not controls.armed:
            return {"attempted_count": 0, "submitted_count": 0, "items": []}, controls

        preflight = await live_preflight(force=True)
        if current_settings.require_live_preflight and not preflight.get("ready"):
            updated_controls = save_controls(
                repo,
                TradingControls(
                    live_trading_enabled=controls.live_trading_enabled,
                    auto_execute_enabled=False,
                    kill_switch_enabled=controls.kill_switch_enabled,
                ),
            )
            repo.save_execution_event(
                source="dashboard",
                mode="live",
                opportunity_id=None,
                status="preflight_blocked",
                message="Live preflight failed. Auto execution has been paused.",
                details={"blocking_reasons": preflight.get("blocking_reasons", [])},
            )
            return (
                {
                    "attempted_count": 0,
                    "submitted_count": 0,
                    "items": [
                        {
                            "opportunity_id": None,
                            "status": "preflight_blocked",
                            "legs": 0,
                            "message": "Live preflight failed. Auto execution has been paused.",
                        }
                    ],
                },
                updated_controls,
            )

        runtime_settings = controls.apply(current_settings)
        planner = ExecutionPlanner(max_leg_size=runtime_settings.live_max_order_size)
        risk_manager = RiskManager(runtime_settings)
        liquidity_filter = LiquidityFilter(runtime_settings)
        live_trader: PolymarketLiveTradingAdapter = app.state.live_trader
        live_trader.settings = runtime_settings
        executions: list[dict[str, Any]] = []
        current_controls = controls

        for opportunity in opportunities:
            if not liquidity_filter.is_alert_eligible(opportunity):
                continue
            if repo.was_alerted_recently(opportunity.opportunity_id, runtime_settings.alert_cooldown_sec):
                continue

            plan = planner.build_plan(opportunity)
            if not plan.live_trading_allowed:
                continue

            risk_decision = risk_manager.assess(plan, repo, mode="live")
            if not risk_decision.allowed:
                repo.save_execution_event(
                    source="dashboard",
                    mode="live",
                    opportunity_id=opportunity.opportunity_id,
                    status="risk_blocked",
                    message=risk_decision.reason,
                    details={
                        "estimated_notional": risk_decision.estimated_notional,
                        "projected_daily_notional": risk_decision.projected_daily_notional,
                        "projected_daily_orders": risk_decision.projected_daily_orders,
                    },
                )
                executions.append(
                    {
                        "opportunity_id": opportunity.opportunity_id,
                        "status": "risk_blocked",
                        "legs": 0,
                        "message": risk_decision.reason,
                    }
                )
                continue

            claim_key = build_execution_claim_key(opportunity, mode="live")
            claimed = repo.claim_execution(
                claim_key=claim_key,
                opportunity_id=opportunity.opportunity_id,
                source="dashboard",
                mode="live",
                message="Execution claimed by dashboard scan.",
            )
            if not claimed:
                repo.save_execution_event(
                    source="dashboard",
                    mode="live",
                    opportunity_id=opportunity.opportunity_id,
                    status="duplicate_claim",
                    message="Opportunity already claimed by another worker.",
                    details={"claim_key": claim_key},
                    claim_key=claim_key,
                )
                executions.append(
                    {
                        "opportunity_id": opportunity.opportunity_id,
                        "status": "duplicate_claim",
                        "legs": 0,
                        "message": "Opportunity already claimed by another worker.",
                    }
                )
                continue

            try:
                live_result = await live_trader.execute(plan)
            except Exception as exc:
                repo.update_execution_claim(claim_key=claim_key, status="failed", message=str(exc))
                repo.save_execution_event(
                    source="dashboard",
                    mode="live",
                    opportunity_id=opportunity.opportunity_id,
                    status="failed",
                    message=str(exc),
                    details={"claim_key": claim_key},
                    claim_key=claim_key,
                )
                executions.append(
                    {
                        "opportunity_id": opportunity.opportunity_id,
                        "status": "failed",
                        "legs": 0,
                        "message": str(exc),
                    }
                )
                continue

            repo.update_execution_claim(
                claim_key=claim_key,
                status=live_result.status,
                message=live_result.message,
            )
            repo.save_live_execution(live_result)
            repo.save_execution_event(
                source="dashboard",
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
            if live_result.status == "submitted":
                repo.save_alert(opportunity.opportunity_id, "dashboard-live", opportunity.summary)
            elif live_result.status == "partial_failure":
                current_controls = save_controls(
                    repo,
                    TradingControls(
                        live_trading_enabled=False,
                        auto_execute_enabled=False,
                        kill_switch_enabled=True,
                    ),
                )

            executions.append(
                {
                    "opportunity_id": opportunity.opportunity_id,
                    "status": live_result.status,
                    "legs": len(live_result.leg_results),
                    "message": live_result.message,
                }
            )
            if live_result.status == "partial_failure":
                break

        return (
            {
                "attempted_count": len(executions),
                "submitted_count": sum(1 for item in executions if item["status"] == "submitted"),
                "items": executions[:5],
            },
            current_controls,
        )

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "refresh_sec": current_settings.dashboard_refresh_sec,
                "page_size": current_settings.dashboard_page_size,
            },
        )

    @app.get("/api/health")
    async def health() -> JSONResponse:
        watch_status = watch_status_payload({})
        return JSONResponse(
            {
                "status": "ok",
                "scan_in_progress": app.state.scan_lock.locked(),
                "latest_snapshot_at": None,
                "latest_alert_at": None,
                "persistence_backend": current_settings.persistence_backend,
                "watch": watch_status,
            }
        )

    @app.get("/api/dashboard")
    async def api_dashboard() -> JSONResponse:
        return JSONResponse(await dashboard_payload())

    @app.post("/api/actions/trading/live")
    async def api_toggle_live_trading() -> JSONResponse:
        controls = current_runtime_controls()
        if controls.kill_switch_enabled and not controls.live_trading_enabled:
            raise HTTPException(status_code=409, detail="kill_switch_enabled")
        preflight = app.state.preflight_cache
        if not controls.live_trading_enabled:
            preflight = await live_preflight_quick()
            if not preflight.get("address"):
                raise HTTPException(status_code=409, detail="private_key_not_configured")
            if current_settings.require_live_preflight and not preflight.get("ready"):
                raise preflight_failed_response(preflight)
        await set_runtime_controls_quick(
            TradingControls(
                live_trading_enabled=not controls.live_trading_enabled,
                auto_execute_enabled=False if controls.live_trading_enabled else controls.auto_execute_enabled,
                kill_switch_enabled=controls.kill_switch_enabled,
            )
        )
        return JSONResponse({"status": "ok", "payload": action_payload("Runtime controls updated.", preflight)})

    @app.post("/api/actions/trading/auto")
    async def api_toggle_auto_execute() -> JSONResponse:
        controls = current_runtime_controls()
        if controls.kill_switch_enabled:
            raise HTTPException(status_code=409, detail="kill_switch_enabled")
        if not controls.live_trading_enabled:
            raise HTTPException(status_code=409, detail="live_trading_not_enabled")
        preflight = app.state.preflight_cache
        if not controls.auto_execute_enabled:
            preflight = await live_preflight_quick()
            if not preflight.get("address"):
                raise HTTPException(status_code=409, detail="private_key_not_configured")
            if current_settings.require_live_preflight and not preflight.get("ready"):
                raise preflight_failed_response(preflight)
        await set_runtime_controls_quick(
            TradingControls(
                live_trading_enabled=controls.live_trading_enabled,
                auto_execute_enabled=not controls.auto_execute_enabled,
                kill_switch_enabled=controls.kill_switch_enabled,
            )
        )
        return JSONResponse({"status": "ok", "payload": action_payload("Runtime controls updated.", preflight)})

    @app.post("/api/actions/risk/kill-switch")
    async def api_toggle_kill_switch() -> JSONResponse:
        controls = current_runtime_controls()
        enabled = not controls.kill_switch_enabled
        await set_runtime_controls_quick(
            TradingControls(
                live_trading_enabled=False,
                auto_execute_enabled=False,
                kill_switch_enabled=enabled,
            )
        )
        return JSONResponse({"status": "ok", "payload": action_payload("Runtime controls updated.")})

    @app.post("/api/actions/watch")
    async def api_toggle_watch() -> JSONResponse:
        async with app.state.watch_action_lock:
            if watch_task_running():
                stop_event = app.state.watch_stop_event
                if stop_event is not None:
                    stop_event.set()
            else:
                stop_event = asyncio.Event()
                app.state.watch_stop_event = stop_event
                app.state.watch_task = asyncio.create_task(embedded_watch_loop(stop_event))
                await asyncio.sleep(0.25)
        return JSONResponse({"status": "ok", "payload": await dashboard_payload()})

    @app.post("/api/actions/scan")
    async def api_scan(limit: int | None = None) -> JSONResponse:
        if app.state.scan_lock.locked():
            raise HTTPException(status_code=409, detail="scan_already_running")
        async with app.state.scan_lock:
            with repository_scope() as repo:
                result = await execute_scan_cycle(
                    current_settings,
                    limit=limit or current_settings.dashboard_scan_limit,
                    repository=repo,
                )
                persist_scan_cycle(repo, result)
                controls = load_controls(repo)
                execution_summary, controls = await execute_live_from_scan(
                    repo,
                    opportunities=result.opportunities,
                    controls=controls,
                )
                payload = await build_dashboard_payload(
                    repo,
                    current_settings,
                    controls,
                    preflight=await dashboard_preflight(),
                    wallet=await dashboard_wallet_status(),
                )
        payload["scan_in_progress"] = app.state.scan_lock.locked()
        return JSONResponse({"status": "ok", "payload": payload, "execution_summary": execution_summary})

    return app


app = create_app()
