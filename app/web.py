from __future__ import annotations

import asyncio
import contextlib
import copy
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, get_settings
from app.clients.clob_client import ClobClient
from app.models.runtime import TradingControls
from app.orchestration import execute_scan_cycle, persist_scan_cycle
from app.scanners.liquidity_filter import LiquidityFilter
from app.services.preflight import load_preflight_report
from app.services.redeemer import run_auto_redeem_once
from app.services.wallet_status import load_wallet_status
from app.storage.db import connect_db
from app.storage.path_safety import path_sync_warning
from app.storage.repositories import ScannerRepository
from app.strategy.execution_planner import ExecutionPlanner
from app.strategy.near_close_stop_exit import execute_near_close_taker_exits
from app.strategy.polymarket_live_trading import PolymarketLiveTradingAdapter, create_authenticated_clob_v2_client
from app.strategy.risk_manager import RiskManager
from app.utils.execution_utils import build_execution_claim_key


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_LOG_DIR = BASE_DIR.parent / "runtime-logs"
WATCH_PID_FILE = RUNTIME_LOG_DIR / "watch.pid"
WATCH_SUPERVISOR_PID_FILE = RUNTIME_LOG_DIR / "watch-supervisor.pid"
WATCH_SCRIPT_PATH = BASE_DIR.parent / "scripts" / "watch-supervisor.ps1"
DASHBOARD_COMPONENT_TIMEOUT_SEC = 2.5
DASHBOARD_DB_TIMEOUT_SEC = 4.0
EMBEDDED_WATCH_SCAN_TIMEOUT_SEC = 60.0
EMBEDDED_WATCH_LIVE_TIMEOUT_SEC = 25.0
EMBEDDED_WATCH_CYCLE_TIMEOUT_SEC = 60.0
LIVE_FILL_SYNC_INTERVAL_SEC = 5.0
LIVE_FILL_ACTIVITY_LIMIT = 500
NEAR_CLOSE_STOP_EXIT_INTERVAL_SEC = 5.0
NEAR_CLOSE_STOP_EXIT_TIMEOUT_SEC = 12.0
OPEN_POSITION_BOOK_REFRESH_INTERVAL_SEC = 120.0


async def _fetch_orderbooks_for_tokens(settings: Settings, token_ids: list[str]) -> dict[str, Any]:
    clob = ClobClient(settings.clob_base_url, concurrency=min(max(len(token_ids), 1), settings.book_fetch_concurrency))
    try:
        return await clob.get_order_books(token_ids)
    finally:
        await clob.close()


def refresh_open_position_orderbooks(repository: ScannerRepository, settings: Settings) -> int:
    groups = repository.near_close_stop_exit_groups(limit=50)
    token_ids = sorted(
        {
            str(group.get("token_id") or "")
            for group in groups
            if float(group.get("open_size") or 0.0) > 1e-9 and str(group.get("token_id") or "")
        }
    )
    if not token_ids:
        return 0
    books = asyncio.run(_fetch_orderbooks_for_tokens(settings, token_ids))
    if not books:
        return 0
    repository.save_orderbooks(books.values())
    return len(books)


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


