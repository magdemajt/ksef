"""DownloadService - synchronizacja faktur zakupu z KSeF.

Kursor: najwyzsza widziana data permanentStorageDate (HWM) per firma.
Budzet: zapytania o metadane = 20/h per kontekst - kazda strona zuzywa 1 jednostke,
limiter jest utrwalony w SQLite i bramkuje zarowno scheduler jak i przycisk reczny.
HWM przesuwa sie po zapisaniu metadanych calej strony. XML pobierany jest osobno:
KSeF pozwala na 16 pobran/min i 64/h, wiec przy wiekszej liczbie faktur (nowa firma)
reszta czeka w kolejce (wiersz bez xml_path) i dociaga sie w kolejnych rundach.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ksef2.core.exceptions import KSeFRateLimitError

from app.config import CompanyConfig, KsefEnv
from app.fa3.parser import ParseError, parse_invoice
from app.ksef.gateway import KsefGateway
from app.ksef.ratelimit import RateLimiter, RateLimitExceeded
from app.state.repo import AuditRepo, DownloadRepo, SyncStateRepo

logger = logging.getLogger(__name__)

HWM_KEY = "purchase_hwm"
BLOCKED_KEY = "download_blocked_until"
MAX_RANGE_DAYS = 88  # zapas ponizej limitu 3 miesiecy na zakres dat


class DownloadService:
    def __init__(
        self,
        company_id: str,
        company: CompanyConfig,
        env: KsefEnv,
        gateway: KsefGateway,
        limiter: RateLimiter,
        downloads: DownloadRepo,
        sync_state: SyncStateRepo,
        audit: AuditRepo,
        invoice_store: Path,
        backfill_start: str,
    ):
        self.company_id = company_id
        self.company = company
        self.env = env
        self.gateway = gateway
        self.limiter = limiter
        self.downloads = downloads
        self.sync_state = sync_state
        self.audit = audit
        self.invoice_store = invoice_store
        self.backfill_start = backfill_start

    def metadata_budget(self) -> int:
        return self.limiter.remaining(self.company_id, "invoice_metadata_or_export")

    def _cursor(self) -> datetime:
        raw = self.sync_state.get(self.company_id, f"{HWM_KEY}:{self.env}")
        if raw:
            return datetime.fromisoformat(raw)
        return datetime.fromisoformat(self.backfill_start).replace(tzinfo=UTC)

    def _advance_cursor(self, value: datetime) -> None:
        self.sync_state.set(self.company_id, f"{HWM_KEY}:{self.env}", value.isoformat())

    def sync(self, max_pages: int = 4) -> dict:
        """Jedna runda synchronizacji. Zwraca statystyki dla GUI/CLI."""
        start_cursor = self._cursor()
        cursor = start_cursor
        now = datetime.now(UTC)
        stats = {"pages": 0, "new": 0, "known": 0, "errors": 0}
        self.fetch_pending()  # zalegle XML maja pierwszenstwo przed nowymi fakturami

        while stats["pages"] < max_pages:
            window_end = min(cursor + timedelta(days=MAX_RANGE_DAYS), now)
            page = self.gateway.query_purchase_metadata(
                date_from=cursor, date_to=window_end, page_offset=0
            )
            stats["pages"] += 1
            newest_seen: datetime | None = None
            for meta in page.invoices:
                stored = self._ingest(meta)
                stats["new" if stored else "known"] += 1
                if meta.storage_date:
                    try:
                        seen = datetime.fromisoformat(meta.storage_date)
                        if seen.tzinfo is None:
                            seen = seen.replace(tzinfo=UTC)
                        newest_seen = max(newest_seen or seen, seen)
                    except ValueError:
                        pass

            # cala strona utrwalona -> mozna przesunac kursor
            if newest_seen is not None:
                cursor = newest_seen
                self._advance_cursor(cursor)
            elif not page.invoices and window_end < now:
                # puste okno w przeszlosci - przeskocz je
                cursor = window_end
                self._advance_cursor(cursor)

            if not (page.has_more or page.is_truncated):
                if window_end >= now:
                    break
                cursor = max(cursor, window_end)
                self._advance_cursor(cursor)

        stats["pending"] = len(self.downloads.pending_xml(self.company_id, self.env))
        self.audit.log(
            "purchase_sync", self.company_id, environment=self.env,
            details={**stats, "cursor_before": str(start_cursor), "cursor_after": str(cursor)},
        )
        return stats

    # -- pobieranie XML --

    def _blocked_until(self) -> datetime | None:
        raw = self.sync_state.get(self.company_id, f"{BLOCKED_KEY}:{self.env}")
        until = datetime.fromisoformat(raw) if raw else None
        return until if until and until > datetime.now(UTC) else None

    def _block_downloads(self, until: datetime) -> None:
        self.sync_state.set(self.company_id, f"{BLOCKED_KEY}:{self.env}", until.isoformat())
        logger.info(
            "Limit pobran KSeF dla %s - wznowienie o %s",
            self.company_id, until.astimezone().strftime("%H:%M:%S"),
        )

    def fetch_pending(self) -> dict:
        """Dociaga XML faktur czekajacych w kolejce, dopoki pozwala na to limit KSeF."""
        # wiersze, ktore starsza wersja oznaczyla bledem z powodu limitu (429) - to kolejka
        with self.downloads.conn:
            self.downloads.conn.execute(
                "UPDATE downloaded_invoices SET status='new', error_detail=NULL "
                "WHERE company_id=? AND environment=? AND xml_path IS NULL "
                "AND status='error' AND error_detail LIKE '%RATE_LIMIT%'",
                (self.company_id, self.env),
            )
        fetched = 0
        for row in self.downloads.pending_xml(self.company_id, self.env):
            if self._blocked_until() is not None:
                break
            fetched += self._fetch(row["id"], row["ksef_number"])
        pending = len(self.downloads.pending_xml(self.company_id, self.env))
        return {"fetched": fetched, "pending": pending}

    def _ingest(self, meta) -> bool:
        """Utrwala fakture z metadanych i probuje pobrac XML. True = nowa, False = juz znana."""
        existing_new = self.downloads.insert_if_new(
            self.company_id, self.env, meta.ksef_number,
            seller_nip=meta.seller_nip, seller_name=meta.seller_name,
            invoice_number=meta.invoice_number, issue_date=meta.issue_date,
            storage_date=meta.storage_date, net=meta.net, vat=meta.vat,
            gross=meta.gross, currency=meta.currency,
        )
        if not existing_new:
            return False
        row = self._row_id(meta.ksef_number)
        if row and self._blocked_until() is None:
            self._fetch(row, meta.ksef_number)
        return True

    def _fetch(self, row_id: int, ksef_number: str) -> bool:
        """Pobiera i zapisuje XML jednej faktury. False = zostaje w kolejce albo blad."""
        xml_path = self.invoice_store / self.company_id / f"{ksef_number}.xml"
        try:
            if xml_path.exists() and xml_path.stat().st_size:
                xml = xml_path.read_bytes()  # plik z wczesniejszej, przerwanej proby
            else:
                xml = self.gateway.download_invoice(ksef_number)
                xml_path.parent.mkdir(parents=True, exist_ok=True)
                xml_path.write_bytes(xml)
        except RateLimitExceeded as exc:
            self._block_downloads(exc.resets_at)
            return False
        except KSeFRateLimitError as exc:
            # serwer i tak odmowil (np. z tego samego NIP korzysta inny program)
            wait = timedelta(seconds=(exc.retry_after or 60) + 2)
            self._block_downloads(datetime.now(UTC) + wait)
            return False
        except Exception as exc:
            logger.exception("Pobranie %s nie powiodlo sie", ksef_number)
            self.downloads.set_status(row_id, "error", error_detail=str(exc)[:1000])
            return False
        # sciezka zapisana przed parsowaniem - blad parsowania nie wraca faktury do kolejki
        self.downloads.set_status(row_id, "new", xml_path=str(xml_path), error_detail=None)
        try:
            parsed = parse_invoice(xml, ksef_number)
        except ParseError as exc:
            logger.warning("Parsowanie %s nie powiodlo sie: %s", ksef_number, exc)
            self.downloads.set_status(row_id, "error", error_detail=str(exc)[:1000])
            return True
        fields = {
            "net": float(parsed.total_net), "vat": float(parsed.total_vat),
            "gross": float(parsed.total_gross), "currency": parsed.currency,
        }
        if parsed.seller_name:
            fields["seller_name"] = parsed.seller_name
        self.downloads.set_status(row_id, "new", **fields)
        return True

    def _row_id(self, ksef_number: str) -> int | None:
        row = self.downloads.conn.execute(
            "SELECT id FROM downloaded_invoices "
            "WHERE company_id=? AND ksef_number=? AND environment=?",
            (self.company_id, ksef_number, self.env),
        ).fetchone()
        return row["id"] if row else None
