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

    def reset_interrupted(self) -> int:
        """Po restarcie: wysylki przerwane zamknieciem programu przestaja wisiec w kolejce.

        Bez tego faktura zostawala "w kolejce"/"wyslano" na zawsze (bez pola wyboru na liscie).
        """
        with self.conn:
            queued = self.conn.execute(
                "UPDATE sent_invoices SET status='error', updated_at=?, error_detail="
                "'Wysylka przerwana (program zamkniety) przed wyslaniem do KSeF - mozna wyslac "
                "ponownie' WHERE status='queued'",
                (_now(),),
            ).rowcount
            sent = self.conn.execute(
                "UPDATE sent_invoices SET status='error', updated_at=?, error_detail="
                "'Program zamkniety w trakcie wysylki - faktura mogla trafic do KSeF, sprawdz "
                "w KSeF przed ponowna wysylka' WHERE status='sent'",
                (_now(),),
            ).rowcount
        return queued + sent

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
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM pending_batches WHERE confirm_token=?", (confirm_token,)
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                return None
            if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
                return None
            # warunek consumed_at IS NULL: dwa rownoczesne potwierdzenia (podwojne klikniecie)
            # nie moga oba dostac tej samej partii - wysylka poszlaby dwa razy
            cur = self.conn.execute(
                "UPDATE pending_batches SET consumed_at=? WHERE id=? AND consumed_at IS NULL",
                (_now(), row["id"]),
            )
            if cur.rowcount != 1:
                return None
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

    def pending_xml(self, company_id: str, environment: str):
        """Faktury znane z metadanych, ktorych XML jeszcze nie pobrano (najstarsze najpierw)."""
        return self.conn.execute(
            "SELECT * FROM downloaded_invoices WHERE company_id=? AND environment=? "
            "AND xml_path IS NULL AND status IN ('new','error') ORDER BY storage_date, id",
            (company_id, environment),
        ).fetchall()

    def set_status(self, invoice_id: int, status: str, **fields) -> None:
        sets = ", ".join(f"{k}=?" for k in fields)
        q = f"UPDATE downloaded_invoices SET status=?{', ' + sets if sets else ''} WHERE id=?"
        with self.conn:
            self.conn.execute(q, [status, *fields.values(), invoice_id])


