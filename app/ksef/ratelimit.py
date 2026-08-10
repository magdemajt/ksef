"""Przesuwne okno limitow API KSeF, utrwalane w SQLite (przezywa restart).

Najciasniejszy limit: zapytania o metadane faktur i eksporty = 20/h per kontekst (NIP).
Limity sekundowe (8/s download itd.) zalatwiamy prostym odstepem miedzy wywolaniami,
tutaj pilnujemy tylko budzetow godzinowych, ktorych przekroczenie boli (429 + blokada).
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

# endpoint -> (limit, okno)
LIMITS: dict[str, tuple[int, timedelta]] = {
    "invoice_metadata_or_export": (20, timedelta(hours=1)),
    "invoice_send": (180, timedelta(hours=1)),
}


class RateLimitExceeded(RuntimeError):
    def __init__(self, endpoint: str, resets_at: datetime):
        super().__init__(
            f"Limit API KSeF dla {endpoint!r} wyczerpany; odnowienie o "
            f"{resets_at.astimezone().strftime('%H:%M:%S')}"
        )
        self.endpoint = endpoint
        self.resets_at = resets_at


class RateLimiter:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def _window_rows(self, company_id: str, endpoint: str) -> list[str]:
        limit, window = LIMITS[endpoint]
        cutoff = (datetime.now(UTC) - window).isoformat()
        self.conn.execute(
            "DELETE FROM rate_limits WHERE company_id=? AND endpoint=? AND ts < ?",
            (company_id, endpoint, cutoff),
        )
        rows = self.conn.execute(
            "SELECT ts FROM rate_limits WHERE company_id=? AND endpoint=? ORDER BY ts",
            (company_id, endpoint),
        ).fetchall()
        return [r["ts"] for r in rows]

    def remaining(self, company_id: str, endpoint: str) -> int:
        limit, _ = LIMITS[endpoint]
        return max(0, limit - len(self._window_rows(company_id, endpoint)))

    def resets_at(self, company_id: str, endpoint: str) -> datetime | None:
        _, window = LIMITS[endpoint]
        rows = self._window_rows(company_id, endpoint)
        if not rows:
            return None
        return datetime.fromisoformat(rows[0]) + window

    def acquire(self, company_id: str, endpoint: str) -> None:
        """Zuzywa jedna jednostke budzetu albo rzuca RateLimitExceeded."""
        limit, window = LIMITS[endpoint]
        with self.conn:
            rows = self._window_rows(company_id, endpoint)
            if len(rows) >= limit:
                raise RateLimitExceeded(
                    endpoint, datetime.fromisoformat(rows[0]) + window
                )
            self.conn.execute(
                "INSERT INTO rate_limits (company_id, endpoint, ts) VALUES (?,?,?)",
                (company_id, endpoint, datetime.now(UTC).isoformat()),
            )
