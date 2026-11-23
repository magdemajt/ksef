"""Polaczenie SQLite ze schematem i migracjami (PRAGMA user_version)."""

from __future__ import annotations

import sqlite3
import threading
from importlib import resources
from pathlib import Path

SCHEMA_VERSION = 2  # 2: tabela xml_queue


class SerializedConnection(sqlite3.Connection):
    """Polaczenie wspoldzielone przez watki: blok `with conn:` to jedna transakcja naraz.

    Aplikacja uzywa jednego polaczenia z watkow zadan HTTP, schedulera, kolejki XML i wysylki.
    Transakcja SQLite nalezy do polaczenia, nie do watku - bez blokady commit albo rollback
    jednego watku konczylby niedokonczona transakcje innego (np. blad w zadaniu HTTP cofalby
    zapis pobranej wlasnie faktury).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tx_lock = threading.RLock()

    def __enter__(self):
        self._tx_lock.acquire()
        try:
            return super().__enter__()
        except BaseException:
            self._tx_lock.release()
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self._tx_lock.release()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, factory=SerializedConnection)
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
