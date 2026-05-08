from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any

import httpx
from eth_account import Account

from app.config import Settings
from app.services.onchain import (
    NATIVE_TOKEN_DECIMALS,
    TOKEN_DECIMALS,
    fetch_chain_id,
    fetch_erc20_allowance,
    fetch_erc20_balance,
    fetch_native_balance,
    format_units,
)


LEGACY_STACK_LABEL = "Legacy V1 / USDC.e / py_clob_client"
V2_STACK_LABEL = "CLOB V2 / pUSD / py-clob-client-v2"
V2_PUSD_SPENDER_ADDRESSES = (
    "0xE111180000d2663C0091e4f400237545B87B996B",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    "0xe2222d279d744050d28e00520010520000310F59",
)
_CLOCK_CHECK_CACHE: dict[str, tuple[float, "PreflightCheck"]] = {}


@dataclass(slots=True)
class PreflightCheck:
    """Single live-trading preflight check rendered in CLI/UI payloads."""

    check_id: str
    label: str
    status: str
    message: str
    required: bool = True
    value: float | str | dict[str, float] | None = None
    threshold: float | str | None = None

    @property
    def blocking(self) -> bool:
        return self.required and self.status == "critical"

    def as_payload(self) -> dict[str, Any]:
        return {
            "id": self.check_id,
            "label": self.label,
            "status": self.status,
            "message": self.message,
            "required": self.required,
            "value": self.value,
            "threshold": self.threshold,
        }


@dataclass(slots=True)
class PreflightReport:
    """Aggregated read-only safety report before live trading can be armed."""

    generated_at: datetime
    address: str | None
    funder_address: str | None
    collateral_symbol: str
    checks: list[PreflightCheck]

    @property
    def ready(self) -> bool:
        return not any(check.blocking for check in self.checks)

    @property
    def blocking_reasons(self) -> list[str]:
        return [check.message for check in self.checks if check.blocking]

    def as_payload(self) -> dict[str, Any]:
        checks = [
            check
            for check in self.checks
            if not (self.collateral_symbol == "pUSD" and check.check_id == "usdce_balance")
        ]
        return {
            "ready": self.ready,
            "generated_at": self.generated_at.isoformat(),
            "address": self.address,
            "funder_address": self.funder_address,
            "collateral_symbol": self.collateral_symbol,
            "blocking_reasons": self.blocking_reasons,
            "checks": [check.as_payload() for check in checks],
        }


def _critical(check_id: str, label: str, message: str, **kwargs: Any) -> PreflightCheck:
    return PreflightCheck(check_id=check_id, label=label, status="critical", message=message, **kwargs)


def _warning(check_id: str, label: str, message: str, **kwargs: Any) -> PreflightCheck:
    return PreflightCheck(check_id=check_id, label=label, status="warning", message=message, **kwargs)


def _ok(check_id: str, label: str, message: str, **kwargs: Any) -> PreflightCheck:
    return PreflightCheck(check_id=check_id, label=label, status="ok", message=message, **kwargs)


def _clob_v2_sdk_error() -> str | None:
    try:
        importlib.import_module("py_clob_client_v2")
    except Exception as exc:
        return str(exc)
    return None


async def _check_clob_clock(
    client: httpx.AsyncClient,
    settings: Settings,
    now: datetime,
) -> PreflightCheck:
    cache_key = f"{settings.clob_base_url}|{settings.max_clock_drift_sec}"
    cached = _CLOCK_CHECK_CACHE.get(cache_key)
    if cached is not None:
        cached_at, cached_check = cached
        if monotonic() - cached_at < settings.clock_drift_cache_sec:
            return cached_check
    try:
        response = await client.get(settings.clob_base_url.rstrip("/") + "/", timeout=8.0)
        server_date = response.headers.get("date")
        if not server_date:
            check = _warning(
                "clock_drift",
                "系統時間",
                "CLOB 未回傳 Date header，暫時無法比對本機時間。",
                required=False,
            )
        else:
            server_time = parsedate_to_datetime(server_date)
            if server_time.tzinfo is None:
                server_time = server_time.replace(tzinfo=timezone.utc)
            drift = abs((now - server_time.astimezone(timezone.utc)).total_seconds())
            if drift > settings.max_clock_drift_sec:
                check = _critical(
                    "clock_drift",
                    "系統時間",
                    f"本機時間與 CLOB 相差 {drift:.0f} 秒，可能造成簽名失敗。",
                    value=round(drift, 2),
                    threshold=settings.max_clock_drift_sec,
                )
            else:
                check = _ok(
                    "clock_drift",
                    "系統時間",
                    f"本機時間偏差 {drift:.0f} 秒。",
                    value=round(drift, 2),
                    threshold=settings.max_clock_drift_sec,
                )
    except Exception as exc:
        check = _warning(
            "clock_drift",
            "系統時間",
            f"暫時無法讀取 CLOB 時間：{exc}",
            required=False,
        )
    _CLOCK_CHECK_CACHE[cache_key] = (monotonic(), check)
    return check


