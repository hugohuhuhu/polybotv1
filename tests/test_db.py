from pathlib import Path

from app.storage import db as db_module
from app.storage.db import connect_db


def test_sqlite_schema_initializes_once_per_path(tmp_path, monkeypatch) -> None:
    sqlite_path = (tmp_path / "once.db").resolve()
    db_module._initialized_sqlite_paths.discard(sqlite_path)

    original_initialize = db_module._initialize_schema
    calls: list[Path] = []

    def wrapped_initialize(session) -> None:
        calls.append(sqlite_path)
        original_initialize(session)

    monkeypatch.setattr(db_module, "_initialize_schema", wrapped_initialize)

    first = connect_db(sqlite_path)
    second = connect_db(sqlite_path)

    try:
        assert len(calls) == 1
        assert sqlite_path in db_module._initialized_sqlite_paths
    finally:
        first.close()
        second.close()


def test_existing_sqlite_db_skips_reinitialization_after_restart(tmp_path, monkeypatch) -> None:
    sqlite_path = (tmp_path / "existing.db").resolve()
    db_module._initialized_sqlite_paths.discard(sqlite_path)

    seeded = connect_db(sqlite_path)
    seeded.close()
    db_module._initialized_sqlite_paths.discard(sqlite_path)

    original_initialize = db_module._initialize_schema
    calls: list[Path] = []

    def wrapped_initialize(session) -> None:
        calls.append(sqlite_path)
        original_initialize(session)

    monkeypatch.setattr(db_module, "_initialize_schema", wrapped_initialize)

    reopened = connect_db(sqlite_path)
    try:
        assert calls == []
        assert sqlite_path in db_module._initialized_sqlite_paths
    finally:
        reopened.close()
