from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import pythoncom
import win32com.client

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.config import Settings
from app.storage.db import connect_db
from app.storage.repositories import ScannerRepository


DEFAULT_OUTPUT_NAME = "交易機器人_驗屍資料.xlsx"
DEFAULT_PID_FILE = REPO_ROOT / "runtime-logs" / "autopsy-excel-updater.pid"

CANDIDATE_COLUMNS = [
    "candidate_autopsy_id",
    "opportunity_id",
    "observed_at",
    "market_slug",
    "market_title",
    "selected_asset",
    "outcome",
    "crypto_winning_outcome",
    "final_outcome",
    "did_bought_outcome_win",
    "candidate_quality",
    "hypothetical_hold_pnl",
    "fillability",
    "fillability_weight",
    "fillability_weighted_hold_pnl",
    "time_to_resolution_sec",
    "entry_price",
    "size",
    "notional",
    "best_bid",
    "best_ask",
    "midpoint",
    "spread",
    "bid_depth_at_best",
    "ask_depth_at_best",
    "crypto_spot_price",
    "crypto_start_price",
    "crypto_start_distance",
    "crypto_start_distance_pct",
    "crypto_start_distance_required",
    "start_distance_ratio",
    "entry_price_bucket",
    "spread_bucket",
    "start_distance_bucket",
    "resolution_bucket_key",
    "passed_gate",
    "passed_gate_label",
    "tradable_live",
    "post_only",
    "order_type",
    "execution_status",
    "execution_at",
    "observation_to_execution_sec",
    "execution_message",
    "settlement_source",
    "fillability_evidence",
    "token_id",
    "raw_details_json",
]

UNIQUE_MARKET_COLUMNS = [
    "market_slug",
    "market_title",
    "selected_asset",
    "outcome",
    "final_outcome",
    "did_bought_outcome_win",
    "candidate_quality",
    "hypothetical_hold_pnl",
    "fillability",
    "fillability_weight",
    "fillability_weighted_hold_pnl",
    "latest_observed_at",
    "observation_count",
    "time_to_resolution_sec",
    "entry_price",
    "best_bid",
    "best_ask",
    "midpoint",
    "spread",
    "bid_depth_at_best",
    "ask_depth_at_best",
    "crypto_start_distance",
    "crypto_start_distance_pct",
    "crypto_start_distance_required",
    "start_distance_ratio",
    "execution_status",
    "observation_to_execution_sec",
]


def default_output_path() -> Path:
    desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    return desktop / DEFAULT_OUTPUT_NAME


def default_db_path() -> Path:
    configured = os.environ.get("SQLITE_PATH")
    if configured:
        return Path(configured)
    return Path(os.environ["LOCALAPPDATA"]) / "PolymarketScanner" / "polymarket_scanner.db"


