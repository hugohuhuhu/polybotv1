from __future__ import annotations

import os
from pathlib import Path


SYNC_FOLDER_MARKERS = (
    "google drive",
    "google drive (not synced)",
    "my drive",
    "我的雲端硬碟",
    "onedrive",
    "dropbox",
)


def default_local_sqlite_path() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "PolymarketScanner" / "polymarket_scanner.db"
    return Path.home() / ".local" / "share" / "polymarket-scanner" / "polymarket_scanner.db"


def default_backup_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data"


def path_sync_warning(path: Path) -> str | None:
    normalized = str(Path(path).expanduser().resolve()).lower()
    if any(marker in normalized for marker in SYNC_FOLDER_MARKERS):
        return (
            "SQLite live DB is inside a cloud sync folder. "
            "Move SQLITE_PATH to a local non-synced directory and use SQLITE_BACKUP_DIR for Drive backups."
        )
    return None
