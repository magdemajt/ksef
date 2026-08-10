"""Polaczenie SQLite ze schematem i migracjami (PRAGMA user_version)."""

from __future__ import annotations

import sqlite3
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 1


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= SCHEMA_VERSION:
        return
    schema = resources.files("app.state").joinpath("schema.sql").read_text(encoding="utf-8")
    with conn:
        conn.executescript(schema)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
