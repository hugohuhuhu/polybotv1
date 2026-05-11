from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import Settings


def backup_sqlite_database(settings: Settings, *, label: str = "scheduled") -> dict[str, Any]:
    if settings.persistence_backend != "sqlite":
        return {"status": "skipped", "reason": "non_sqlite_backend"}

    source = Path(settings.sqlite_path).expanduser()
    if not source.exists():
        return {"status": "skipped", "reason": "missing_source", "source": str(source)}

    backup_dir = Path(settings.sqlite_backup_dir).expanduser()
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"polymarket_scanner.{label}.{timestamp}.db"
    latest_path = backup_dir / "polymarket_scanner.latest.db"

    with sqlite3.connect(source) as src, sqlite3.connect(backup_path) as dest:
        src.backup(dest)
    shutil.copy2(backup_path, latest_path)

    return {
        "status": "backed_up",
        "source": str(source),
        "backup_path": str(backup_path),
        "latest_path": str(latest_path),
    }
