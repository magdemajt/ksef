"""Repozytoria nad SQLite. Wszystkie operacje sa per company_id + environment."""

from __future__ import annotations

import json
import secrets as pysecrets
import sqlite3
from datetime import UTC, datetime, timedelta


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SentRepo:
    """Stan wysylki faktur sprzedazy."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, company_id: str, trn_id: int, environment: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM sent_invoices WHERE company_id=? AND trn_id=? AND environment=?",
            (company_id, trn_id, environment),
        ).fetchone()

    def statuses_for(self, company_id: str, environment: str) -> dict[int, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM sent_invoices WHERE company_id=? AND environment=?",
            (company_id, environment),
        ).fetchall()
        return {r["trn_id"]: r for r in rows}

    def upsert(self, company_id: str, trn_id: int, environment: str, **fields) -> int:
        row = self.get(company_id, trn_id, environment)
        if row is None:
            cols = ["company_id", "trn_id", "environment", *fields.keys()]
            vals = [company_id, trn_id, environment, *fields.values()]
            placeholders = ",".join("?" * len(cols))
            with self.conn:
                cur = self.conn.execute(
                    f"INSERT INTO sent_invoices ({','.join(cols)}) VALUES ({placeholders})",
                    vals,
                )
            return cur.lastrowid
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.conn:
            self.conn.execute(
                f"UPDATE sent_invoices SET {sets}, updated_at=? WHERE id=?",
                [*fields.values(), _now(), row["id"]],
            )
        return row["id"]

    def accepted_numbers(self, company_id: str, environment: str) -> set[str]:
        rows = self.conn.execute(
            "SELECT numer_pelny FROM sent_invoices "
            "WHERE company_id=? AND environment=? AND status IN ('accepted','duplicate')",
            (company_id, environment),
        ).fetchall()
        return {r["numer_pelny"] for r in rows}


class BatchRepo:
    """Partie oczekujace na potwierdzenie (token jednorazowy z TTL)."""

    TTL_MINUTES = 10

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def create(self, company_id: str, environment: str, trn_ids: list[int], totals: dict) -> str:
        token = pysecrets.token_urlsafe(24)
        expires = (datetime.now(UTC) + timedelta(minutes=self.TTL_MINUTES)).isoformat()
        with self.conn:
            self.conn.execute(
                "INSERT INTO pending_batches "
                "(company_id, confirm_token, trn_ids_json, totals_json, environment, expires_at) "
                "VALUES (?,?,?,?,?,?)",
                (company_id, token, json.dumps(trn_ids), json.dumps(totals), environment, expires),
            )
        return token

    def consume(self, confirm_token: str) -> sqlite3.Row | None:
        """Zwraca partie i oznacza jako zuzyta. None gdy nieznana/zuzyta/przeterminowana."""
        row = self.conn.execute(
            "SELECT * FROM pending_batches WHERE confirm_token=?", (confirm_token,)
        ).fetchone()
        if row is None or row["consumed_at"] is not None:
            return None
        if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
            return None
        with self.conn:
            self.conn.execute(
                "UPDATE pending_batches SET consumed_at=? WHERE id=?", (_now(), row["id"])
            )
        return row


class SessionRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def record_open(self, company_id: str, ref: str, environment: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO ksef_sessions (company_id, ref, environment, opened_at) "
                "VALUES (?,?,?,?)",
                (company_id, ref, environment, _now()),
            )

    def record_close(self, ref: str, upo_ref: str | None = None, upo_path: str | None = None):
        with self.conn:
            self.conn.execute(
                "UPDATE ksef_sessions SET closed_at=?, upo_ref=COALESCE(?, upo_ref), "
                "upo_path=COALESCE(?, upo_path) WHERE ref=?",
                (_now(), upo_ref, upo_path, ref),
            )


class DownloadRepo:
    """Pobrane faktury zakupu."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def insert_if_new(self, company_id: str, environment: str, ksef_number: str, **fields) -> bool:
        cols = ["company_id", "environment", "ksef_number", *fields.keys()]
        vals = [company_id, environment, ksef_number, *fields.values()]
        placeholders = ",".join("?" * len(cols))
        with self.conn:
            cur = self.conn.execute(
                f"INSERT OR IGNORE INTO downloaded_invoices ({','.join(cols)}) "
                f"VALUES ({placeholders})",
                vals,
            )
        return cur.rowcount > 0

    def list_for(self, company_id: str, environment: str, status: str | None = None):
        q = "SELECT * FROM downloaded_invoices WHERE company_id=? AND environment=?"
        params: list = [company_id, environment]
        if status:
            q += " AND status=?"
            params.append(status)
        q += " ORDER BY storage_date DESC, id DESC"
        return self.conn.execute(q, params).fetchall()

    def get(self, invoice_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM downloaded_invoices WHERE id=?", (invoice_id,)
        ).fetchone()

    def set_status(self, invoice_id: int, status: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        q = f"UPDATE downloaded_invoices SET status=?{', ' + sets if sets else ''} WHERE id=?"
        with self.conn:
            self.conn.execute(q, [status, *fields.values(), invoice_id])


class SyncStateRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get(self, company_id: str, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM sync_state WHERE company_id=? AND key=?", (company_id, key)
        ).fetchone()
        return row["value"] if row else None

    def set(self, company_id: str, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO sync_state (company_id, key, value, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(company_id, key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (company_id, key, value, _now()),
            )


class AuditRepo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def log(
        self,
        action: str,
        company_id: str | None = None,
        entity: str | None = None,
        entity_id: str | None = None,
        environment: str | None = None,
        details: dict | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO audit_log (company_id, action, entity, entity_id, environment, "
                "details_json) VALUES (?,?,?,?,?,?)",
                (
                    company_id,
                    action,
                    entity,
                    entity_id,
                    environment,
                    json.dumps(details, ensure_ascii=False, default=str) if details else None,
                ),
            )

    def recent(self, limit: int = 200):
        return self.conn.execute(
            "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