async def load_preflight_report(
    settings: Settings,
    *,
    verify_clob_credentials: bool = True,
    now: datetime | None = None,
) -> PreflightReport:
    """Run read-only checks required before live trading can be armed."""

    current_time = now or datetime.now(timezone.utc)
    checks: list[PreflightCheck] = []
    private_key = (settings.polymarket_private_key or "").strip()
    cutover = settings.clob_v2_cutover_utc.astimezone(timezone.utc)
    after_v2_cutover = current_time >= cutover
    collateral_symbol = "pUSD" if after_v2_cutover else "USDC.e"

    if settings.risk_kill_switch:
        checks.append(_critical("kill_switch", "風控開關", "Risk kill switch 已啟動，Live 交易會被鎖住。"))
    else:
        checks.append(_ok("kill_switch", "風控開關", "Kill switch 未啟動。"))

    if after_v2_cutover:
        sdk_error = _clob_v2_sdk_error()
        if sdk_error:
            checks.append(
                _critical(
                    "live_stack",
                    "交易堆疊",
                    f"缺少 {V2_STACK_LABEL} SDK，Live 送單仍不能啟用：{sdk_error}",
                    value=V2_STACK_LABEL,
                )
            )
        else:
            checks.append(
                _ok(
                    "live_stack",
                    "交易堆疊",
                    f"Live adapter 已升級為 {V2_STACK_LABEL}。",
                    value=V2_STACK_LABEL,
                )
            )
    else:
        checks.append(
            _warning(
                "live_stack",
                "交易堆疊",
                f"目前 Live 交易使用 {LEGACY_STACK_LABEL}；在 cutover {cutover.isoformat()} 之前可用，之後需升級。",
                required=False,
                value=LEGACY_STACK_LABEL,
            )
        )

    if not private_key:
        checks.append(_critical("private_key", "私鑰", "尚未載入私鑰。"))
        return PreflightReport(current_time, None, None, collateral_symbol, checks)

    try:
        normalized_key = private_key if private_key.startswith("0x") else f"0x{private_key}"
        address = Account.from_key(normalized_key).address
        checks.append(_ok("private_key", "私鑰", "私鑰格式可讀取。"))
    except Exception:
        checks.append(_critical("private_key", "私鑰", "私鑰格式無法解析。"))
        return PreflightReport(current_time, None, None, collateral_symbol, checks)

    funder = (settings.polymarket_funder_address or "").strip() or None
    if settings.polymarket_signature_type == 0:
        funder = funder or address
        checks.append(_ok("funder", "Funder 地址", "EOA 模式會使用私鑰對應地址作為 funder。", value=funder))
    elif funder:
        checks.append(_ok("funder", "Funder 地址", "已設定 funder 地址。", value=funder))
    else:
        checks.append(_critical("funder", "Funder 地址", "非 EOA 簽名模式必須設定 POLYMARKET_FUNDER_ADDRESS。"))

    rpc_url = settings.polygon_rpc_url.strip()
    if not rpc_url:
        checks.append(_critical("polygon_rpc", "Polygon RPC", "尚未設定 POLYGON_RPC_URL。"))
        return PreflightReport(current_time, address, funder, collateral_symbol, checks)

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            chain_id = await fetch_chain_id(client, rpc_url)
            if chain_id != settings.polymarket_chain_id:
                checks.append(
                    _critical(
                        "polygon_chain",
                        "Polygon Chain ID",
                        f"RPC chain id 為 {chain_id}，但目前設定要求 {settings.polymarket_chain_id}。",
                        value=chain_id,
                        threshold=settings.polymarket_chain_id,
                    )
                )
            else:
                checks.append(_ok("polygon_chain", "Polygon Chain ID", "RPC 已連到 Polygon 主網。", value=chain_id))
        except Exception as exc:
            checks.append(_critical("polygon_chain", "Polygon Chain ID", f"Polygon RPC 讀取失敗：{exc}"))

        token_results = await asyncio.gather(
            fetch_native_balance(client, rpc_url, address),
            fetch_erc20_balance(client, rpc_url, settings.polygon_usdc_e_token_address, address),
            fetch_erc20_balance(client, rpc_url, settings.polygon_pusd_token_address, address),
            return_exceptions=True,
        )

        if isinstance(token_results[0], Exception):
            checks.append(_critical("pol_balance", "POL Gas", f"POL 餘額讀取失敗：{token_results[0]}"))
        else:
            pol_balance = format_units(token_results[0], NATIVE_TOKEN_DECIMALS)
            if pol_balance < settings.min_pol_balance:
                checks.append(
                    _critical(
                        "pol_balance",
                        "POL Gas",
                        f"POL 餘額 {pol_balance:.6f} 低於最低需求 {settings.min_pol_balance}。",
                        value=round(pol_balance, 8),
                        threshold=settings.min_pol_balance,
                    )
                )
            else:
                checks.append(
                    _ok(
                        "pol_balance",
                        "POL Gas",
                        f"POL 餘額 {pol_balance:.6f}。",
                        value=round(pol_balance, 8),
                        threshold=settings.min_pol_balance,
                    )
                )

        if isinstance(token_results[2], Exception):
            checks.append(_critical("collateral_ready", "pUSD 餘額", f"pUSD 讀取失敗：{token_results[2]}"))
        else:
            pusd_balance = format_units(token_results[2], TOKEN_DECIMALS)
            if pusd_balance < settings.min_trading_collateral:
                checks.append(
                    _critical(
                        "collateral_ready",
                        "pUSD 餘額",
                        f"pUSD 餘額 {pusd_balance:.2f} 低於最低需求 {settings.min_trading_collateral}。",
                        value=round(pusd_balance, 4),
                        threshold=settings.min_trading_collateral,
                    )
                )
            else:
                checks.append(
                    _ok(
                        "collateral_ready",
                        "pUSD 餘額",
                        f"pUSD 可用餘額 {pusd_balance:.2f}。",
                        value=round(pusd_balance, 4),
                        threshold=settings.min_trading_collateral,
                    )
                )

        if isinstance(token_results[1], Exception):
            checks.append(_warning("usdce_balance", "USDC.e", f"USDC.e 讀取失敗：{token_results[1]}", required=False))
        else:
            usdc_e_balance = format_units(token_results[1], TOKEN_DECIMALS)
            checks.append(
                _warning(
                    "usdce_balance",
                    "USDC.e",
                    f"目前錢包 USDC.e 餘額 {usdc_e_balance:.2f}；V2 交易使用 pUSD。",
                    required=False,
                    value=round(usdc_e_balance, 4),
                )
            )

        checks.append(await _check_clob_clock(client, settings, current_time))

    if after_v2_cutover:
        async with httpx.AsyncClient(timeout=10.0) as client:
            allowance_results = await asyncio.gather(
                *[
                    fetch_erc20_allowance(
                        client,
                        rpc_url,
                        settings.polygon_pusd_token_address,
                        address,
                        spender_address,
                    )
                    for spender_address in V2_PUSD_SPENDER_ADDRESSES
                ],
                return_exceptions=True,
            )
        allowance_values = [
            format_units(result, TOKEN_DECIMALS)
            for result in allowance_results
            if not isinstance(result, Exception)
        ]
        allowance_errors = [result for result in allowance_results if isinstance(result, Exception)]
        if allowance_errors:
            checks.append(
                _warning(
                    "exchange_allowance",
                    "pUSD Exchange 授權",
                    f"pUSD allowance 讀取失敗：{allowance_errors[0]}",
                    required=False,
                )
            )
        elif allowance_values and min(allowance_values) >= settings.min_exchange_allowance:
            checks.append(
                _ok(
                    "exchange_allowance",
                    "pUSD Exchange 授權",
                    "pUSD 已授權給 CLOB V2 exchange spenders。",
                    value=round(min(allowance_values), 4),
                    threshold=settings.min_exchange_allowance,
                )
            )
        else:
            checks.append(
                _critical(
                    "exchange_allowance",
                    "pUSD Exchange 授權",
                    "pUSD 尚未授權給 CLOB V2 exchange spenders。",
                    value=round(min(allowance_values), 4) if allowance_values else 0.0,
                    threshold=settings.min_exchange_allowance,
                )
            )
        return PreflightReport(current_time, address, funder, collateral_symbol, checks)
    if False:
        checks.append(
            _warning(
                "exchange_allowance",
                "pUSD Exchange 授權",
                "V2 adapter 會在送單前透過 CLOB 檢查 pUSD balance / allowance；目前 preflight 先確認 SDK、Polygon 與 pUSD 餘額。",
                required=False,
            )
        )
        return PreflightReport(current_time, address, funder, collateral_symbol, checks)

    checks.append(
        _warning(
            "exchange_allowance",
            "Legacy Exchange 授權",
            "目前日期仍在 V2 cutover 前的測試時間；legacy allowance 不再是本程式主要維護路徑。",
            required=False,
        )
    )
    checks.append(
        _warning(
            "conditional_allowance",
            "Outcome Token 授權",
            "SELL 所需 conditional token allowance 會在送出 SELL 腿前即時驗證。",
            required=False,
        )
    )
    if verify_clob_credentials:
        checks.append(
            _warning(
                "clob_credentials",
                "CLOB API 憑證",
                "目前程式已改維護 V2 SDK；legacy 憑證不再於 preflight 中建立。",
                required=False,
            )
        )
    else:
        checks.append(
            _warning(
                "clob_credentials",
                "CLOB API 憑證",
                "這次使用快取結果，未重新驗證 CLOB API 憑證。",
                required=False,
            )
        )
    return PreflightReport(current_time, address, funder, collateral_symbol, checks)