def json_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def execution_details(repository: ScannerRepository) -> dict[str, dict[str, Any]]:
    rows = repository.connection.fetchall(
        """
        SELECT opportunity_id, status, message, created_at
        FROM execution_audit_log
        WHERE source IN ('watch', 'dashboard')
          AND status IN ('submitted', 'failed', 'partial_failure', 'risk_blocked')
          AND opportunity_id IS NOT NULL
        ORDER BY created_at ASC, id ASC
        """
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        opportunity_id = str(row.get("opportunity_id") or "")
        if opportunity_id and opportunity_id not in result:
            result[opportunity_id] = dict(row)
    return result


def load_workbook_data(db_path: Path) -> dict[str, Any]:
    settings = Settings(_env_file=None, SQLITE_PATH=db_path)
    with closing(connect_db(settings)) as connection:
        repository = ScannerRepository(connection)
        candidate_events = repository.candidate_autopsy_events(limit=1_000_000)
        candidate_report = repository.candidate_autopsy_report(limit=1_000_000)
        cancel_report = repository.cancel_autopsy_report(limit=1_000_000)
        trade_report = repository.trade_autopsy_report(limit=1_000_000)
        executions = execution_details(repository)

    raw_by_id = {
        str(event.get("candidate_autopsy_id") or ""): event.get("details", {})
        for event in candidate_events
    }
    candidate_rows: list[dict[str, Any]] = []
    for row in candidate_report.get("rows", []):
        merged = dict(row)
        raw = raw_by_id.get(str(row.get("candidate_autopsy_id") or ""), {})
        merged.update(
            {
                "crypto_winning_outcome": raw.get("crypto_winning_outcome"),
                "crypto_start_distance_required": raw.get("crypto_start_distance_required"),
                "entry_price_bucket": raw.get("entry_price_bucket"),
                "spread_bucket": raw.get("spread_bucket"),
                "start_distance_bucket": raw.get("start_distance_bucket"),
                "post_only": raw.get("post_only"),
                "order_type": raw.get("order_type"),
                "raw_details_json": raw,
            }
        )
        distance = merged.get("crypto_start_distance")
        required = merged.get("crypto_start_distance_required")
        try:
            merged["crypto_start_distance_pct"] = float(distance) * 100.0
        except (TypeError, ValueError):
            merged["crypto_start_distance_pct"] = None
        try:
            merged["start_distance_ratio"] = float(distance) / float(required) if float(required) else None
        except (TypeError, ValueError):
            merged["start_distance_ratio"] = None

        execution = executions.get(str(merged.get("opportunity_id") or ""), {})
        observed_at = parse_iso(merged.get("observed_at"))
        executed_at = parse_iso(execution.get("created_at"))
        merged["execution_status"] = execution.get("status")
        merged["execution_at"] = execution.get("created_at")
        merged["execution_message"] = execution.get("message")
        merged["observation_to_execution_sec"] = (
            (executed_at - observed_at).total_seconds()
            if observed_at is not None and executed_at is not None
            else None
        )
        candidate_rows.append(merged)

    latest_by_market: dict[str, dict[str, Any]] = {}
    observation_counts: dict[str, int] = {}
    for row in sorted(candidate_rows, key=lambda item: str(item.get("observed_at") or "")):
        slug = str(row.get("market_slug") or "")
        if not slug:
            continue
        observation_counts[slug] = observation_counts.get(slug, 0) + 1
        latest_by_market[slug] = row
    unique_rows: list[dict[str, Any]] = []
    for slug, row in latest_by_market.items():
        unique = dict(row)
        unique["latest_observed_at"] = row.get("observed_at")
        unique["observation_count"] = observation_counts.get(slug, 0)
        unique_rows.append(unique)
    unique_rows.sort(key=lambda item: str(item.get("latest_observed_at") or ""), reverse=True)

    summary = dict(candidate_report.get("summary", {}))
    settled_unique = [row for row in unique_rows if row.get("did_bought_outcome_win") is not None]
    unique_wins = sum(row.get("did_bought_outcome_win") is True for row in settled_unique)
    unique_losses = sum(row.get("did_bought_outcome_win") is False for row in settled_unique)
    summary.update(
        {
            "unique_market_count": len(unique_rows),
            "unique_settled_count": len(settled_unique),
            "unique_win_count": unique_wins,
            "unique_loss_count": unique_losses,
            "unique_hit_rate": unique_wins / len(settled_unique) if settled_unique else None,
        }
    )
    return {
        "summary": summary,
        "candidate_rows": candidate_rows,
        "unique_rows": unique_rows,
        "cancel_rows": cancel_report.get("rows", []),
        "cancel_by_reason": cancel_report.get("by_reason", []),
        "trade_rows": trade_report,
    }


def database_signature(db_path: Path) -> str:
    settings = Settings(_env_file=None, SQLITE_PATH=db_path)
    with closing(connect_db(settings)) as connection:
        parts = [
            dict(
                connection.fetchone(
                """
                SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS max_id
                FROM execution_audit_log
                WHERE source IN ('candidate-autopsy', 'cancel-autopsy', 'trade-autopsy')
                   OR status IN ('candidate_autopsy_observation', 'cancel_autopsy')
                """
                )
            ),
            dict(
                connection.fetchone(
                """
                SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS max_id,
                       COALESCE(SUM(LENGTH(status) + LENGTH(response_json)), 0) AS content_size
                FROM live_trades
                """
                )
            ),
            [
                dict(row)
                for row in connection.fetchall(
                """
                SELECT slug, active, closed, raw_json
                FROM markets
                WHERE slug IN (
                    SELECT DISTINCT json_extract(details_json, '$.market_slug')
                    FROM execution_audit_log
                    WHERE source IN ('candidate-autopsy', 'cancel-autopsy', 'trade-autopsy')
                       OR status IN ('candidate_autopsy_observation', 'cancel_autopsy')
                )
                ORDER BY slug
                """
                )
            ],
        ]
    payload = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ordered_columns(rows: list[dict[str, Any]], preferred: list[str] | None = None) -> list[str]:
    preferred = preferred or []
    present = {key for row in rows for key in row}
    columns = [key for key in preferred if key in present]
    columns.extend(sorted(present - set(columns)))
    return columns


def cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list, tuple)):
        return json_text(value)
    return value


def rgb(red: int, green: int, blue: int) -> int:
    return red + (green << 8) + (blue << 16)


