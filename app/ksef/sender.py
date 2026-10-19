"""SendService - orkiestracja wysylki partii faktur do KSeF.

Przebieg: dla kazdej faktury z potwierdzonej partii:
    mapowanie -> budowa XML -> walidacja XSD -> wysylka w sesji online -> poll statusu.
Po wszystkich: zamkniecie sesji i pobranie UPO per faktura.
Stan kazdego kroku trafia do SQLite (GUI odpytuje partialem htmx).
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import date, timedelta
from pathlib import Path

from app.config import CompanyConfig, KsefEnv
from app.fa3.builder import build_fa3
from app.fa3.mapper import MappingError, map_invoice
from app.fa3.validator import validate
from app.ksef.gateway import KsefGateway
from app.ksef.ratelimit import RateLimitExceeded
from app.optima.reader import SalesReader
from app.state.repo import AuditRepo, SentRepo, SessionRepo

logger = logging.getLogger(__name__)

# Statusy KSeF dla faktury w sesji
STATUS_OK = 200
STATUS_DUPLICATE = 440


class SendService:
    # odstep miedzy probami pobrania UPO po zamknieciu sesji (nadpisywany w testach)
    UPO_RETRY_DELAY_S = 10.0

    def __init__(
        self,
        company_id: str,
        company: CompanyConfig,
        env: KsefEnv,
        gateway: KsefGateway,
        reader: SalesReader,
        sent_repo: SentRepo,
        session_repo: SessionRepo,
        audit: AuditRepo,
        invoice_store: Path,
    ):
        self.company_id = company_id
        self.company = company
        self.env = env
        self.gateway = gateway
        self.reader = reader
        self.sent = sent_repo
        self.sessions = session_repo
        self.audit = audit
        self.invoice_store = invoice_store

    def send_batch_async(self, trn_ids: list[int], date_from: date, date_to: date) -> None:
        thread = threading.Thread(
            target=self._send_batch,
            args=(trn_ids, date_from, date_to),
            daemon=True,
            name=f"send-{self.company_id}",
        )
        thread.start()

    def _send_batch(self, trn_ids: list[int], date_from: date, date_to: date) -> None:
        try:
            self._send_batch_inner(trn_ids, date_from, date_to)
        except Exception:
            logger.exception("Wysylka partii nie powiodla sie")
            for trn_id in trn_ids:
                row = self.sent.get(self.company_id, trn_id, self.env)
                if row and row["status"] in ("queued", "sent"):
                    self.sent.upsert(
                        self.company_id, trn_id, self.env,
                        status="error", error_detail="Blad krytyczny wysylki - patrz log",
                    )

    def _send_batch_inner(self, trn_ids: list[int], date_from: date, date_to: date) -> None:
        # szeroki zakres dat, by get_invoice znalazl fakture niezaleznie od filtra listy
        hint_from = date_from - timedelta(days=366)
        hint_to = date_to + timedelta(days=31)

        session = self.gateway.open_online_session()
        session_ref = self.gateway.session_ref(session)
        self.sessions.record_open(self.company_id, session_ref, self.env)
        self.audit.log(
            "session_open", self.company_id, "ksef_session", session_ref, self.env,
            {"trn_ids": trn_ids},
        )

        sent_refs: dict[int, str] = {}
        try:
            for trn_id in trn_ids:
                try:
                    self._send_one(session, session_ref, trn_id, hint_from, hint_to, sent_refs)
                except Exception as exc:
                    # blad jednej faktury nie przerywa partii
                    logger.exception("Wysylka faktury TrN_TrNID=%s nie powiodla sie", trn_id)
                    self._mark_failed(trn_id, exc)
        finally:
            try:
                self.gateway.close_session(session)
                self.sessions.record_close(session_ref)
                self.audit.log(
                    "session_close", self.company_id, "ksef_session", session_ref, self.env
                )
            except Exception:
                logger.exception("Zamkniecie sesji %s nie powiodlo sie", session_ref)

        # UPO per faktura - generowane asynchronicznie po zamknieciu sesji,
        # zwykle w ciagu 1-3 minut. Retry z odstepem; brak UPO nie jest bledem
        # wysylki (mozna je pobrac pozniej przyciskiem w GUI).
        import time

        pending = dict(sent_refs)
        for _attempt in range(12):
            if not pending:
                break
            time.sleep(self.UPO_RETRY_DELAY_S)
            for trn_id, invoice_ref in list(pending.items()):
                try:
                    upo = self.gateway.fetch_upo(session_ref, invoice_ref)
                except Exception:
                    continue
                upo_path = self._store_file(f"upo-{invoice_ref}.xml", upo)
                self.sent.upsert(self.company_id, trn_id, self.env, upo_path=str(upo_path))
                del pending[trn_id]
        if pending:
            logger.info("UPO jeszcze niegotowe dla %s - do pobrania z GUI", list(pending.values()))

    def _send_one(
        self, session, session_ref: str, trn_id: int,
        hint_from: date, hint_to: date, sent_refs: dict[int, str],
    ) -> None:
        inv = self.reader.get_invoice(trn_id, hint_from, hint_to)
        if inv is None:
            self.sent.upsert(
                self.company_id, trn_id, self.env,
                status="error", numer_pelny="?",
                error_detail="Faktura zniknela z bazy Optimy (bufor/anulowanie?)",
            )
            return
        try:
            fa3 = map_invoice(
                inv,
                seller_nip=self.company.nip,
                seller_name=self.company.name,
                seller_address_l1=self.company.address_line1,
                seller_address_l2=self.company.address_line2,
                seller_country=self.company.country_code,
            )
            xml = build_fa3(fa3)
            errors = validate(xml)
            if errors:
                raise MappingError("; ".join(errors[:5]))
        except MappingError as exc:
            self.sent.upsert(
                self.company_id, trn_id, self.env,
                status="rejected", numer_pelny=inv.numer_pelny,
                error_detail=str(exc),
            )
            return

        xml_path = self._store_file(f"fa3-{self.company_id}-{trn_id}.xml", xml)
        sha = hashlib.sha256(xml).hexdigest()
        self.sent.upsert(
            self.company_id, trn_id, self.env,
            status="sent", numer_pelny=inv.numer_pelny,
            buyer_nip=inv.buyer.nip_normalized if inv.buyer else None,
            xml_path=str(xml_path), xml_sha256=sha, session_ref=session_ref,
        )
        try:
            invoice_ref = self.gateway.send_invoice(session, xml)
        except RateLimitExceeded as exc:
            self.sent.upsert(
                self.company_id, trn_id, self.env,
                status="error", error_detail=str(exc),
            )
            return
        self.sent.upsert(self.company_id, trn_id, self.env, invoice_ref=invoice_ref)
        self.audit.log(
            "invoice_sent", self.company_id, "sales_invoice", str(trn_id), self.env,
            {"numer": inv.numer_pelny, "invoice_ref": invoice_ref, "sha256": sha},
        )

        status = self.gateway.wait_for_invoice(session, invoice_ref)
        code = getattr(getattr(status, "status", None), "code", None)
        ksef_number = getattr(status, "ksef_number", None)
        if code == STATUS_OK or ksef_number:
            self.sent.upsert(
                self.company_id, trn_id, self.env,
                status="accepted", ksef_number=ksef_number,
            )
            sent_refs[trn_id] = invoice_ref
        elif code == STATUS_DUPLICATE:
            self.sent.upsert(
                self.company_id, trn_id, self.env,
                status="duplicate", error_code=code,
                error_detail=_status_detail(status),
            )
        else:
            self.sent.upsert(
                self.company_id, trn_id, self.env,
                status="rejected", error_code=code,
                error_detail=_status_detail(status),
            )
        self.audit.log(
            "invoice_status", self.company_id, "sales_invoice", str(trn_id), self.env,
            {"code": code, "ksef_number": ksef_number},
        )

    def _mark_failed(self, trn_id: int, exc: Exception) -> None:
        row = self.sent.get(self.company_id, trn_id, self.env)
        if row and row["status"] == "sent":
            # XML juz poszedl do KSeF - faktura mogla zostac przyjeta
            detail = (
                f"Wyslano do KSeF, ale status nieznany ({exc}) - sprawdz w KSeF "
                f"przed ponowna wysylka"
            )
        else:
            detail = f"Blad przed wysylka do KSeF: {exc}"
        extra = {} if row else {"numer_pelny": "?"}
        self.sent.upsert(
            self.company_id, trn_id, self.env, status="error", error_detail=detail[:2000], **extra,
        )

    def _store_file(self, name: str, content: bytes) -> Path:
        path = self.invoice_store / self.company_id / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path


def _status_detail(status) -> str:
    info = getattr(status, "status", None)
    desc = getattr(info, "description", None) or ""
    details = getattr(info, "details", None) or []
    if details:
        desc += " | " + "; ".join(str(d) for d in details)
    return desc[:2000]
