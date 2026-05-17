from __future__ import annotations

import sqlite3
import time
import warnings
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Iterator, Sequence

from app.config import Settings
from app.storage.path_safety import path_sync_warning

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional dependency for PostgreSQL deployments
    psycopg = None
    dict_row = None


SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    event_id TEXT,
    slug TEXT NOT NULL,
    question TEXT NOT NULL,
    end_date TEXT,
    outcome_labels_json TEXT NOT NULL,
    token_ids_json TEXT NOT NULL,
    category TEXT,
    tags_json TEXT NOT NULL,
    active INTEGER NOT NULL,
    closed INTEGER NOT NULL,
    liquidity REAL,
    volume REAL,
    raw_json TEXT NOT NULL,
    discovered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_id TEXT NOT NULL,
    market_id TEXT,
    captured_minute TEXT NOT NULL DEFAULT '',
    best_bid REAL,
    best_ask REAL,
    midpoint REAL,
    spread REAL,
    bids_json TEXT NOT NULL,
    asks_json TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id TEXT PRIMARY KEY,
    strategy_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    title TEXT NOT NULL,
    market_slugs_json TEXT NOT NULL,
    gross_edge REAL NOT NULL,
    estimated_fees REAL NOT NULL,
    slippage_estimate REAL NOT NULL,
    net_edge REAL NOT NULL,
    max_safe_size REAL NOT NULL,
    available_liquidity REAL NOT NULL,
    confidence_score REAL NOT NULL,
    prices_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL,
    filled INTEGER NOT NULL,
    average_entry_price REAL,
    filled_size REAL NOT NULL,
    gross_notional REAL NOT NULL DEFAULT 0,
    estimated_fees_paid REAL NOT NULL DEFAULT 0,
    expected_pnl REAL,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    executed_at TEXT NOT NULL,
    discovered_market_count INTEGER NOT NULL,
    monitored_market_count INTEGER NOT NULL,
    book_count INTEGER NOT NULL,
    opportunity_count INTEGER NOT NULL,
    actionable_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    summary_date TEXT PRIMARY KEY,
    scan_cycle_count INTEGER NOT NULL DEFAULT 0,
    max_discovered_market_count INTEGER NOT NULL DEFAULT 0,
    max_monitored_market_count INTEGER NOT NULL DEFAULT 0,
    total_book_count INTEGER NOT NULL DEFAULT 0,
    total_opportunity_count INTEGER NOT NULL DEFAULT 0,
    total_actionable_count INTEGER NOT NULL DEFAULT 0,
    total_candidate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    state TEXT NOT NULL,
    latest_scan_at TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL,
    leg_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    outcome_label TEXT NOT NULL,
    target_price REAL NOT NULL,
    requested_size REAL NOT NULL,
    order_id TEXT,
    status TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_controls (
    id INTEGER PRIMARY KEY,
    live_trading_enabled INTEGER NOT NULL,
    auto_execute_enabled INTEGER NOT NULL,
    kill_switch_enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_claims (
    claim_key TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    source TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_key TEXT,
    opportunity_id TEXT,
    source TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    market_id TEXT PRIMARY KEY,
    event_id TEXT,
    slug TEXT NOT NULL,
    question TEXT NOT NULL,
    end_date TEXT,
    outcome_labels_json TEXT NOT NULL,
    token_ids_json TEXT NOT NULL,
    category TEXT,
    tags_json TEXT NOT NULL,
    active BOOLEAN NOT NULL,
    closed BOOLEAN NOT NULL,
    liquidity DOUBLE PRECISION,
    volume DOUBLE PRECISION,
    raw_json TEXT NOT NULL,
    discovered_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    id BIGSERIAL PRIMARY KEY,
    token_id TEXT NOT NULL,
    market_id TEXT,
    captured_minute TEXT NOT NULL DEFAULT '',
    best_bid DOUBLE PRECISION,
    best_ask DOUBLE PRECISION,
    midpoint DOUBLE PRECISION,
    spread DOUBLE PRECISION,
    bids_json TEXT NOT NULL,
    asks_json TEXT NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS opportunities (
    opportunity_id TEXT PRIMARY KEY,
    strategy_type TEXT NOT NULL,
    direction TEXT NOT NULL,
    title TEXT NOT NULL,
    market_slugs_json TEXT NOT NULL,
    gross_edge DOUBLE PRECISION NOT NULL,
    estimated_fees DOUBLE PRECISION NOT NULL,
    slippage_estimate DOUBLE PRECISION NOT NULL,
    net_edge DOUBLE PRECISION NOT NULL,
    max_safe_size DOUBLE PRECISION NOT NULL,
    available_liquidity DOUBLE PRECISION NOT NULL,
    confidence_score DOUBLE PRECISION NOT NULL,
    prices_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id BIGSERIAL PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    filled BOOLEAN NOT NULL,
    average_entry_price DOUBLE PRECISION,
    filled_size DOUBLE PRECISION NOT NULL,
    gross_notional DOUBLE PRECISION NOT NULL DEFAULT 0,
    estimated_fees_paid DOUBLE PRECISION NOT NULL DEFAULT 0,
    expected_pnl DOUBLE PRECISION,
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_cycles (
    id BIGSERIAL PRIMARY KEY,
    executed_at TEXT NOT NULL,
    discovered_market_count INTEGER NOT NULL,
    monitored_market_count INTEGER NOT NULL,
    book_count INTEGER NOT NULL,
    opportunity_count INTEGER NOT NULL,
    actionable_count INTEGER NOT NULL,
    candidate_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_summaries (
    summary_date TEXT PRIMARY KEY,
    scan_cycle_count INTEGER NOT NULL DEFAULT 0,
    max_discovered_market_count INTEGER NOT NULL DEFAULT 0,
    max_monitored_market_count INTEGER NOT NULL DEFAULT 0,
    total_book_count INTEGER NOT NULL DEFAULT 0,
    total_opportunity_count INTEGER NOT NULL DEFAULT 0,
    total_actionable_count INTEGER NOT NULL DEFAULT 0,
    total_candidate_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS maintenance_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watch_heartbeats (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    state TEXT NOT NULL,
    latest_scan_at TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS live_trades (
    id BIGSERIAL PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    leg_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    token_id TEXT NOT NULL,
    market_slug TEXT NOT NULL,
    outcome_label TEXT NOT NULL,
    target_price DOUBLE PRECISION NOT NULL,
    requested_size DOUBLE PRECISION NOT NULL,
    order_id TEXT,
    status TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_controls (
    id SMALLINT PRIMARY KEY,
    live_trading_enabled BOOLEAN NOT NULL,
    auto_execute_enabled BOOLEAN NOT NULL,
    kill_switch_enabled BOOLEAN NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_claims (
    claim_key TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL,
    source TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS execution_audit_log (
    id BIGSERIAL PRIMARY KEY,
    claim_key TEXT,
    opportunity_id TEXT,
    source TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class DatabaseSession:
    def __init__(self, backend: str, connection: Any) -> None:
        self.backend = backend
        self.connection = connection

    def _sql(self, statement: str) -> str:
        if self.backend == "postgresql":
            return statement.replace("?", "%s")
        return statement

    def execute(self, statement: str, params: Sequence[Any] | None = None) -> Any:
        sql = self._sql(statement)
        parameters = tuple(params or ())
        attempts = 6 if self.backend == "sqlite" else 1
        delay_sec = 0.15
        for attempt in range(attempts):
            try:
                return self.connection.execute(sql, parameters)
            except sqlite3.OperationalError as exc:
                if self.backend != "sqlite" or not _is_sqlite_lock_error(exc) or attempt >= attempts - 1:
                    raise
                time.sleep(delay_sec)
                delay_sec = min(delay_sec * 2, 2.0)
        raise RuntimeError("unreachable database execute retry state")

    def fetchone(self, statement: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        cursor = self.execute(statement, params)
        try:
            row = cursor.fetchone()
            return self._normalize_row(row)
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    def fetchall(self, statement: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        cursor = self.execute(statement, params)
        try:
            return [row_dict for row in cursor.fetchall() if (row_dict := self._normalize_row(row)) is not None]
        finally:
            close = getattr(cursor, "close", None)
            if callable(close):
                close()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self.backend == "postgresql":
            with self.connection.transaction():
                yield
            return
        with _sqlite_write_lock:
            with self.connection:
                yield

    @staticmethod
    def _normalize_row(row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        if isinstance(row, dict):
            return row
        if isinstance(row, sqlite3.Row):
            return dict(row)
        try:
            return dict(row)
        except Exception:
            return None


def _table_columns(session: DatabaseSession, table_name: str) -> set[str]:
    if session.backend == "postgresql":
        rows = session.fetchall(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table_name,),
        )
        return {str(row["column_name"]) for row in rows}
    rows = session.fetchall(f"PRAGMA table_info({table_name})")
    return {str(row["name"]) for row in rows}


def _ensure_column(
    session: DatabaseSession,
    *,
    table_name: str,
    column_name: str,
    definition: str,
) -> None:
    if column_name in _table_columns(session, table_name):
        return
    with session.transaction():
        session.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def _ensure_indexes(session: DatabaseSession) -> None:
    statements = [
        "CREATE INDEX IF NOT EXISTS idx_scan_cycles_executed_at ON scan_cycles (executed_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_opportunities_created_at ON opportunities (created_at DESC, net_edge DESC)",
        "CREATE INDEX IF NOT EXISTS idx_alerts_sent_at ON alerts (sent_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_execution_audit_created_at ON execution_audit_log (created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_live_trades_created_at ON live_trades (created_at DESC, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_paper_trades_created_at ON paper_trades (created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_markets_dashboard ON markets (active, closed, liquidity DESC, discovered_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_watch_heartbeats_created_at ON watch_heartbeats (created_at DESC, id DESC)",
    ]
    with session.transaction():
        for statement in statements:
            session.execute(statement)


def _backfill_orderbook_minutes(session: DatabaseSession) -> None:
    if "captured_minute" not in _table_columns(session, "orderbook_snapshots"):
        return
    with session.transaction():
        session.execute(
            """
            UPDATE orderbook_snapshots
            SET captured_minute = substr(captured_at, 1, 16)
            WHERE captured_minute = '' OR captured_minute IS NULL
            """
        )


def _dedupe_orderbook_snapshots(session: DatabaseSession) -> None:
    if session.backend != "sqlite":
        return
    with session.transaction():
        session.execute(
            """
            DELETE FROM orderbook_snapshots
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM orderbook_snapshots
                GROUP BY token_id, captured_minute
            )
            """
        )


def _ensure_unique_indexes(session: DatabaseSession) -> None:
    statements = [
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_orderbook_token_minute
        ON orderbook_snapshots (token_id, captured_minute)
        """,
    ]
    with session.transaction():
        for statement in statements:
            session.execute(statement)


def _initialize_schema(session: DatabaseSession) -> None:
    schema = POSTGRES_SCHEMA if session.backend == "postgresql" else SQLITE_SCHEMA
    statements = [statement.strip() for statement in schema.split(";") if statement.strip()]
    with session.transaction():
        for statement in statements:
            session.execute(statement)
    _ensure_column(session, table_name="paper_trades", column_name="gross_notional", definition="DOUBLE PRECISION NOT NULL DEFAULT 0")
    _ensure_column(
        session,
        table_name="paper_trades",
        column_name="estimated_fees_paid",
        definition="DOUBLE PRECISION NOT NULL DEFAULT 0",
    )
    _ensure_column(
        session,
        table_name="scan_cycles",
        column_name="watch_bucket_counts_json",
        definition="TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        session,
        table_name="scan_cycles",
        column_name="shortlist_reason_counts_json",
        definition="TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        session,
        table_name="scan_cycles",
        column_name="shortlist_markets_json",
        definition="TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        session,
        table_name="scan_cycles",
        column_name="excluded_long_tail_count",
        definition="INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        session,
        table_name="scan_cycles",
        column_name="excluded_family_cap_count",
        definition="INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        session,
        table_name="scan_cycles",
        column_name="positive_edge_candidates_24h",
        definition="INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        session,
        table_name="scan_cycles",
        column_name="near_close_funnel_json",
        definition="TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_column(
        session,
        table_name="orderbook_snapshots",
        column_name="captured_minute",
        definition="TEXT NOT NULL DEFAULT ''",
    )
    _backfill_orderbook_minutes(session)
    _dedupe_orderbook_snapshots(session)
    _ensure_indexes(session)
    _ensure_unique_indexes(session)


_sqlite_init_lock = Lock()
_sqlite_write_lock = RLock()
_initialized_sqlite_paths: set[Path] = set()
_required_sqlite_tables = {
    "markets",
    "orderbook_snapshots",
    "opportunities",
    "alerts",
    "paper_trades",
    "scan_cycles",
    "daily_summaries",
    "live_trades",
    "runtime_controls",
    "execution_claims",
    "execution_audit_log",
    "maintenance_state",
    "watch_heartbeats",
}
_required_scan_cycle_columns = {
    "executed_at",
    "discovered_market_count",
    "monitored_market_count",
    "book_count",
    "opportunity_count",
    "actionable_count",
    "candidate_count",
    "watch_bucket_counts_json",
    "shortlist_reason_counts_json",
    "shortlist_markets_json",
    "excluded_long_tail_count",
    "excluded_family_cap_count",
    "positive_edge_candidates_24h",
    "near_close_funnel_json",
}


def _configure_sqlite_connection(connection: sqlite3.Connection) -> None:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")


def _is_sqlite_lock_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "database is locked" in message or "database table is locked" in message


def _is_sqlite_corruption_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "database disk image is malformed",
            "file is not a database",
            "malformed",
        )
    )


def _quarantine_sqlite_database(sqlite_path: Path) -> Path | None:
    if not sqlite_path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    quarantined = sqlite_path.with_name(f"{sqlite_path.stem}.corrupt-{timestamp}{sqlite_path.suffix}")
    counter = 1
    while quarantined.exists():
        quarantined = sqlite_path.with_name(
            f"{sqlite_path.stem}.corrupt-{timestamp}-{counter}{sqlite_path.suffix}"
        )
        counter += 1

    sqlite_path.replace(quarantined)
    for suffix in ("-wal", "-shm"):
        sidecar = sqlite_path.with_name(f"{sqlite_path.name}{suffix}")
        if sidecar.exists():
            sidecar.replace(quarantined.with_name(f"{quarantined.name}{suffix}"))
    _initialized_sqlite_paths.discard(sqlite_path.resolve())
    return quarantined


def _sqlite_schema_ready(session: DatabaseSession) -> bool:
    rows = session.fetchall(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    )
    existing_tables = {str(row["name"]) for row in rows}
    if not _required_sqlite_tables.issubset(existing_tables):
        return False
    paper_trade_columns = _table_columns(session, "paper_trades")
    scan_cycle_columns = _table_columns(session, "scan_cycles")
    return (
        {"gross_notional", "estimated_fees_paid"}.issubset(paper_trade_columns)
        and _required_scan_cycle_columns.issubset(scan_cycle_columns)
    )


def _initialize_sqlite_database(session: DatabaseSession, sqlite_path: Path) -> None:
    with _sqlite_init_lock:
        if sqlite_path in _initialized_sqlite_paths:
            return
        if sqlite_path.exists() and _sqlite_schema_ready(session):
            _initialized_sqlite_paths.add(sqlite_path)
            return
        # Keep the file in WAL mode so the watch loop can write while the
        # dashboard keeps serving read requests from separate connections.
        session.execute("PRAGMA journal_mode = WAL")
        session.execute("PRAGMA synchronous = NORMAL")
        _initialize_schema(session)
        _initialized_sqlite_paths.add(sqlite_path)


def connect_db(target: Settings | Path) -> DatabaseSession:
    if isinstance(target, Settings):
        if target.database_url:
            if psycopg is None or dict_row is None:
                raise RuntimeError("DATABASE_URL is configured but psycopg is not installed.")
            connection = psycopg.connect(target.database_url, row_factory=dict_row)
            session = DatabaseSession("postgresql", connection)
            _initialize_schema(session)
            return session
        sqlite_path = target.sqlite_path
    else:
        sqlite_path = target

    sqlite_path = Path(sqlite_path).expanduser()
    warning = path_sync_warning(sqlite_path)
    if warning:
        warnings.warn(warning, RuntimeWarning, stacklevel=2)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(sqlite_path, timeout=30.0, check_same_thread=False)
        _configure_sqlite_connection(connection)
        session = DatabaseSession("sqlite", connection)
        _initialize_sqlite_database(session, sqlite_path.resolve())
        return session
    except sqlite3.DatabaseError as exc:
        try:
            connection.close()
        except Exception:
            pass
        if not _is_sqlite_corruption_error(exc):
            raise
        _quarantine_sqlite_database(sqlite_path.resolve())
        connection = sqlite3.connect(sqlite_path, timeout=30.0, check_same_thread=False)
        _configure_sqlite_connection(connection)
        session = DatabaseSession("sqlite", connection)
        _initialize_sqlite_database(session, sqlite_path.resolve())
        return session