def write_table(
    workbook: Any,
    sheet_name: str,
    rows: list[dict[str, Any]],
    *,
    preferred_columns: list[str] | None = None,
) -> Any:
    sheet = workbook.Worksheets.Add()
    sheet.Name = sheet_name[:31]
    columns = ordered_columns(rows, preferred_columns)
    if not columns:
        sheet.Cells(1, 1).Value = "目前沒有資料"
        return sheet

    matrix = [tuple(columns)]
    matrix.extend(tuple(cell_value(row.get(column)) for column in columns) for row in rows)
    end_row = len(matrix)
    end_col = len(columns)
    target = sheet.Range(sheet.Cells(1, 1), sheet.Cells(end_row, end_col))
    target.Value = tuple(matrix)
    target.VerticalAlignment = -4160
    target.WrapText = False

    header = sheet.Range(sheet.Cells(1, 1), sheet.Cells(1, end_col))
    header.Font.Bold = True
    header.Font.Color = rgb(255, 255, 255)
    header.Interior.Color = rgb(31, 78, 121)
    header.HorizontalAlignment = -4108
    header.AutoFilter()

    sheet.Activate()
    workbook.Application.ActiveWindow.SplitRow = 1
    workbook.Application.ActiveWindow.FreezePanes = True

    for index, column in enumerate(columns, start=1):
        values = [str(row.get(column) or "") for row in rows[:500]]
        width = min(max([len(column), *(len(value) for value in values)]), 42) + 2
        sheet.Columns(index).ColumnWidth = max(width, 10)
        if column in {
            "entry_price",
            "best_bid",
            "best_ask",
            "midpoint",
            "spread",
            "crypto_start_distance",
            "crypto_start_distance_required",
            "start_distance_ratio",
            "hypothetical_hold_pnl",
            "fillability_weighted_hold_pnl",
        }:
            sheet.Columns(index).NumberFormat = "0.000000"
        elif column == "crypto_start_distance_pct":
            sheet.Columns(index).NumberFormat = "0.00000"
        elif column in {"fillability_weight", "unique_hit_rate"}:
            sheet.Columns(index).NumberFormat = "0.00%"
        elif column in {
            "time_to_resolution_sec",
            "observation_to_execution_sec",
            "size",
            "notional",
            "bid_depth_at_best",
            "ask_depth_at_best",
        }:
            sheet.Columns(index).NumberFormat = "0.00"

    quality_index = columns.index("candidate_quality") + 1 if "candidate_quality" in columns else None
    fillability_index = columns.index("fillability") + 1 if "fillability" in columns else None
    for row_index, row in enumerate(rows, start=2):
        if quality_index is not None:
            quality = str(row.get("candidate_quality") or "")
            color = {
                "would_profit": rgb(226, 239, 218),
                "would_loss": rgb(244, 204, 204),
                "pending_settlement": rgb(217, 217, 217),
            }.get(quality)
            if color is not None:
                sheet.Cells(row_index, quality_index).Interior.Color = color
        if fillability_index is not None:
            fillability = str(row.get("fillability") or "unknown")
            color = {
                "likely_fill": rgb(198, 224, 180),
                "touch_possible": rgb(226, 239, 218),
                "would_cross_post_only": rgb(255, 230, 153),
                "unfillable": rgb(244, 176, 132),
                "unknown": rgb(217, 217, 217),
            }.get(fillability, rgb(217, 217, 217))
            sheet.Cells(row_index, fillability_index).Interior.Color = color
    return sheet


def write_summary(workbook: Any, data: dict[str, Any], output_path: Path) -> Any:
    sheet = workbook.Worksheets(1)
    sheet.Name = "摘要"
    summary = data["summary"]
    rows = [
        ("更新時間", datetime.now().astimezone().isoformat(timespec="seconds")),
        ("Excel 路徑", str(output_path)),
        ("Gate 8 觀察筆數", summary.get("count")),
        ("獨立市場數", summary.get("unique_market_count")),
        ("已結算獨立市場", summary.get("unique_settled_count")),
        ("獨立市場勝", summary.get("unique_win_count")),
        ("獨立市場負", summary.get("unique_loss_count")),
        ("獨立市場命中率", summary.get("unique_hit_rate")),
        ("觀察筆數假設 PnL", summary.get("hypothetical_hold_pnl_total")),
        ("成交可能性加權 PnL", summary.get("fillability_weighted_hold_pnl_total")),
        ("Likely fill 筆數", summary.get("likely_fill_count")),
        ("等待結算／未知成交", summary.get("unknown_count")),
    ]
    sheet.Range("A1:B1").Value = (("指標", "數值"),)
    sheet.Range("A2:B13").Value = tuple(rows)
    sheet.Range("A1:B1").Font.Bold = True
    sheet.Range("A1:B1").Font.Color = rgb(255, 255, 255)
    sheet.Range("A1:B1").Interior.Color = rgb(31, 78, 121)
    sheet.Columns("A").ColumnWidth = 28
    sheet.Columns("B").ColumnWidth = 60
    sheet.Range("B9").NumberFormat = "0.00%"

    notes = [
        ("說明", "內容"),
        ("Gate 8", "已通過 entry price、流動性與 post-only 核心條件的觀察。"),
        ("假設 PnL", "假設以觀察價買入並持有至最終結算；不是實際已成交損益。"),
        ("成交可能性", "likely_fill 90%、touch_possible 70%、unfillable 5%、unknown 無權重。"),
        ("自動更新", "背景程序每 30 秒檢查資料；只有資料變動才重建 Excel。"),
        ("Excel 開啟時", "若檔案被 Excel 鎖住，該輪更新會跳過；關閉後下一輪自動補上。"),
        ("資料來源", "本機 PolymarketScanner SQLite，未修改交易資料。"),
    ]
    sheet.Range("D1:E7").Value = tuple(notes)
    sheet.Range("D1:E1").Font.Bold = True
    sheet.Range("D1:E1").Font.Color = rgb(255, 255, 255)
    sheet.Range("D1:E1").Interior.Color = rgb(31, 78, 121)
    sheet.Columns("D").ColumnWidth = 20
    sheet.Columns("E").ColumnWidth = 70
    sheet.Range("D1:E7").WrapText = True
    return sheet


