from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings


_BACKUP_DATE_PATTERN = re.compile(r"^polymarket_scanner\..*?(20\d{6})(?:-\d{6})?\.db$")
_ACTIVE_SQLITE_NAMES = {"polymarket_scanner.db", "polymarket_scanner.db-wal", "polymarket_scanner.db-shm"}


def _backup_date(path: Path) -> str | None:
    match = _BACKUP_DATE_PATTERN.match(path.name)
    return match.group(1) if match else None


def consolidate_sqlite_backups(backup_dir: Path) -> dict[str, Any]:
    backup_dir = Path(backup_dir).expanduser()
    if not backup_dir.exists():
        return {"status": "skipped", "reason": "missing_backup_dir", "deleted": []}

    grouped: dict[str, list[Path]] = {}
    deleted: list[str] = []
    kept: list[str] = []
    for path in backup_dir.iterdir():
        if not path.is_file() or path.name in _ACTIVE_SQLITE_NAMES:
            continue
        if path.name == "polymarket_scanner.latest.db":
            path.unlink()
            deleted.append(str(path))
            continue
        backup_date = _backup_date(path)
        if backup_date:
            grouped.setdefault(backup_date, []).append(path)

    for backup_date, paths in grouped.items():
        canonical = backup_dir / f"polymarket_scanner.{backup_date}.db"
        keeper = max(paths, key=lambda item: (item.stat().st_mtime_ns, item.name))
        if keeper != canonical:
            if canonical.exists():
                canonical.unlink()
                deleted.append(str(canonical))
            keeper.replace(canonical)
        kept.append(str(canonical))
        for path in paths:
            if path.exists() and path != canonical:
                path.unlink()
                deleted.append(str(path))

    return {"status": "completed", "kept": kept, "deleted": deleted}


def backup_sqlite_database(settings: Settings, *, label: str = "scheduled") -> dict[str, Any]:
    if settings.persistence_backend != "sqlite":
        return {"status": "skipped", "reason": "non_sqlite_backend"}

    source = Path(settings.sqlite_path).expanduser()
    if not source.exists():
        return {"status": "skipped", "reason": "missing_source", "source": str(source)}

    backup_dir = Path(settings.sqlite_backup_dir).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_date = datetime.now().strftime("%Y%m%d")
    backup_path = backup_dir / f"polymarket_scanner.{backup_date}.db"
    temp_path = backup_dir / f".{backup_path.name}.tmp"
    if temp_path.exists():
        temp_path.unlink()

    with sqlite3.connect(source) as src, sqlite3.connect(temp_path) as dest:
        src.backup(dest)
    temp_path.replace(backup_path)
    cleanup = consolidate_sqlite_backups(backup_dir)

    return {
        "status": "backed_up",
        "source": str(source),
        "backup_path": str(backup_path),
        "backup_date": backup_date,
        "label": label,
        "cleanup": cleanup,
    }
