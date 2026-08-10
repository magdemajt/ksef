"""Fabryka polaczen do MS SQL (baza Optimy) przez mssql-python.

Dwa loginy (zasada najmniejszych uprawnien):
    read  - ksef_reader, tylko SELECT
    write - ksef_writer, SELECT + INSERT na VatNag/VatTab/BnkZdarzenia
"""

from __future__ import annotations

import logging

from app.config import AppConfig, CompanyConfig
from app.secrets import SecretStore

logger = logging.getLogger(__name__)


def _connection_string(
    server: str, database: str, user: str, password: str, trust_cert: bool
) -> str:
    parts = [
        f"Server={server}",
        f"Database={database}",
        f"UID={user}",
        f"PWD={password}",
        "Encrypt=yes",
    ]
    if trust_cert:
        parts.append("TrustServerCertificate=yes")
    return ";".join(parts)


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
        return mssql_python.connect(conn_str)

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