def export_excel(db_path: Path, output_path: Path) -> None:
    data = load_workbook_data(db_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.stem}.updating.xlsx")
    if temp_path.exists():
        temp_path.unlink()

    pythoncom.CoInitialize()
    excel = None
    workbook = None
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
        excel.Visible = False
        excel.DisplayAlerts = False
        excel.ScreenUpdating = False
        workbook = excel.Workbooks.Add()
        while workbook.Worksheets.Count > 1:
            workbook.Worksheets(workbook.Worksheets.Count).Delete()
        write_summary(workbook, data, output_path)
        write_table(
            workbook,
            "獨立市場摘要",
            data["unique_rows"],
            preferred_columns=UNIQUE_MARKET_COLUMNS,
        )
        write_table(
            workbook,
            "Gate8全部觀察",
            data["candidate_rows"],
            preferred_columns=CANDIDATE_COLUMNS,
        )
        write_table(workbook, "取消驗屍", data["cancel_rows"])
        write_table(workbook, "取消原因統計", data["cancel_by_reason"])
        write_table(workbook, "實際交易驗屍", data["trade_rows"])
        workbook.Worksheets("摘要").Move(Before=workbook.Worksheets(1))
        workbook.Worksheets("摘要").Activate()
        workbook.SaveAs(str(temp_path), FileFormat=51)
        workbook.Close(SaveChanges=False)
        workbook = None
        excel.Quit()
        excel = None
        os.replace(temp_path, output_path)
    finally:
        if workbook is not None:
            try:
                workbook.Close(SaveChanges=False)
            except Exception:
                pass
        if excel is not None:
            try:
                excel.Quit()
            except Exception:
                pass
        pythoncom.CoUninitialize()
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    process_query_limited_information = 0x1000
    still_active = 259
    handle = ctypes.windll.kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == still_active
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def claim_pid_file(pid_file: Path) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    if pid_file.exists():
        try:
            existing = int(pid_file.read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            existing = 0
        if process_is_running(existing):
            raise RuntimeError(f"Excel updater is already running with PID {existing}.")
    pid_file.write_text(str(os.getpid()), encoding="ascii")


def release_pid_file(pid_file: Path) -> None:
    try:
        if pid_file.exists() and pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
            pid_file.unlink()
    except OSError:
        pass


def run_watch(db_path: Path, output_path: Path, interval: float, pid_file: Path) -> None:
    claim_pid_file(pid_file)
    last_signature = ""
    try:
        while True:
            try:
                signature = database_signature(db_path)
                if signature != last_signature or not output_path.exists():
                    export_excel(db_path, output_path)
                    last_signature = signature
                    print(
                        f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                        f"updated {output_path}",
                        flush=True,
                    )
            except PermissionError:
                print(
                    f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                    "Excel file is open; update deferred.",
                    flush=True,
                )
            except Exception as exc:
                print(
                    f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] "
                    f"update failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            time.sleep(max(interval, 5.0))
    finally:
        release_pid_file(pid_file)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export trade autopsy research data to a desktop Excel workbook.")
    parser.add_argument("--db", type=Path, default=default_db_path())
    parser.add_argument("--output", type=Path, default=default_output_path())
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--pid-file", type=Path, default=DEFAULT_PID_FILE)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.watch:
        run_watch(args.db, args.output, args.interval, args.pid_file)
        return
    export_excel(args.db, args.output)
    print(args.output)


if __name__ == "__main__":
    main()
