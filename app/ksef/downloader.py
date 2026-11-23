"""DownloadService - synchronizacja faktur zakupu z KSeF.

Kursor: najwyzsza widziana data permanentStorageDate (HWM) per firma.
Budzet: zapytania o metadane = 20/h per kontekst - kazda strona zuzywa 1 jednostke,
limiter jest utrwalony w SQLite i bramkuje zarowno scheduler jak i przycisk reczny.
HWM przesuwa sie po zapisaniu metadanych calej strony.

XML nie pobiera sie sam: synchronizacja zapisuje tylko metadane (wiersz bez xml_path).
Uzytkownik wskazuje faktury do pobrania - trafiaja do kolejki xml_queue, ktora w tle
oproznia process_queue(). KSeF pozwala na 16 pobran/min i 64/h, wiec kolejnosc w kolejce
decyduje, ktore faktury beda dostepne najpierw; kolejke mozna zatrzymac i wyczyscic.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from ksef2.core.exceptions import KSeFAuthError, KSeFRateLimitError

from app.config import CompanyConfig, KsefEnv
from app.fa3.parser import ParseError, parse_invoice
from app.ksef.gateway import KsefGateway
from app.ksef.ratelimit import RateLimiter, RateLimitExceeded
from app.state.repo import AuditRepo, DownloadRepo, SyncStateRepo, XmlQueueRepo

logger = logging.getLogger(__name__)

HWM_KEY = "purchase_hwm"
BLOCKED_KEY = "download_blocked_until"
BLOCK_REASON_KEY = "download_blocked_reason"
PAUSED_KEY = "xml_queue_paused"
ERROR_BACKOFF = timedelta(seconds=60)  # po bledzie pobrania jednej faktury
# Blad logowania albo polaczenia dotyczy calej kolejki, nie faktury: pozycja zostaje,
# a kolejka ponawia probe po przerwie (logowanie rzadziej - zly token sam sie nie naprawi).
CONNECTION_BACKOFF = timedelta(seconds=60)
AUTH_BACKOFF = timedelta(minutes=5)
MAX_RANGE_DAYS = 88  # zapas ponizej limitu 3 miesiecy na zakres dat


def blocked_until(sync_state: SyncStateRepo, company_id: str, env: str) -> datetime | None:
    """Do kiedy pobieranie XML jest wstrzymane (limit KSeF albo blad); None = mozna pobierac."""
    raw = sync_state.get(company_id, f"{BLOCKED_KEY}:{env}")
    until = datetime.fromisoformat(raw) if raw else None
    return until if until and until > datetime.now(UTC) else None


def blocked_reason(sync_state: SyncStateRepo, company_id: str, env: str) -> str:
    """Powod trwajacej przerwy, gdy jest nim blad (pusty napis = zwykly limit KSeF)."""
    return sync_state.get(company_id, f"{BLOCK_REASON_KEY}:{env}") or ""


def queue_paused(sync_state: SyncStateRepo, company_id: str, env: str) -> bool:
    """Czy uzytkownik zatrzymal kolejke pobierania XML."""
    return sync_state.get(company_id, f"{PAUSED_KEY}:{env}") == "1"


def set_queue_paused(sync_state: SyncStateRepo, company_id: str, env: str, paused: bool) -> None:
    sync_state.set(company_id, f"{PAUSED_KEY}:{env}", "1" if paused else "")


class DownloadService:
    def __init__(
        self,
        company_id: str,
        company: CompanyConfig,
        env: KsefEnv,
        gateway: KsefGateway,
        limiter: RateLimiter,
        downloads: DownloadRepo,
        queue: XmlQueueRepo,
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
        self.queue = queue
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
        """Jedna runda synchronizacji metadanych (bez XML). Zwraca statystyki dla GUI/CLI."""
        start_cursor = self._cursor()
        cursor = start_cursor
        now = datetime.now(UTC)
        stats = {"pages": 0, "new": 0, "known": 0, "errors": 0}

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
        return blocked_until(self.sync_state, self.company_id, self.env)

    def _block_downloads(self, until: datetime, reason: str = "") -> None:
        self.sync_state.set(self.company_id, f"{BLOCKED_KEY}:{self.env}", until.isoformat())
        self.sync_state.set(self.company_id, f"{BLOCK_REASON_KEY}:{self.env}", reason)
        logger.info(
            "Pobieranie XML dla %s wstrzymane - wznowienie o %s%s",
            self.company_id, until.astimezone().strftime("%H:%M:%S"),
            f" ({reason})" if reason else "",
        )

    def enqueue(self, invoice_ids: list[int], *, front: bool = False) -> dict:
        """Wstawia wskazane faktury (te bez XML) do kolejki pobierania."""
        result = self.queue.add(self.company_id, self.env, invoice_ids, front=front)
        result["queued"] = self.queue.count(self.company_id, self.env)
        if result["added"] or result["moved"]:
            self.audit.log(
                "xml_queue_add", self.company_id, environment=self.env,
                details={**result, "front": front},
            )
        return result

    def queue_ready(self) -> bool:
        """Czy kolejka moze teraz pobierac: nie zatrzymana i bez przerwy na limit."""
        return (
            not queue_paused(self.sync_state, self.company_id, self.env)
            and self._blocked_until() is None
        )

    def process_queue(self) -> dict:
        """Pobiera XML z kolejki po kolei, az sie skonczy, zostanie zatrzymana albo trafi na limit.

        Stan (zatrzymanie, kolejnosc) czytany jest przed kazda faktura, wiec zmiany z GUI
        dzialaja od nastepnej pozycji - trwajace pobranie zawsze dobiega konca.
        """
        fetched = failed = 0
        while self.queue_ready():
            item = self.queue.claim_next(self.company_id, self.env)
            if item is None:
                break
            outcome = self._fetch(item["invoice_id"], item["ksef_number"])
            if outcome == "limited":
                self.queue.release(item["invoice_id"])
                break
            self.queue.remove(item["invoice_id"])
            if outcome == "failed":
                # blad zostaje przy fakturze (mozna ja dodac ponownie); chwila przerwy,
                # zeby seria bledow nie oznaczyla bledem calej kolejki w kilka sekund
                failed += 1
                self._block_downloads(
                    datetime.now(UTC) + ERROR_BACKOFF, "blad pobrania faktury - szczegoly na liscie"
                )
                break
            fetched += 1
        return {
            "fetched": fetched, "failed": failed,
            "queued": self.queue.count(self.company_id, self.env),
        }

    def _ingest(self, meta) -> bool:
        """Utrwala metadane faktury (XML pobiera dopiero kolejka). True = nowa, False = znana."""
        return self.downloads.insert_if_new(
            self.company_id, self.env, meta.ksef_number,
            seller_nip=meta.seller_nip, seller_name=meta.seller_name,
            invoice_number=meta.invoice_number, issue_date=meta.issue_date,
            storage_date=meta.storage_date, net=meta.net, vat=meta.vat,
            gross=meta.gross, currency=meta.currency,
        )

    def _fetch(self, row_id: int, ksef_number: str) -> str:
        """Pobiera i zapisuje XML jednej faktury.

        Zwraca "done" (XML zapisany), "limited" (limit KSeF, blad logowania albo
        polaczenia - faktura zostaje w kolejce) albo "failed" (blad zapisany przy fakturze).
        """
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
            return "limited"
        except KSeFRateLimitError as exc:
            # serwer i tak odmowil (np. z tego samego NIP korzysta inny program)
            wait = timedelta(seconds=(exc.retry_after or 60) + 2)
            self._block_downloads(datetime.now(UTC) + wait)
            return "limited"
        except KSeFAuthError as exc:
            # bramka juz ponowila logowanie - to problem tokena/uprawnien, nie tej faktury
            logger.warning("KSeF odmowil dostepu przy pobieraniu %s: %s", ksef_number, exc)
            self._block_downloads(
                datetime.now(UTC) + AUTH_BACKOFF,
                f"KSeF odmowil dostepu ({exc.status_code}) - sprawdz token w Ustawieniach",
            )
            return "limited"
        except httpx.HTTPError as exc:
            logger.warning("Brak polaczenia z KSeF przy pobieraniu %s: %s", ksef_number, exc)
            self._block_downloads(
                datetime.now(UTC) + CONNECTION_BACKOFF, f"brak polaczenia z KSeF: {exc}"[:200]
            )
            return "limited"
        except Exception as exc:
            logger.exception("Pobranie %s nie powiodlo sie", ksef_number)
            self.downloads.set_status(row_id, "error", error_detail=str(exc)[:1000])
            return "failed"
        # sciezka zapisana przed parsowaniem - blad parsowania nie wraca faktury do kolejki
        self.downloads.set_status(row_id, "new", xml_path=str(xml_path), error_detail=None)
        try:
            parsed = parse_invoice(xml, ksef_number)
        except ParseError as exc:
            logger.warning("Parsowanie %s nie powiodlo sie: %s", ksef_number, exc)
            self.downloads.set_status(row_id, "error", error_detail=str(exc)[:1000])
            return "done"
        fields = {
            "net": float(parsed.total_net), "vat": float(parsed.total_vat),
            "gross": float(parsed.total_gross), "currency": parsed.currency,
        }
        if parsed.seller_name:
            fields["seller_name"] = parsed.seller_name
        self.downloads.set_status(row_id, "new", **fields)
        return "done"
