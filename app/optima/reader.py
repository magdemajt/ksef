"""Odczyt faktur sprzedazy z bazy Optimy (login read-only).

Czesc kolumn (KSeF, waluta/kurs) rozni sie miedzy wersjami Optimy i nie jest
publicznie udokumentowana - reader introspekcjonuje INFORMATION_SCHEMA i buduje
SELECT tylko z kolumn faktycznie istniejacych. `ksef-bridge diagnose` raportuje
co znaleziono.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from app.optima.models import Contractor, SalesInvoice, SalesLine

logger = logging.getLogger(__name__)

TYP_FAKTURA_SPRZEDAZY = 302

# Kolumny-kandydaci, ktorych obecnosc weryfikujemy w INFORMATION_SCHEMA
CURRENCY_CANDIDATES = ("TrN_Waluta",)
RATE_NUM_CANDIDATES = ("TrN_KursL",)
RATE_DEN_CANDIDATES = ("TrN_KursM",)
# Kolumny KSeF na TraNag (Optima 2026 z aktywnym modulem KSeF) - nazwy ustalane w runtime
KSEF_NUMBER_HINTS = ("numer", "nrksef")
KSEF_STATUS_HINTS = ("status",)


class SalesReader:
    def __init__(self, conn):
        self.conn = conn
        self._tranag_cols: set[str] | None = None
        self._knt_cols: set[str] | None = None

    # -- introspekcja --

    def _columns(self, table: str) -> set[str]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='CDN' AND TABLE_NAME=?",
            (table,),
        )
        return {row[0] for row in cur.fetchall()}

    def tranag_columns(self) -> set[str]:
        if self._tranag_cols is None:
            self._tranag_cols = self._columns("TraNag")
        return self._tranag_cols

    def _pick(self, candidates: tuple[str, ...]) -> str | None:
        cols = self.tranag_columns()
        for cand in candidates:
            if cand in cols:
                return cand
        return None

    def ksef_columns(self) -> dict[str, str]:
        """Kolumny KSeF wykryte na TraNag: {'number': ..., 'status': ...}."""
        found: dict[str, str] = {}
        for col in self.tranag_columns():
            low = col.lower()
            if "ksef" not in low:
                continue
            if any(h in low for h in KSEF_NUMBER_HINTS) and "number" not in found:
                found["number"] = col
            elif any(h in low for h in KSEF_STATUS_HINTS) and "status" not in found:
                found["status"] = col
        return found

    # -- odczyt --

    def list_sales(self, date_from: date, date_to: date) -> list[SalesInvoice]:
        waluta_col = self._pick(CURRENCY_CANDIDATES)
        kurs_l_col = self._pick(RATE_NUM_CANDIDATES)
        kurs_m_col = self._pick(RATE_DEN_CANDIDATES)
        ksef = self.ksef_columns()

        select_extra = []
        select_extra.append(f"T.{waluta_col}" if waluta_col else "'PLN'")
        select_extra.append(f"T.{kurs_l_col}" if kurs_l_col else "NULL")
        select_extra.append(f"T.{kurs_m_col}" if kurs_m_col else "NULL")
        select_extra.append(f"T.{ksef['number']}" if "number" in ksef else "NULL")
        select_extra.append(f"CAST(T.{ksef['status']} AS NVARCHAR(64))" if "status" in ksef else "NULL")

        sql = f"""
            SELECT T.TrN_TrNID, T.TrN_NumerPelny, T.TrN_DataDok, T.TrN_DataWys, T.TrN_Termin,
                   T.TrN_RazemNetto, T.TrN_RazemVAT, T.TrN_RazemBrutto,
                   ISNULL(T.TrN_Korekta, 0),
                   FP.FPl_Nazwa,
                   K.Knt_KntId, K.Knt_Kod, K.Knt_Nip, K.Knt_Nazwa1, K.Knt_Nazwa2, K.Knt_Nazwa3,
                   K.Knt_Ulica, K.Knt_Miasto, K.Knt_KodPocztowy,
                   {', '.join(select_extra)}
            FROM CDN.TraNag T
            LEFT JOIN CDN.Kontrahenci K
                   ON K.Knt_KntId = T.TrN_PodID AND T.TrN_PodmiotTyp = 1
            LEFT JOIN CDN.FormyPlatnosci FP ON FP.FPl_FPlId = T.TrN_FPlId
            WHERE T.TrN_TypDokumentu = ?
              AND T.TrN_Bufor = 0
              AND ISNULL(T.TrN_Anulowany, 0) = 0
              AND T.TrN_DataDok BETWEEN ? AND ?
            ORDER BY T.TrN_DataDok DESC, T.TrN_TrNID DESC
        """
        cur = self.conn.cursor()
        try:
            cur.execute(sql, (TYP_FAKTURA_SPRZEDAZY, date_from, date_to))
        except Exception:
            # Nazwy kolumn adresowych kontrahenta roznia sie miedzy wersjami -
            # sprobuj wariantu minimalnego i pozwol diagnose ustalic reszte.
            logger.exception("Pelny SELECT faktur nie przeszedl - fallback minimalny")
            return self._list_sales_minimal(date_from, date_to)
        invoices = []
        for row in cur.fetchall():
            invoices.append(self._row_to_invoice(row))
        return invoices

    def _list_sales_minimal(self, date_from: date, date_to: date) -> list[SalesInvoice]:
        sql = """
            SELECT T.TrN_TrNID, T.TrN_NumerPelny, T.TrN_DataDok, T.TrN_DataWys, T.TrN_Termin,
                   T.TrN_RazemNetto, T.TrN_RazemVAT, T.TrN_RazemBrutto,
                   ISNULL(T.TrN_Korekta, 0),
                   NULL,
                   K.Knt_KntId, K.Knt_Kod, K.Knt_Nip, K.Knt_Nazwa1, NULL, NULL,
                   NULL, NULL, NULL,
                   'PLN', NULL, NULL, NULL, NULL
            FROM CDN.TraNag T
            LEFT JOIN CDN.Kontrahenci K
                   ON K.Knt_KntId = T.TrN_PodID AND T.TrN_PodmiotTyp = 1
            WHERE T.TrN_TypDokumentu = ?
              AND T.TrN_Bufor = 0
              AND ISNULL(T.TrN_Anulowany, 0) = 0
              AND T.TrN_DataDok BETWEEN ? AND ?
            ORDER BY T.TrN_DataDok DESC, T.TrN_TrNID DESC
        """
        cur = self.conn.cursor()
        cur.execute(sql, (TYP_FAKTURA_SPRZEDAZY, date_from, date_to))
        return [self._row_to_invoice(row) for row in cur.fetchall()]

    def _row_to_invoice(self, row) -> SalesInvoice:
        (
            trn_id, numer, data_dok, data_wys, termin,
            netto, vat, brutto, korekta, fpl_nazwa,
            knt_id, knt_kod, knt_nip, n1, n2, n3,
            ulica, miasto, kod_p,
            waluta, kurs_l, kurs_m, ksef_nr, ksef_status,
        ) = row
        buyer = None
        if knt_id is not None:
            nazwa = " ".join(p for p in (n1, n2, n3) if p)
            buyer = Contractor(
                knt_id=knt_id,
                kod=knt_kod or "",
                nip=knt_nip or "",
                nazwa=nazwa,
                ulica=ulica or "",
                miasto=miasto or "",
                kod_pocztowy=kod_p or "",
            )
        return SalesInvoice(
            trn_id=trn_id,
            numer_pelny=numer,
            data_dok=_as_date(data_dok),
            data_wys=_as_date(data_wys),
            termin=_as_date(termin),
            razem_netto=Decimal(str(netto or 0)),
            razem_vat=Decimal(str(vat or 0)),
            razem_brutto=Decimal(str(brutto or 0)),
            korekta=bool(korekta),
            waluta=(waluta or "PLN").strip() or "PLN",
            kurs=Decimal(str(kurs_l)) if kurs_l else None,
            kurs_l=Decimal(str(kurs_m)) if kurs_m else None,
            forma_platnosci=fpl_nazwa,
            buyer=buyer,
            optima_ksef_number=(ksef_nr or None),
            optima_ksef_status=(str(ksef_status) if ksef_status is not None else None),
        )

    def get_lines(self, trn_id: int) -> list[SalesLine]:
        sql = """
            SELECT TrE_TwrNazwa, TrE_Ilosc, TrE_JmZ, TrE_Cena, TrE_Stawka, TrE_Flaga,
                   TrE_WartoscNetto
            FROM CDN.TraElem
            WHERE TrE_TrNId = ?
            ORDER BY TrE_TrEId
        """
        cur = self.conn.cursor()
        cur.execute(sql, (trn_id,))
        lines = []
        for i, row in enumerate(cur.fetchall(), start=1):
            nazwa, ilosc, jm, cena, stawka, flaga, netto = row
            lines.append(
                SalesLine(
                    lp=i,
                    nazwa=nazwa or "",
                    ilosc=Decimal(str(ilosc or 0)),
                    jednostka=(jm or "szt.").strip(),
                    cena=Decimal(str(cena or 0)),
                    stawka=Decimal(str(stawka or 0)),
                    flaga=int(flaga or 0),
                    wartosc_netto=Decimal(str(netto or 0)),
                )
            )
        return lines

    def get_invoice(self, trn_id: int, date_hint_from: date, date_hint_to: date) -> SalesInvoice | None:
        for inv in self.list_sales(date_hint_from, date_hint_to):
            if inv.trn_id == trn_id:
                inv.lines = self.get_lines(trn_id)
                return inv
        return None


def _as_date(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