def _wallet_portfolio_value(wallet: dict[str, Any]) -> float | None:
    portfolio = wallet.get("portfolio")
    if not isinstance(portfolio, dict):
        return None
    value = portfolio.get("position_value")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _portfolio_positions_by_asset(wallet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    portfolio = wallet.get("portfolio")
    if not isinstance(portfolio, dict):
        return {}
    positions = portfolio.get("positions")
    if not isinstance(positions, list):
        return {}
    indexed: dict[str, dict[str, Any]] = {}
    for position in positions:
        if not isinstance(position, dict):
            continue
        asset = str(position.get("asset") or "")
        if asset:
            indexed[asset] = position
    return indexed


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_string(position: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = position.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _portfolio_position_size(position: dict[str, Any]) -> float:
    for key in ("size", "amount", "quantity", "shares"):
        value = _float_or_none(position.get(key))
        if value is not None:
            return value
    return 0.0


def _portfolio_position_entry_notional(position: dict[str, Any], size: float, current_value: float | None) -> float | None:
    for key in ("initialValue", "costBasis", "totalBought", "valueBought"):
        value = _float_or_none(position.get(key))
        if value is not None:
            return value
    avg_price = _float_or_none(position.get("avgPrice"))
    if avg_price is not None and size > 0:
        return avg_price * size
    cash_pnl = _float_or_none(position.get("cashPnl"))
    if current_value is not None and cash_pnl is not None:
        return current_value - cash_pnl
    return None


def _wallet_only_trade_group(position: dict[str, Any]) -> dict[str, Any] | None:
    asset = str(position.get("asset") or "").strip()
    if not asset:
        return None
    size = _portfolio_position_size(position)
    current_value = _float_or_none(position.get("currentValue"))
    if size <= 1e-9 and (current_value is None or current_value <= 1e-9):
        return None

    current_price = _float_or_none(position.get("curPrice"))
    cash_pnl = _float_or_none(position.get("cashPnl"))
    entry_notional = _portfolio_position_entry_notional(position, size, current_value)
    unrealized_pnl = cash_pnl
    if unrealized_pnl is None and current_value is not None and entry_notional is not None:
        unrealized_pnl = current_value - entry_notional

    market_slug = _first_string(
        position,
        ("slug", "marketSlug", "eventSlug", "conditionSlug", "title", "market", "eventTitle"),
    )
    outcome_label = _first_string(position, ("outcome", "side", "outcomeName", "name")) or "-"
    updated_at = _first_string(position, ("updatedAt", "lastUpdated", "createdAt")) or datetime.now(timezone.utc).isoformat()

    return {
        "opportunity_id": f"wallet:{asset}",
        "token_id": asset,
        "market_slug": market_slug or asset,
        "outcome_label": outcome_label,
        "latest_status": "redeemable" if position.get("redeemable") is True else "wallet_position",
        "latest_at": updated_at,
        "entry_notional": entry_notional,
        "exit_notional": 0.0,
        "estimated_realized_pnl": 0.0,
        "buy_size": size,
        "sell_size": 0.0,
        "redeemed_size": 0.0,
        "open_size": size,
        "open_cost_basis": entry_notional,
        "current_price": current_price,
        "current_price_source": "polymarket_data_api",
        "current_value": current_value,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": cash_pnl if cash_pnl is not None else unrealized_pnl,
        "trades": [],
    }


def _apply_portfolio_position_values(groups: list[dict[str, Any]], wallet: dict[str, Any]) -> list[dict[str, Any]]:
    positions = _portfolio_positions_by_asset(wallet)
    if not positions:
        return groups
    adjusted: list[dict[str, Any]] = []
    matched_assets: set[str] = set()
    for group in groups:
        item = dict(group)
        asset = str(item.get("token_id") or "")
        position = positions.get(asset)
        if position is not None and float(item.get("open_size") or 0.0) > 1e-9:
            matched_assets.add(asset)
            current_value = _float_or_none(position.get("currentValue"))
            cash_pnl = _float_or_none(position.get("cashPnl"))
            cur_price = _float_or_none(position.get("curPrice"))
            local_value = _float_or_none(item.get("current_value"))
            local_price = _float_or_none(item.get("current_price"))
            local_has_price = local_value is not None and local_price is not None
            if current_value is not None and not local_has_price:
                item["current_value"] = current_value
                item["unrealized_pnl"] = current_value - float(item.get("open_cost_basis") or 0.0)
                item["current_price_source"] = "polymarket_data_api"
            if cash_pnl is not None and not local_has_price:
                item["total_pnl"] = cash_pnl
            if cur_price is not None and local_price is None:
                item["current_price"] = cur_price
        adjusted.append(item)
    for asset, position in positions.items():
        if asset in matched_assets:
            continue
        wallet_group = _wallet_only_trade_group(position)
        if wallet_group is not None:
            adjusted.append(wallet_group)
    return adjusted


def _extract_order_ids(payload: object) -> list[str]:
    ids: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key in ("id", "order_id", "orderId", "hash"):
                order_id = value.get(key)
                if isinstance(order_id, str) and order_id.startswith("0x"):
                    ids.append(order_id)
            for nested in value.values():
                visit(nested)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    return list(dict.fromkeys(ids))


def _trade_journal_payload(
    repository: ScannerRepository,
    settings: Settings,
    wallet: dict[str, Any],
) -> dict[str, Any]:
    payload = repository.live_trade_journal_summary()
    pusd_balance = _wallet_balance(wallet, "pUSD")
    if settings.pusd_pnl_baseline is not None and pusd_balance is not None:
        baseline = float(settings.pusd_pnl_baseline)
        local_open_market_value = sum(
            float(group.get("current_value") or 0.0)
            for group in repository.live_trade_groups(limit=50)
            if float(group.get("open_size") or 0.0) > 1e-9
        )
        portfolio_value = _wallet_portfolio_value(wallet)
        open_market_value = portfolio_value if portfolio_value is not None else local_open_market_value
        account_equity_estimate = pusd_balance + open_market_value
        payload["pusd_balance"] = pusd_balance
        payload["pusd_pnl_baseline"] = baseline
        payload["pusd_balance_delta"] = pusd_balance - baseline
        payload["open_market_value"] = open_market_value
        payload["local_open_market_value"] = local_open_market_value
        payload["open_market_value_source"] = "polymarket_data_api" if portfolio_value is not None else "local_trade_journal"
        payload["account_equity_estimate"] = account_equity_estimate
        payload["account_equity_delta"] = account_equity_estimate - baseline
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


def _last_funnel_stage(summary: dict[str, Any]) -> dict[str, Any] | None:
    stages = summary.get("near_close_funnel")
    if not isinstance(stages, list) or not stages:
        return None
    stage = stages[-1]
    return stage if isinstance(stage, dict) else None


def _trading_parameters_payload(
    settings: Settings,
    controls: TradingControls,
    *,
    summary: dict[str, Any] | None = None,
    risk_summary: dict[str, Any] | None = None,
    preflight: dict[str, Any] | None = None,
    watch: dict[str, Any] | None = None,
    opportunities: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    summary = summary or {}
    risk_summary = risk_summary or {}
    preflight = preflight or {}
    watch = watch or {}
    latest_heartbeat = watch.get("latest_heartbeat") or {}
    heartbeat_details = latest_heartbeat.get("details") or {}
    latest_opportunity_count = int(summary.get("latest_scan_opportunities") or 0)
    latest_actionable_count = int(summary.get("latest_actionable_count") or 0)
    latest_opportunities = opportunities or []
    live_eligible_count = (
        sum(1 for item in latest_opportunities if (item.get("details") or {}).get("tradable_live"))
        if latest_opportunity_count > 0
        else 0
    )
    funnel_stage = _last_funnel_stage(summary)
    diagnostics: list[dict[str, str]] = []

    if not controls.live_trading_enabled:
        diagnostics.append({"level": "blocked", "label": "Live 未啟用", "detail": "Live 模式關閉時不會送真單。"})
    if not controls.auto_execute_enabled:
        diagnostics.append({"level": "blocked", "label": "自動下單未啟用", "detail": "自動下單關閉時只會掃描與顯示機會。"})
    if controls.kill_switch_enabled:
        diagnostics.append({"level": "blocked", "label": "Kill switch", "detail": "緊急停止已啟用，所有 Live 送單會被風控擋下。"})
    if settings.require_live_preflight and preflight and preflight.get("stale"):
        diagnostics.append({"level": "watch", "label": "前置檢查快取", "detail": "dashboard 這次前置檢查 timeout，操作按鈕會重新檢查；watch 仍會用自己的前置檢查。"})
    elif settings.require_live_preflight and preflight and not preflight.get("ready"):
        diagnostics.append({"level": "blocked", "label": "前置檢查未通過", "detail": "錢包、RPC、pUSD 或授權檢查未完全通過。"})
    if not watch.get("running"):
        diagnostics.append({"level": "blocked", "label": "watch 未運行", "detail": "背景 watch 停止時不會自動掃描與送單。"})
    if latest_opportunity_count <= 0:
        label = str(funnel_stage.get("label") or "掃描條件") if funnel_stage else "掃描條件"
        diagnostics.append({"level": "watch", "label": "最新掃描沒有可下單機會", "detail": f"最新一輪 opportunity=0；目前最後卡在「{label}」。"})
    elif latest_actionable_count <= 0:
        diagnostics.append({"level": "watch", "label": "最新掃描沒有 actionable", "detail": f"最新一輪有 {latest_opportunity_count} 個候選，但沒有可直接送單的 actionable。"})
    elif live_eligible_count <= 0:
        diagnostics.append({"level": "watch", "label": "候選尚未進入 Live 時間窗", "detail": f"最新列表有 {latest_actionable_count} 個 actionable，但沒有 tradable_live；目前 Live 只在結束前 {settings.near_close_live_max_minutes_to_end:g} 分鐘內送單。"})
    if heartbeat_details.get("previous_phase") == "timeout" or latest_heartbeat.get("state") == "timeout":
        diagnostics.append({"level": "watch", "label": "watch 最近 timeout", "detail": f"單輪掃描超過 {settings.watch_scan_timeout_sec:.0f} 秒會 drop，之後 delay {settings.watch_timeout_retry_sec:.0f} 秒再掃。"})
    if float(risk_summary.get("live_notional_today") or 0.0) >= float(settings.max_daily_live_notional):
        diagnostics.append({"level": "blocked", "label": "Live 金額上限", "detail": "今日 Live 名目金額已達上限。"})
    if int(risk_summary.get("live_orders_today") or 0) >= int(settings.max_daily_live_orders):
        diagnostics.append({"level": "blocked", "label": "Live 筆數上限", "detail": "今日 Live 送單筆數已達上限。"})
    if not diagnostics:
        diagnostics.append({"level": "ok", "label": "待命中", "detail": "Live、自動下單、前置檢查與風控目前沒有阻擋；下一步取決於掃描是否命中可交易機會。"})

    return {
        "diagnostics": diagnostics,
        "groups": [
            {
                "title": "目前狀態",
                "items": [
                    {"label": "Live", "value": "開" if controls.live_trading_enabled else "關"},
                    {"label": "自動下單", "value": "開" if controls.auto_execute_enabled else "關"},
                    {"label": "Kill switch", "value": "開" if controls.kill_switch_enabled else "關"},
                    {"label": "前置檢查", "value": "通過" if preflight.get("ready") else "未通過"},
                    {"label": "watch", "value": str(watch.get("state") or "unknown")},
                    {"label": "watch 階段", "value": str(watch.get("phase") or "-")},
                    {"label": "最新 opportunities", "value": latest_opportunity_count},
                    {"label": "最新 actionable", "value": latest_actionable_count},
                    {"label": "最新 live eligible", "value": live_eligible_count},
                ],
            },
            {
                "title": "掃描 / watch",
                "items": [
                    {"label": "掃描間隔", "value": settings.scan_interval_sec, "unit": "秒"},
                    {"label": "掃描 timeout", "value": settings.watch_scan_timeout_sec, "unit": "秒"},
                    {"label": "timeout 後 delay", "value": settings.watch_timeout_retry_sec, "unit": "秒"},
                    {"label": "watch 市場上限", "value": settings.watch_market_limit},
                    {"label": "book 併發", "value": settings.book_fetch_concurrency},
                    {"label": "dashboard 更新", "value": settings.dashboard_refresh_sec, "unit": "秒"},
                ],
            },
            {
                "title": "Near-close 條件",
                "items": [
                    {"label": "maker live", "value": "開" if settings.near_close_maker_live_enabled else "關"},
                    {"label": "paper signals 門檻", "value": settings.near_close_min_paper_signals_for_live},
                    {"label": "下單大小", "value": settings.near_close_order_size, "unit": "pUSD"},
                    {"label": "同市場曝險", "value": settings.near_close_max_market_exposure, "unit": "pUSD"},
                    {"label": "總曝險", "value": settings.near_close_max_total_exposure, "unit": "pUSD"},
                    {"label": "最大部位", "value": settings.near_close_max_position_size, "unit": "股"},
                    {"label": "Live 進場時間", "value": settings.near_close_live_max_minutes_to_end, "unit": "分"},
                    {"label": "掃描時間窗", "value": f"{settings.near_close_min_minutes_to_end:g}-{settings.near_close_max_minutes_to_end:g} 分"},
                    {"label": "最高 bid", "value": settings.near_close_max_bid_price},
                    {"label": "最低 ask", "value": settings.near_close_min_best_ask},
                    {"label": "最低 midpoint", "value": settings.near_close_min_midpoint},
                    {"label": "最大 spread", "value": settings.near_close_max_spread},
                    {"label": "最低淨邊際", "value": settings.near_close_min_net_edge},
                    {"label": "GTD 秒數", "value": settings.near_close_gtd_seconds, "unit": "秒"},
                    {"label": "reprice 門檻", "value": settings.near_close_reprice_threshold},
                    {"label": "reprice cooldown", "value": settings.near_close_reprice_cooldown_sec, "unit": "秒"},
                ],
            },
            {
                "title": "Crypto Up/Down",
                "items": [
                    {"label": "啟用", "value": "開" if settings.near_close_crypto_updown_enabled else "關"},
                    {"label": "下單大小", "value": settings.near_close_crypto_updown_order_size, "unit": "pUSD"},
                    {"label": "時間窗", "value": f"{settings.near_close_crypto_updown_min_minutes_to_end:g}-{settings.near_close_crypto_updown_max_minutes_to_end:g} 分"},
                    {"label": "start distance", "value": settings.near_close_crypto_updown_min_start_distance},
                    {"label": "取消 start distance", "value": settings.near_close_crypto_updown_cancel_start_distance},
                    {"label": "最低 ask", "value": settings.near_close_crypto_updown_min_best_ask},
                    {"label": "最低 midpoint", "value": settings.near_close_crypto_updown_min_midpoint},
                    {"label": "最大 spread", "value": settings.near_close_crypto_updown_max_spread},
                    {"label": "最高 bid", "value": settings.near_close_crypto_updown_max_bid_price},
                    {"label": "最低 depth", "value": settings.near_close_crypto_updown_min_depth},
                    {"label": "midpoint discount", "value": settings.near_close_crypto_updown_midpoint_discount},
                ],
            },
            {
                "title": "風控",
                "items": [
                    {"label": "單筆上限", "value": settings.max_notional_per_plan, "unit": "pUSD"},
                    {"label": "今日 Live 金額", "value": f"{float(risk_summary.get('live_notional_today') or 0):g} / {settings.max_daily_live_notional:g} pUSD"},
                    {"label": "今日 Live 筆數", "value": f"{int(risk_summary.get('live_orders_today') or 0)} / {settings.max_daily_live_orders}"},
                    {"label": "今日 paper 金額", "value": f"{float(risk_summary.get('paper_notional_today') or 0):g} / {settings.max_daily_paper_notional:g} pUSD"},
                    {"label": "今日 paper 筆數", "value": f"{int(risk_summary.get('paper_trades_today') or 0)} / {settings.max_daily_paper_trades}"},
                    {"label": "日損上限", "value": settings.near_close_daily_loss_limit, "unit": "pUSD"},
                    {"label": "連續虧損上限", "value": settings.near_close_max_consecutive_losses},
                ],
            },
        ],
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
    if _pid_running(_read_pid(WATCH_PID_FILE)) or _pid_running(_read_pid(WATCH_SUPERVISOR_PID_FILE)):
        return False
    RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
    if WATCH_SCRIPT_PATH.exists():
        stdout_log = RUNTIME_LOG_DIR / "watch-supervisor.start.stdout.log"
        stderr_log = RUNTIME_LOG_DIR / "watch-supervisor.start.stderr.log"
        with stdout_log.open("ab") as stdout, stderr_log.open("ab") as stderr:
            process = subprocess.Popen(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(WATCH_SCRIPT_PATH),
                ],
                cwd=str(BASE_DIR.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdout=stdout,
                stderr=stderr,
            )
        WATCH_SUPERVISOR_PID_FILE.write_text(str(process.pid), encoding="ascii")
        return True
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


def build_watch_status(
    settings: Settings,
    summary: dict[str, Any],
    latest_heartbeat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    supervisor_pid = _read_pid(WATCH_SUPERVISOR_PID_FILE)
    watch_pid = _read_pid(WATCH_PID_FILE)
    supervisor_running = _pid_running(supervisor_pid)
    watch_running = _pid_running(watch_pid)
    latest_scan_at = summary.get("latest_scan_at")
    latest_scan_dt = _parse_utc_timestamp(latest_scan_at)
    now = datetime.now(timezone.utc)
    max_scan_lag_sec = max(
        settings.watch_scan_timeout_sec + settings.watch_timeout_retry_sec + 30,
        settings.dashboard_refresh_sec * 2,
        90,
    )
    last_scan_age_sec = None
    scan_recent = False
    if latest_scan_dt is not None:
        last_scan_age_sec = max((now - latest_scan_dt).total_seconds(), 0.0)
        scan_recent = last_scan_age_sec <= max_scan_lag_sec
    heartbeat_details = latest_heartbeat.get("details", {}) if latest_heartbeat else {}
    heartbeat_state = str(latest_heartbeat.get("state") or "") if latest_heartbeat else ""
    heartbeat_message = str(latest_heartbeat.get("message") or "") if latest_heartbeat else ""
    heartbeat_created_at = latest_heartbeat.get("created_at") if latest_heartbeat else None
    heartbeat_dt = _parse_utc_timestamp(heartbeat_created_at)
    heartbeat_age_sec = None
    heartbeat_fresh = False
    if heartbeat_dt is not None:
        heartbeat_age_sec = max((now - heartbeat_dt).total_seconds(), 0.0)
        heartbeat_fresh = heartbeat_age_sec <= max_scan_lag_sec
    phase = str(heartbeat_details.get("phase") or heartbeat_state or "")
    pid_updated_at = _file_mtime_utc(WATCH_PID_FILE) or _file_mtime_utc(WATCH_SUPERVISOR_PID_FILE)
    startup_age_sec = None
    if pid_updated_at is not None:
        startup_age_sec = max((now - pid_updated_at).total_seconds(), 0.0)
    startup_grace_sec = max(settings.watch_scan_timeout_sec + 15, 45)

    running = watch_running and (scan_recent or heartbeat_fresh)
    if watch_running and heartbeat_fresh and phase == "scanning":
        state = "running"
        message = heartbeat_message or "watch 正在掃描。"
    elif watch_running and heartbeat_fresh and phase == "delay":
        state = "running"
        message = heartbeat_message or "watch 掃描完成，正在 delay 30 秒。"
    elif watch_running and heartbeat_fresh and phase == "timeout":
        state = "running"
        message = heartbeat_message or "watch 上一輪掃描超時，正在等待下一輪。"
    elif running:
        state = "running"
        message = "watch 背景監看正常運行中。"
    elif watch_running and latest_scan_dt is None and startup_age_sec is not None and startup_age_sec <= startup_grace_sec:
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
        "watch_scan_timeout_sec": settings.watch_scan_timeout_sec,
        "watch_delay_sec": settings.watch_timeout_retry_sec,
        "dashboard_refresh_sec": settings.dashboard_refresh_sec,
        "supervisor_pid": supervisor_pid,
        "watch_pid": watch_pid,
        "latest_scan_at": latest_scan_at,
        "last_scan_age_sec": round(last_scan_age_sec, 1) if last_scan_age_sec is not None else None,
        "heartbeat_age_sec": round(heartbeat_age_sec, 1) if heartbeat_age_sec is not None else None,
        "phase": phase,
        "phase_started_at": heartbeat_details.get("scan_started_at") or heartbeat_details.get("delay_started_at"),
        "phase_until": heartbeat_details.get("delay_until"),
        "latest_heartbeat": latest_heartbeat,
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
    sqlite_warning = path_sync_warning(settings.sqlite_path) if settings.persistence_backend == "sqlite" else None
    persistence_warning = bool(sqlite_warning) or (settings.persistence_backend == "sqlite" and bool(os.getenv("K_SERVICE")))
    repository.expire_open_orders_for_ended_markets()
    summary = repository.dashboard_summary()
    strategy_variant = _dashboard_strategy_variant(settings)
    watch_heartbeats = repository.recent_watch_heartbeats(limit=6)
    risk_summary = repository.trading_risk_summary()
    watch_status = build_watch_status(settings, summary, watch_heartbeats[0] if watch_heartbeats else None)
    opportunities = repository.latest_opportunities(
        limit=settings.dashboard_page_size,
        strategy_variant=strategy_variant,
    )
    return {
        "summary": summary,
        "strategies": repository.strategy_summary(strategy_variant=strategy_variant),
        "opportunities": opportunities,
        "alerts": repository.recent_alerts(limit=8),
        "markets": repository.top_markets(limit=10, shortlist_only=strategy_variant == "near_close_maker"),
        "watch_heartbeats": watch_heartbeats,
        "execution_events": repository.recent_execution_events(limit=10),
        "live_orders": repository.recent_live_orders(limit=20),
        "positions": repository.recent_live_positions(limit=10),
                "trade_groups": _apply_portfolio_position_values(repository.live_trade_groups(limit=8), wallet or {}),
        "open_positions": repository.open_live_positions(limit=12),
        "pnl": repository.settled_pnl_summary(),
        "trade_journal": _trade_journal_payload(repository, settings, wallet or {}),
        "refresh_sec": settings.dashboard_refresh_sec,
        "trading": controls.as_payload(),
        "risk": {
            **risk_summary,
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
        "trading_parameters": _trading_parameters_payload(
            settings,
            controls,
            summary=summary,
            risk_summary=risk_summary,
            preflight=preflight,
            watch=watch_status,
            opportunities=opportunities,
        ),
        "watch": watch_status,
        "persistence": {
            "backend": settings.persistence_backend,
            "cloud_warning": persistence_warning,
            "warning": sqlite_warning,
            "sqlite_path": str(settings.sqlite_path),
            "backup_dir": str(settings.sqlite_backup_dir),
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
    app.state.dashboard_db_task_started_at = None
    app.state.dashboard_payload_cache = None
    app.state.open_position_book_refresh_at = 0.0
    app.state.watch_task = None
    app.state.watch_stop_event = None
    app.state.auto_redeem_task = None
    app.state.auto_redeem_stop_event = None
    app.state.stop_exit_task = None
    app.state.stop_exit_stop_event = None
    app.state.stop_exit_lock = asyncio.Lock()
    app.state.embedded_watch_cycle_lock = threading.Lock()
    app.state.watch_action_lock = asyncio.Lock()
    app.state.watch_started_at = None
    app.state.watch_latest_scan_at = None
    app.state.watch_last_error = None
    app.state.default_controls = TradingControls.from_settings(current_settings)
    app.state.controls_override = None
    app.state.controls_sync_task = None
    app.state.live_fill_sync_at = 0.0
    app.state.live_fill_sync_error = None
    app.state.live_fill_sync_task = None
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
        fills: list[dict[str, Any]] = []
        activities: list[dict[str, Any]] = []
        if (current_settings.polymarket_private_key or "").strip():
            live_trader: PolymarketLiveTradingAdapter | None = getattr(app.state, "live_trader", None)
            if live_trader is not None:
                live_trader.settings = current_settings
                client = live_trader._get_authenticated_client()
            else:
                client = create_authenticated_clob_v2_client(current_settings)
            clob_fills = client.get_trades()
            if isinstance(clob_fills, list):
                fills = [fill for fill in clob_fills if isinstance(fill, dict)]
        funder_address = str(current_settings.polymarket_funder_address or "").strip()
        if funder_address:
            with httpx.Client(timeout=5.0) as client:
                response = client.get(
                    f"{current_settings.polymarket_data_api_base_url.rstrip('/')}/activity",
                    params={"user": funder_address, "limit": LIVE_FILL_ACTIVITY_LIMIT},
                )
                response.raise_for_status()
                payload = response.json()
            if isinstance(payload, list):
                activities = [item for item in payload if isinstance(item, dict)]
        with repository_scope() as repo:
            inserted = repo.save_clob_fills(fills, wallet_address=funder_address)
            inserted += repo.save_polymarket_activity_trades(activities, wallet_address=funder_address)
            return inserted

    async def maybe_sync_live_fills() -> int:
        loop_time = asyncio.get_running_loop().time()
        running_task = app.state.live_fill_sync_task
        if running_task is not None:
            if not running_task.done():
                return 0
            app.state.live_fill_sync_task = None
            try:
                inserted = running_task.result()
            except Exception as exc:
                app.state.live_fill_sync_error = str(exc)
                return 0
            app.state.live_fill_sync_error = None
            if inserted:
                app.state.wallet_cache_at = 0.0
                app.state.dashboard_payload_cache = None
                dashboard_task = app.state.dashboard_db_task
                if dashboard_task is not None and not dashboard_task.done():
                    dashboard_task.cancel()
                app.state.dashboard_db_task = None
                app.state.dashboard_db_task_started_at = None
            return inserted
        if loop_time - float(app.state.live_fill_sync_at) < LIVE_FILL_SYNC_INTERVAL_SEC:
            return 0
        app.state.live_fill_sync_at = loop_time
        task = asyncio.create_task(asyncio.to_thread(sync_live_fills_to_db))
        app.state.live_fill_sync_task = task
        try:
            inserted = await asyncio.wait_for(
                asyncio.shield(task),
                timeout=DASHBOARD_COMPONENT_TIMEOUT_SEC,
            )
            app.state.live_fill_sync_task = None
        except TimeoutError:
            app.state.live_fill_sync_error = "Live fill sync is still running; dashboard skipped this cycle."
            return 0
        except Exception as exc:
            app.state.live_fill_sync_task = None
            app.state.live_fill_sync_error = str(exc)
            return 0
        app.state.live_fill_sync_error = None
        if inserted:
            app.state.wallet_cache_at = 0.0
            app.state.dashboard_payload_cache = None
            running_task = app.state.dashboard_db_task
            if running_task is not None and not running_task.done():
                running_task.cancel()
            app.state.dashboard_db_task = None
            app.state.dashboard_db_task_started_at = None
        return inserted

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

    def watch_status_payload(
        summary: dict[str, Any] | None = None,
        latest_heartbeat: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        summary = dict(summary or {})
        external_status = build_watch_status(current_settings, summary, latest_heartbeat)
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
        elif (
            running_task
            and latest_scan_dt is None
            and startup_age_sec is not None
            and startup_age_sec <= startup_grace_sec
        ):
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
            "watch_scan_timeout_sec": current_settings.watch_scan_timeout_sec,
            "watch_delay_sec": current_settings.watch_timeout_retry_sec,
            "dashboard_refresh_sec": current_settings.dashboard_refresh_sec,
            "supervisor_pid": external_status["supervisor_pid"],
            "watch_pid": external_status["watch_pid"],
            "latest_scan_at": latest_scan_at,
            "phase": external_status.get("phase"),
            "phase_started_at": external_status.get("phase_started_at"),
            "phase_until": external_status.get("phase_until"),
            "latest_heartbeat": latest_heartbeat or external_status.get("latest_heartbeat"),
            "last_scan_age_sec": round(last_scan_age_sec, 1) if last_scan_age_sec is not None else None,
            "startup_age_sec": round(startup_age_sec, 1) if startup_age_sec is not None else None,
            "startup_grace_sec": startup_grace_sec,
            "max_scan_lag_sec": max_scan_lag_sec,
            "message": message,
        }

    def run_watch_cycle_once() -> datetime:
        cycle_lock = app.state.embedded_watch_cycle_lock
        if not cycle_lock.acquire(blocking=False):
            raise RuntimeError("previous watch cycle is still running; skipped overlapping scan")
        scan_started_at = datetime.now(timezone.utc)
        try:
            with repository_scope() as repo:
                repo.save_watch_heartbeat(
                    source="dashboard",
                    state="scanning",
                    message="watch scan started",
                    details={
                        "phase": "scanning",
                        "scan_started_at": scan_started_at.isoformat(),
                        "timeout_sec": EMBEDDED_WATCH_SCAN_TIMEOUT_SEC,
                        "delay_sec": current_settings.watch_timeout_retry_sec,
                    },
                )
                result = asyncio.run(
                    asyncio.wait_for(
                        execute_scan_cycle(
                            current_settings,
                            limit=current_settings.watch_market_limit,
                            repository=repo,
                        ),
                        timeout=EMBEDDED_WATCH_SCAN_TIMEOUT_SEC,
                    )
                )
                persist_scan_cycle(repo, result, current_settings)
                repo.save_watch_heartbeat(
                    source="dashboard",
                    state="running",
                    latest_scan_at=scan_started_at,
                    message="watch scan completed; live execution pending",
                    details={
                        "phase": "completed",
                        "scan_started_at": scan_started_at.isoformat(),
                        "scan_completed_at": getattr(result, "executed_at", datetime.now(timezone.utc)).isoformat(),
                        "delay_sec": current_settings.watch_timeout_retry_sec,
                        "monitored_markets": len(getattr(result, "shortlisted_markets", []) or []),
                        "book_count": len(getattr(result, "books", {}) or {}),
                        "opportunity_count": len(getattr(result, "opportunities", []) or []),
                        "live_attempted_count": None,
                        "live_submitted_count": None,
                    },
                )
                controls = load_controls(repo)
                try:
                    execution_summary, updated_controls = asyncio.run(
                        asyncio.wait_for(
                            execute_live_from_scan(
                                repo,
                                opportunities=getattr(result, "opportunities", []),
                                controls=controls,
                            ),
                            timeout=EMBEDDED_WATCH_LIVE_TIMEOUT_SEC,
                        )
                    )
                    if updated_controls is not controls:
                        app.state.controls_override = updated_controls
                except TimeoutError:
                    execution_summary = {"attempted_count": 0, "submitted_count": 0}
                    repo.save_execution_event(
                        source="dashboard",
                        mode="live",
                        opportunity_id=None,
                        status="live_execution_timeout",
                        message=f"Embedded watch live execution exceeded {EMBEDDED_WATCH_LIVE_TIMEOUT_SEC:.0f}s; next scan will continue.",
                        details={
                            "scan_started_at": scan_started_at.isoformat(),
                            "timeout_sec": EMBEDDED_WATCH_LIVE_TIMEOUT_SEC,
                        },
                    )
                repo.save_watch_heartbeat(
                    source="dashboard",
                    state="running",
                    latest_scan_at=scan_started_at,
                    message="watch scan completed",
                    details={
                        "phase": "completed",
                        "scan_started_at": scan_started_at.isoformat(),
                        "scan_completed_at": getattr(result, "executed_at", datetime.now(timezone.utc)).isoformat(),
                        "delay_sec": current_settings.watch_timeout_retry_sec,
                        "monitored_markets": len(getattr(result, "shortlisted_markets", []) or []),
                        "book_count": len(getattr(result, "books", {}) or {}),
                        "opportunity_count": len(getattr(result, "opportunities", []) or []),
                        "live_attempted_count": int(execution_summary.get("attempted_count", 0) or 0),
                        "live_submitted_count": int(execution_summary.get("submitted_count", 0) or 0),
                    },
                )
            return scan_started_at
        finally:
            cycle_lock.release()

    def run_background_auto_redeem_once() -> list[dict[str, Any]]:
        try:
            sync_live_fills_to_db()
        except Exception as exc:
            with repository_scope() as repo:
                repo.save_execution_event(
                    source="auto-redeem",
                    mode="live",
                    opportunity_id=None,
                    status="fill_sync_failed",
                    message=str(exc),
                    details={"trigger": "background_auto_redeem"},
                )
        with repository_scope() as repo:
            return run_auto_redeem_for_scan(repo, trigger="background_auto_redeem")

    async def background_auto_redeem_loop(stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if current_settings.auto_redeem_enabled:
                await asyncio.to_thread(run_background_auto_redeem_once)
            if stop_event.is_set():
                break
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=max(int(current_settings.auto_redeem_refresh_sec), 60),
                )
            except TimeoutError:
                pass

    async def run_near_close_stop_exit_once() -> None:
        if app.state.stop_exit_lock.locked():
            return
        async with app.state.stop_exit_lock:
            with repository_scope() as repo:
                controls = load_controls(repo)
                runtime_settings = controls.apply(current_settings)
                groups = repo.near_close_stop_exit_groups(limit=50)
                token_ids = [
                    str(group.get("token_id") or "")
                    for group in groups
                    if float(group.get("open_size") or 0.0) > 1e-9 and str(group.get("token_id") or "")
                ]
            token_ids = list(dict.fromkeys(token_ids))
            if not token_ids:
                return
            books = await asyncio.wait_for(
                _fetch_orderbooks_for_tokens(runtime_settings, token_ids),
                timeout=NEAR_CLOSE_STOP_EXIT_TIMEOUT_SEC,
            )
            with repository_scope() as repo:
                if books:
                    repo.save_orderbooks(books.values())
                live_trader: PolymarketLiveTradingAdapter = app.state.live_trader
                live_trader.settings = runtime_settings
                exits = await asyncio.wait_for(
                    execute_near_close_taker_exits(
                        repository=repo,
                        live_trader=live_trader,
                        settings=runtime_settings,
                        watch_books=books,
                    ),
                    timeout=NEAR_CLOSE_STOP_EXIT_TIMEOUT_SEC,
                )
                if exits:
                    repo.save_execution_event(
                        source="watch",
                        mode="live",
                        opportunity_id=None,
                        status="stop_exit_checked",
                        message=f"Near-close stop exit submitted for {len(exits)} position(s).",
                        details={"exits": exits},
                    )

    async def near_close_stop_exit_loop(stop_event: asyncio.Event) -> None:
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=NEAR_CLOSE_STOP_EXIT_INTERVAL_SEC)
        except TimeoutError:
            pass
        while not stop_event.is_set():
            try:
                external_watch_running = any(_pid_running(pid) for pid in _iter_watch_processes())
                if not external_watch_running:
                    await run_near_close_stop_exit_once()
            except Exception as exc:
                try:
                    with repository_scope() as repo:
                        repo.save_execution_event(
                            source="watch",
                            mode="live",
                            opportunity_id=None,
                            status="stop_exit_failed",
                            message=str(exc),
                            details={"trigger_price": current_settings.near_close_taker_exit_price},
                        )
                except Exception:
                    pass
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=NEAR_CLOSE_STOP_EXIT_INTERVAL_SEC)
            except TimeoutError:
                pass

    async def embedded_watch_loop(stop_event: asyncio.Event) -> None:
        app.state.watch_started_at = datetime.now(timezone.utc)
        app.state.watch_last_error = None
        try:
            while not stop_event.is_set():
                try:
                    executed_at = await asyncio.wait_for(
                        asyncio.to_thread(run_watch_cycle_once),
                        timeout=EMBEDDED_WATCH_CYCLE_TIMEOUT_SEC,
                    )
                    app.state.watch_latest_scan_at = executed_at.isoformat()
                    app.state.watch_last_error = None
                except TimeoutError:
                    app.state.watch_last_error = (
                        f"watch 單輪掃描超過 {EMBEDDED_WATCH_CYCLE_TIMEOUT_SEC:.0f}s，已跳過並 delay {current_settings.watch_timeout_retry_sec:.0f} 秒。"
                    )
                    try:
                        with repository_scope() as repo:
                            repo.save_watch_heartbeat(
                                source="dashboard",
                                state="error",
                                message=app.state.watch_last_error,
                                details={
                                    "phase": "timeout",
                                    "timeout_sec": EMBEDDED_WATCH_CYCLE_TIMEOUT_SEC,
                                    "delay_sec": current_settings.watch_timeout_retry_sec,
                                },
                            )
                    except Exception:
                        pass
                except Exception as exc:
                    app.state.watch_last_error = f"watch 掃描失敗：{exc}"
                    try:
                        with repository_scope() as repo:
                            repo.save_watch_heartbeat(
                                source="dashboard",
                                state="error",
                                message=str(exc),
                            )
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=current_settings.watch_timeout_retry_sec,
                    )
                except TimeoutError:
                    pass
        finally:
            app.state.watch_stop_event = None

    @app.on_event("startup")
    async def start_background_tasks() -> None:
        redeem_stop_event = asyncio.Event()
        app.state.auto_redeem_stop_event = redeem_stop_event
        app.state.auto_redeem_task = asyncio.create_task(background_auto_redeem_loop(redeem_stop_event))
        app.state.stop_exit_stop_event = None
        app.state.stop_exit_task = None

    @app.on_event("shutdown")
    async def stop_background_tasks() -> None:
        stop_event = app.state.auto_redeem_stop_event
        if stop_event is not None:
            stop_event.set()
        task = app.state.auto_redeem_task
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        stop_exit_stop_event = app.state.stop_exit_stop_event
        if stop_exit_stop_event is not None:
            stop_exit_stop_event.set()
        stop_exit_task = app.state.stop_exit_task
        if stop_exit_task is not None:
            stop_exit_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stop_exit_task

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
            "watch_heartbeats": [],
            "execution_events": [],
            "live_orders": [],
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
            "trading_parameters": _trading_parameters_payload(
                current_settings,
                controls,
                summary={},
                risk_summary={},
                preflight=preflight if preflight is not None else timed_out_preflight_payload(),
                watch=watch_status_payload({}),
            ),
            "watch": watch_status_payload({}),
            "persistence": {
                "backend": current_settings.persistence_backend,
                "cloud_warning": current_settings.persistence_backend == "sqlite",
                "warning": reason,
            },
            "scan_in_progress": app.state.scan_lock.locked(),
        }

    def cached_dashboard_payload(
        *,
        reason: str,
        preflight: dict[str, Any],
        wallet: dict[str, Any],
    ) -> dict[str, Any]:
        cached = app.state.dashboard_payload_cache
        if cached is None:
            return fallback_dashboard_payload(reason=reason, preflight=preflight, wallet=wallet)
        payload = copy.deepcopy(cached)
        payload["preflight"] = preflight
        payload["wallet"] = wallet
        payload["scan_in_progress"] = app.state.scan_lock.locked()
        payload["data_stale"] = True
        payload["warning"] = reason
        summary = payload.get("summary")
        if isinstance(summary, dict):
            summary["warning"] = reason
        return payload

    def load_dashboard_payload_from_db(preflight: dict[str, Any], wallet: dict[str, Any]) -> dict[str, Any]:
        with repository_scope() as repo:
            controls = load_controls(repo)
            external_watch_running = (
                current_settings.sqlite_path == get_settings().sqlite_path
                and any(_pid_running(pid) for pid in _iter_watch_processes())
            )
            refresh_due = (
                not external_watch_running
                and (
                time.monotonic() - float(app.state.open_position_book_refresh_at)
                )
                >= OPEN_POSITION_BOOK_REFRESH_INTERVAL_SEC
            )
            if refresh_due:
                try:
                    refresh_open_position_orderbooks(repo, current_settings)
                    app.state.open_position_book_refresh_at = time.monotonic()
                except Exception as exc:
                    repo.save_execution_event(
                        source="dashboard",
                        mode="live",
                        opportunity_id=None,
                        status="price_refresh_failed",
                        message=f"開倉部位即時價格刷新失敗：{exc}",
                        details={"component": "open_position_orderbook_refresh"},
                    )
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
            sqlite_warning = (
                path_sync_warning(current_settings.sqlite_path)
                if current_settings.persistence_backend == "sqlite"
                else None
            )
            persistence_warning = bool(sqlite_warning) or (
                current_settings.persistence_backend == "sqlite" and bool(os.getenv("K_SERVICE"))
            )
            summary = repo.dashboard_summary()
            strategy_variant = _dashboard_strategy_variant(current_settings)
            watch_heartbeats = repo.recent_watch_heartbeats(limit=6)
            risk_summary = repo.trading_risk_summary()
            watch_status = watch_status_payload(summary, watch_heartbeats[0] if watch_heartbeats else None)
            opportunities = repo.latest_opportunities(
                limit=current_settings.dashboard_page_size,
                strategy_variant=strategy_variant,
            )
            return {
                "summary": summary,
                "strategies": repo.strategy_summary(strategy_variant=strategy_variant),
                "opportunities": opportunities,
                "alerts": repo.recent_alerts(limit=8),
                "markets": repo.top_markets(limit=10, shortlist_only=strategy_variant == "near_close_maker"),
                "watch_heartbeats": watch_heartbeats,
                "execution_events": repo.recent_execution_events(limit=10),
                "live_orders": repo.recent_live_orders(limit=20),
                "positions": repo.recent_live_positions(limit=10),
                "trade_groups": _apply_portfolio_position_values(repo.live_trade_groups(limit=8), wallet),
                "open_positions": repo.open_live_positions(limit=12),
                "pnl": repo.settled_pnl_summary(),
                "trade_journal": _trade_journal_payload(repo, current_settings, wallet),
                "refresh_sec": current_settings.dashboard_refresh_sec,
                "trading": controls.as_payload(),
                "risk": {
                    **risk_summary,
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
                "trading_parameters": _trading_parameters_payload(
                    current_settings,
                    controls,
                    summary=summary,
                    risk_summary=risk_summary,
                    preflight=preflight,
                    watch=watch_status,
                    opportunities=opportunities,
                ),
                "watch": watch_status,
                "persistence": {
                    "backend": current_settings.persistence_backend,
                    "cloud_warning": persistence_warning,
                    "warning": sqlite_warning,
                    "sqlite_path": str(current_settings.sqlite_path),
                    "backup_dir": str(current_settings.sqlite_backup_dir),
                },
            }

    async def dashboard_payload() -> dict[str, Any]:
        try:
            preflight, wallet = await asyncio.wait_for(
                asyncio.gather(dashboard_preflight(), dashboard_wallet_status()),
                timeout=DASHBOARD_COMPONENT_TIMEOUT_SEC,
            )
        except TimeoutError:
            preflight = timed_out_preflight_payload(app.state.preflight_cache)
            wallet = timed_out_wallet_payload(app.state.wallet_cache)
        synced_live_fills = await maybe_sync_live_fills()
        running_task = app.state.dashboard_db_task
        if synced_live_fills <= 0 and running_task is not None and not running_task.done():
            started_at = app.state.dashboard_db_task_started_at
            task_age = asyncio.get_running_loop().time() - float(started_at or asyncio.get_running_loop().time())
            if task_age > max(DASHBOARD_DB_TIMEOUT_SEC * 3.0, 10.0):
                running_task.cancel()
                app.state.dashboard_db_task = None
                app.state.dashboard_db_task_started_at = None
            else:
                return cached_dashboard_payload(
                    reason="Dashboard database is still warming up; showing a lightweight startup view.",
                    preflight=preflight,
                    wallet=wallet,
                )
        running_task = app.state.dashboard_db_task
        if synced_live_fills <= 0 and running_task is not None and not running_task.done():
            return cached_dashboard_payload(
                reason="Dashboard database is still warming up; showing a lightweight startup view.",
                preflight=preflight,
                wallet=wallet,
            )
        if synced_live_fills <= 0 and running_task is not None and running_task.done():
            app.state.dashboard_db_task = None
            app.state.dashboard_db_task_started_at = None
            try:
                payload = running_task.result()
            except BaseException:
                payload = None
            if payload is not None:
                payload["scan_in_progress"] = app.state.scan_lock.locked()
                app.state.dashboard_payload_cache = copy.deepcopy(payload)
                return payload

        task = asyncio.create_task(asyncio.to_thread(load_dashboard_payload_from_db, preflight, wallet))
        app.state.dashboard_db_task = task
        app.state.dashboard_db_task_started_at = asyncio.get_running_loop().time()
        try:
            payload = await asyncio.wait_for(asyncio.shield(task), timeout=DASHBOARD_DB_TIMEOUT_SEC)
        except TimeoutError:
            return cached_dashboard_payload(
                reason="Dashboard database response timed out; showing a lightweight startup view.",
                preflight=preflight,
                wallet=wallet,
            )
        except asyncio.CancelledError:
            app.state.dashboard_db_task = None
            app.state.dashboard_db_task_started_at = None
            return cached_dashboard_payload(
                reason="Dashboard database refresh was cancelled; keeping the last good payload.",
                preflight=preflight,
                wallet=wallet,
            )
        app.state.dashboard_db_task = None
        app.state.dashboard_db_task_started_at = None
        payload["scan_in_progress"] = app.state.scan_lock.locked()
        app.state.dashboard_payload_cache = copy.deepcopy(payload)
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

    def run_auto_redeem_for_scan(repo: ScannerRepository, *, trigger: str) -> list[dict[str, Any]]:
        try:
            redeem_results = run_auto_redeem_once(current_settings, repo)
        except Exception as exc:
            repo.save_execution_event(
                source="auto-redeem",
                mode="live",
                opportunity_id=None,
                status="failed",
                message=str(exc),
                details={"trigger": trigger},
            )
            return [
                {
                    "status": "failed",
                    "message": str(exc),
                    "trigger": trigger,
                }
            ]
        summaries: list[dict[str, Any]] = []
        for redeem_result in redeem_results:
            summaries.append(
                {
                    "status": redeem_result.status,
                    "market_slug": redeem_result.market_slug,
                    "outcome_label": redeem_result.outcome_label,
                    "redeemed_size": redeem_result.redeemed_size,
                    "message": redeem_result.message,
                    "trigger": trigger,
                }
            )
        return summaries

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
        summary: dict[str, Any] = {}
        latest_heartbeat: dict[str, Any] | None = None
        try:
            def load_health_summary() -> tuple[dict[str, Any], dict[str, Any] | None]:
                with repository_scope() as repo:
                    heartbeats = repo.recent_watch_heartbeats(limit=1)
                    return repo.dashboard_summary(), heartbeats[0] if heartbeats else None

            summary, latest_heartbeat = await asyncio.wait_for(
                asyncio.to_thread(load_health_summary),
                timeout=DASHBOARD_COMPONENT_TIMEOUT_SEC,
            )
        except Exception:
            summary = {}
        watch_status = watch_status_payload(summary, latest_heartbeat)
        return JSONResponse(
            {
                "status": "ok",
                "scan_in_progress": app.state.scan_lock.locked(),
                "latest_snapshot_at": summary.get("latest_snapshot_at"),
                "latest_alert_at": summary.get("latest_alert_at"),
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

    @app.post("/api/actions/trading/finish")
    async def api_finish_work() -> JSONResponse:
        await set_runtime_controls_quick(
            TradingControls(
                live_trading_enabled=False,
                auto_execute_enabled=False,
                kill_switch_enabled=False,
            )
        )
        cancelled_count = 0
        cancel_error = None
        order_ids: list[str] = []
        with repository_scope() as repo:
            order_ids.extend(repo.active_live_order_ids())
        try:
            live_trader: PolymarketLiveTradingAdapter = app.state.live_trader
            open_orders = await asyncio.wait_for(live_trader.get_open_orders(), timeout=DASHBOARD_DB_TIMEOUT_SEC)
            order_ids.extend(_extract_order_ids(open_orders))
            order_ids = list(dict.fromkeys(order_ids))
            if order_ids:
                await asyncio.wait_for(live_trader.cancel_orders(order_ids), timeout=DASHBOARD_DB_TIMEOUT_SEC)
                with repository_scope() as repo:
                    cancelled_count = repo.mark_live_orders_cancelled(order_ids)
        except Exception as exc:
            cancel_error = str(exc)
        watch_stopped = _stop_watch_process()

        with repository_scope() as repo:
            repo.save_execution_event(
                source="dashboard",
                mode="live",
                opportunity_id=None,
                status="finish_failed" if cancel_error else "finish_completed",
                message=(
                    f"收工失敗：{cancel_error}"
                    if cancel_error
                    else f"收工完成：已關閉 Live / 自動下單，撤單 {cancelled_count} 筆。"
                ),
                details={
                    "order_ids": order_ids,
                    "cancelled_count": cancelled_count,
                    "error": cancel_error,
                    "watch_stopped": watch_stopped,
                },
            )
        if cancel_error:
            return JSONResponse(
                {
                    "status": "error",
                    "error": cancel_error,
                    "payload": action_payload("收工已停止繼續下單，但撤單狀態需要人工確認。"),
                },
                status_code=502,
            )
        return JSONResponse({"status": "ok", "payload": action_payload("收工完成。")})

    @app.post("/api/actions/watch")
    async def api_toggle_watch() -> JSONResponse:
        async with app.state.watch_action_lock:
            external_watch_running = any(_pid_running(pid) for pid in _iter_watch_processes())
            if watch_task_running() or external_watch_running:
                stop_event = app.state.watch_stop_event
                if stop_event is not None:
                    stop_event.set()
                app.state.watch_task = None
                app.state.watch_stop_event = None
                _stop_watch_process()
            else:
                _start_watch_process(current_settings)
                await asyncio.sleep(1.0)
        return JSONResponse({"status": "ok", "payload": await dashboard_payload()})

    @app.post("/api/actions/scan")
    async def api_scan(limit: int | None = None) -> JSONResponse:
        if app.state.scan_lock.locked():
            raise HTTPException(status_code=409, detail="scan_already_running")
        async with app.state.scan_lock:
            def run_scan_cycle_for_dashboard() -> Any:
                with repository_scope() as scan_repo:
                    return asyncio.run(
                        asyncio.wait_for(
                            execute_scan_cycle(
                                current_settings,
                                limit=limit or current_settings.dashboard_scan_limit,
                                repository=scan_repo,
                            ),
                            timeout=EMBEDDED_WATCH_SCAN_TIMEOUT_SEC,
                        )
                    )

            result = await asyncio.to_thread(run_scan_cycle_for_dashboard)
            with repository_scope() as repo:
                persist_scan_cycle(repo, result, current_settings)
                redeem_summary = {"status": "background_loop"}
                controls = load_controls(repo)
                try:
                    execution_summary, controls = await asyncio.wait_for(
                        execute_live_from_scan(
                            repo,
                            opportunities=result.opportunities,
                            controls=controls,
                        ),
                        timeout=EMBEDDED_WATCH_LIVE_TIMEOUT_SEC,
                    )
                except TimeoutError:
                    execution_summary = {"attempted_count": 0, "submitted_count": 0, "items": []}
                    repo.save_execution_event(
                        source="dashboard",
                        mode="live",
                        opportunity_id=None,
                        status="live_execution_timeout",
                        message=f"Manual scan live execution exceeded {EMBEDDED_WATCH_LIVE_TIMEOUT_SEC:.0f}s.",
                        details={"timeout_sec": EMBEDDED_WATCH_LIVE_TIMEOUT_SEC},
                    )
                payload = await build_dashboard_payload(
                    repo,
                    current_settings,
                    controls,
                    preflight=await dashboard_preflight(),
                    wallet=await dashboard_wallet_status(),
                )
        payload["scan_in_progress"] = app.state.scan_lock.locked()
        return JSONResponse(
            {
                "status": "ok",
                "payload": payload,
                "execution_summary": execution_summary,
                "redeem_summary": redeem_summary,
            }
        )

    return app


app = create_app()