class XmlQueueRepo:
    """Kolejka pobierania XML faktur zakupu (kolejnosc = rosnace position)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(
        self, company_id: str, environment: str, invoice_ids: list[int], *, front: bool = False
    ) -> dict:
        """Dodaje faktury bez XML w podanej kolejnosci; front=True stawia je przed reszta.

        Faktury juz stojace w kolejce zostaja na miejscu, chyba ze front=True - wtedy
        przechodza na poczatek. Faktura wlasnie pobierana nie jest ruszana.
        """
        scope = (company_id, environment)
        with self.conn:
            eligible = {
                row["id"] for row in self.conn.execute(
                    "SELECT id FROM downloaded_invoices WHERE company_id=? AND environment=? "
                    "AND xml_path IS NULL AND status IN ('new','error')", scope,
                )
            }
            queued = {
                row["invoice_id"]: row["state"] for row in self.conn.execute(
                    "SELECT invoice_id, state FROM xml_queue WHERE company_id=? AND environment=?",
                    scope,
                )
            }
            ids = [
                i for i in dict.fromkeys(invoice_ids)
                if i in eligible and queued.get(i) != "downloading" and (front or i not in queued)
            ]
            low, high = self._bounds(company_id, environment)
            start = low - len(ids) if front else high + 1
            for offset, invoice_id in enumerate(ids):
                self.conn.execute(
                    "INSERT INTO xml_queue (invoice_id, company_id, environment, position) "
                    "VALUES (?,?,?,?) ON CONFLICT(invoice_id) DO UPDATE SET "
                    "position=excluded.position",
                    (invoice_id, company_id, environment, start + offset),
                )
        moved = sum(1 for i in ids if i in queued)
        return {"added": len(ids) - moved, "moved": moved}

    def _bounds(self, company_id: str, environment: str) -> tuple[int, int]:
        row = self.conn.execute(
            "SELECT COALESCE(MIN(position), 0) AS low, COALESCE(MAX(position), 0) AS high "
            "FROM xml_queue WHERE company_id=? AND environment=?",
            (company_id, environment),
        ).fetchone()
        return row["low"], row["high"]

    def count(self, company_id: str, environment: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM xml_queue WHERE company_id=? AND environment=?",
            (company_id, environment),
        ).fetchone()[0]

    def ids(self, company_id: str, environment: str) -> set[int]:
        return {
            row["invoice_id"] for row in self.conn.execute(
                "SELECT invoice_id FROM xml_queue WHERE company_id=? AND environment=?",
                (company_id, environment),
            )
        }

    def items(self, company_id: str, environment: str, limit: int | None = None):
        """Pozycje z danymi faktury: najpierw wlasnie pobierana, potem wg kolejnosci."""
        q = (
            "SELECT q.invoice_id, q.state, q.position, d.ksef_number, d.invoice_number, "
            "d.seller_name, d.issue_date, d.storage_date, d.gross "
            "FROM xml_queue q JOIN downloaded_invoices d ON d.id = q.invoice_id "
            "WHERE q.company_id=? AND q.environment=? "
            "ORDER BY q.state = 'downloading' DESC, q.position, q.invoice_id"
        )
        params: list = [company_id, environment]
        if limit is not None:
            q += " LIMIT ?"
            params.append(limit)
        return self.conn.execute(q, params).fetchall()

    def claim_next(self, company_id: str, environment: str) -> sqlite3.Row | None:
        """Oznacza pierwsza czekajaca pozycje jako pobierana i ja zwraca."""
        with self.conn:
            row = self.conn.execute(
                "SELECT q.invoice_id, d.ksef_number FROM xml_queue q "
                "JOIN downloaded_invoices d ON d.id = q.invoice_id "
                "WHERE q.company_id=? AND q.environment=? AND q.state='queued' "
                "ORDER BY q.position, q.invoice_id LIMIT 1",
                (company_id, environment),
            ).fetchone()
            if row is not None:
                self.conn.execute(
                    "UPDATE xml_queue SET state='downloading' WHERE invoice_id=?",
                    (row["invoice_id"],),
                )
        return row

    def release(self, invoice_id: int) -> None:
        """Pobranie sie nie odbylo (limit) - pozycja wraca na swoje miejsce w kolejce."""
        with self.conn:
            self.conn.execute(
                "UPDATE xml_queue SET state='queued' WHERE invoice_id=?", (invoice_id,)
            )

    def release_all(self) -> None:
        """Po restarcie nic nie jest w trakcie pobierania."""
        with self.conn:
            self.conn.execute("UPDATE xml_queue SET state='queued' WHERE state='downloading'")

    def remove(self, invoice_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM xml_queue WHERE invoice_id=?", (invoice_id,))

    def remove_queued(self, company_id: str, environment: str, invoice_id: int) -> bool:
        """Usuwa czekajaca pozycje; wlasnie pobieranej nie da sie juz cofnac."""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM xml_queue WHERE invoice_id=? AND company_id=? AND environment=? "
                "AND state='queued'",
                (invoice_id, company_id, environment),
            )
        return cur.rowcount > 0

    def purge(self, company_id: str, environment: str) -> int:
        """Czysci kolejke firmy. Trwajace pobranie dobiegnie konca, kolejne juz nie rusza."""
        with self.conn:
            cur = self.conn.execute(
                "DELETE FROM xml_queue WHERE company_id=? AND environment=?",
                (company_id, environment),
            )
        return cur.rowcount

    def move(self, company_id: str, environment: str, invoice_id: int, where: str) -> bool:
        """Zmienia miejsce czekajacej pozycji: top / up / down / bottom."""
        scope = (company_id, environment)
        with self.conn:
            current = self.conn.execute(
                "SELECT position FROM xml_queue WHERE invoice_id=? AND company_id=? "
                "AND environment=? AND state='queued'",
                (invoice_id, *scope),
            ).fetchone()
            if current is None:
                return False
            position = current["position"]
            if where in ("top", "bottom"):
                low, high = self._bounds(*scope)
                target = low - 1 if where == "top" else high + 1
                self.conn.execute(
                    "UPDATE xml_queue SET position=? WHERE invoice_id=?", (target, invoice_id)
                )
                return True
            if where not in ("up", "down"):
                return False
            compare, order = ("<", "DESC") if where == "up" else (">", "ASC")
            neighbour = self.conn.execute(
                "SELECT invoice_id, position FROM xml_queue WHERE company_id=? AND environment=? "
                f"AND state='queued' AND position {compare} ? ORDER BY position {order} LIMIT 1",
                (*scope, position),
            ).fetchone()
            if neighbour is None:
                return False
            self.conn.execute(
                "UPDATE xml_queue SET position=? WHERE invoice_id=?",
                (neighbour["position"], invoice_id),
            )
            self.conn.execute(
                "UPDATE xml_queue SET position=? WHERE invoice_id=?",
                (position, neighbour["invoice_id"]),
            )
        return True


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
