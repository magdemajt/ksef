"""Fabryka polaczen do MS SQL (baza Optimy) przez mssql-python.

Dwa loginy (zasada najmniejszych uprawnien):
    read  - ksef_reader, tylko SELECT
    write - ksef_writer, SELECT + INSERT na VatNag/VatTab/BnkZdarzenia
"""

from __future__ import annotations

import contextlib
import logging
import time

from app.config import AppConfig, CompanyConfig
from app.secrets import SecretStore

logger = logging.getLogger(__name__)


def _braced(value: str) -> str:
    """Wartosc w klamrach ODBC: srednik lub klamra w hasle nie rozbija lancucha polaczenia."""
    return "{" + value.replace("}", "}}") + "}"


def _connection_string(
    server: str, database: str, user: str, password: str, trust_cert: bool
) -> str:
    parts = [
        f"Server={server}",
        f"Database={database}",
        f"UID={_braced(user)}",
        f"PWD={_braced(password)}",
        "Encrypt=yes",
    ]
    if trust_cert:
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts)


def fetch_all(conn, sql: str, params: tuple = ()) -> tuple[list[str], list]:
    """Wykonuje zapytanie, czyta CALY wynik i zamyka kursor. Zwraca (kolumny, wiersze).

    Polaczenie z SQL Server obsluguje jeden otwarty wynik naraz. Kursor zostawiony z
    nieprzeczytanymi wierszami (np. fetchone() przy kilku pasujacych kartach kontrahenta)
    blokuje kazde nastepne zapytanie: "Connection is busy with results for another command".
    """
    cur = conn.cursor()
    try:
        cur.execute(sql, params)
        cols = [d[0] for d in cur.description or []]
        return cols, list(cur.fetchall())
    finally:
        with contextlib.suppress(Exception):
            cur.close()


class OptimaDb:
    def __init__(self, cfg: AppConfig, secrets: SecretStore):
        self.cfg = cfg
        self.secrets = secrets

    def _connect(self, company: CompanyConfig, user: str, password_key: str):
        import mssql_python

        password = self.secrets.get(password_key)
        conn_str = _connection_string(
            self.cfg.optima.server,
            company.database,
            user,
            password,
            self.cfg.optima.trust_server_certificate,
        )
        start = time.perf_counter()
        conn = mssql_python.connect(conn_str)
        elapsed = time.perf_counter() - start
        if elapsed >= 1.0:
            logger.warning(
                "Wolne laczenie z baza Optimy %s: %.1f s", company.database, elapsed
            )
        return conn

    def read_connection(self, company: CompanyConfig):
        return self._connect(company, self.cfg.optima.read_user, "sql-read-password")

    def master_connection(self):
        """Polaczenie do bazy master (wykrywanie baz Optimy na serwerze)."""
        import mssql_python

        password = self.secrets.get("sql-read-password")
        conn_str = _connection_string(
            self.cfg.optima.server,
            "master",
            self.cfg.optima.read_user,
            password,
            self.cfg.optima.trust_server_certificate,
        )
        return mssql_python.connect(conn_str)

    def write_connection(self, company: CompanyConfig):
        conn = self._connect(company, self.cfg.optima.write_user, "sql-write-password")
        conn.autocommit = False
        return conn
