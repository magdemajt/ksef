"""Odczyt faktur sprzedazy z bazy Optimy (login read-only).

Nazwy kolumn ustalone raportem diagnose z Optimy 2026 (CDN_ENSIGNER_TECH, 19.10.2026).
Kolumny opcjonalne (KSeF, MPP, waluta) reader sprawdza w INFORMATION_SCHEMA i buduje
SELECT tylko z faktycznie istniejacych - starsza Optima bez nich nadal sie odczyta.

Dane nabywcy i daty pochodza z naglowka dokumentu (TrN_Pod*, TrN_DataWys/DataOpe),
nie z karty kontrahenta: faktura musi miec dane z chwili wystawienia.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from app.optima.models import Contractor, SalesInvoice, SalesLine

logger = logging.getLogger(__name__)

TYP_FAKTURA_SPRZEDAZY = 302

# Kolumny opcjonalne na TraNag - obecnosc weryfikowana w INFORMATION_SCHEMA
CURRENCY_CANDIDATES = ("TrN_Waluta",)
RATE_NUM_CANDIDATES = ("TrN_KursL",)
RATE_DEN_CANDIDATES = ("TrN_KursM",)
KSEF_NUMBER_CANDIDATES = ("TrN_NrKSeF",)
KSEF_STATUS_CANDIDATES = ("TrN_KSeFStatus",)
# numer referencyjny nadawany przy wysylce z modulu KSeF Optimy (przed nadaniem numeru KSeF)
KSEF_REF_CANDIDATES = ("TrN_KSeFReferenceNumber",)
NO_KSEF_CANDIDATES = ("TrN_NieWysylajDokumentuDoKSeF",)
# mechanizm podzielonej platnosci ustawiony przez Optime na dokumencie (Adnotacje/P_18A)
SPLIT_PAY_CANDIDATES = ("TrN_SplitPay",)
# TraElem: jednostka podstawowa pasuje do TrE_Ilosc (TrE_JmZ idzie w parze z TrE_IloscJM)
UNIT_CANDIDATES = ("TrE_Jm", "TrE_JmZ")
# pozycja z zalacznika nr 15 (FaWiersz/P_12_Zal_15)
LINE_SPLIT_PAY_CANDIDATES = ("TrE_SplitPay",)


class SalesReader:
    def __init__(self, conn):
        self.conn = conn
        self._tranag_cols: set[str] | None = None
        self._traelem_cols: set[str] | None = None

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

    def traelem_columns(self) -> set[str]:
        if self._traelem_cols is None:
            self._traelem_cols = self._columns("TraElem")
        return self._traelem_cols

    def _pick(self, candidates: tuple[str, ...], cols: set[str] | None = None) -> str | None:
        # SQL Server (domyslna collation) nie rozroznia wielkosci liter w nazwach kolumn
        by_lower = {c.lower(): c for c in (self.tranag_columns() if cols is None else cols)}
        for cand in candidates:
            if cand.lower() in by_lower:
                return by_lower[cand.lower()]
        return None

    def _tranag_sql(self, candidates: tuple[str, ...], default: str = "NULL") -> str:
        col = self._pick(candidates)
        return f"T.{col}" if col else default

    # -- odczyt --

    def list_sales(self, date_from: date, date_to: date) -> list[SalesInvoice]:
        ksef_status = self._pick(KSEF_STATUS_CANDIDATES)
        extras = [
            self._tranag_sql(CURRENCY_CANDIDATES, "'PLN'"),
            self._tranag_sql(RATE_NUM_CANDIDATES),
            self._tranag_sql(RATE_DEN_CANDIDATES),
            self._tranag_sql(KSEF_NUMBER_CANDIDATES),
            f"CAST(T.{ksef_status} AS NVARCHAR(64))" if ksef_status else "NULL",
            self._tranag_sql(KSEF_REF_CANDIDATES),
            self._tranag_sql(NO_KSEF_CANDIDATES, "0"),
            self._tranag_sql(SPLIT_PAY_CANDIDATES, "0"),
        ]
        sql = f"""
            SELECT T.TrN_TrNID, T.TrN_NumerPelny, T.TrN_DataWys, T.TrN_DataOpe, T.TrN_Termin,
                   T.TrN_RazemNetto, T.TrN_RazemVAT, T.TrN_RazemBrutto,
                   ISNULL(T.TrN_Korekta, 0),
                   FP.FPl_Nazwa,
                   K.Knt_KntId, K.Knt_Kod, T.TrN_PodNipKraj, T.TrN_PodNipE,
                   T.TrN_PodNazwa1, T.TrN_PodNazwa2, T.TrN_PodNazwa3,
                   T.TrN_PodUlica, T.TrN_PodNrDomu, T.TrN_PodNrLokalu,
                   T.TrN_PodMiasto, T.TrN_PodKodPocztowy,
                   ISNULL(T.TrN_Bufor, 0),
                   {', '.join(extras)}
            FROM CDN.TraNag T
            LEFT JOIN CDN.Kontrahenci K
                   ON K.Knt_KntId = T.TrN_PodID AND T.TrN_PodmiotTyp = 1
            LEFT JOIN CDN.FormyPlatnosci FP ON FP.FPl_FPlId = T.TrN_FPlId
            WHERE T.TrN_TypDokumentu = ?
              AND ISNULL(T.TrN_Anulowany, 0) = 0
              AND T.TrN_DataWys BETWEEN ? AND ?
            ORDER BY T.TrN_DataWys DESC, T.TrN_TrNID DESC
        """
        cur = self.conn.cursor()
        try:
            cur.execute(sql, (TYP_FAKTURA_SPRZEDAZY, date_from, date_to))
        except Exception:
            # Inna wersja Optimy bez kolumn snapshotu nabywcy - lista dziala dalej,
            # a mapper zablokuje wysylke (brak adresu nabywcy).
            logger.exception("Pelny SELECT faktur nie przeszedl - fallback minimalny")
            return self._list_sales_minimal(date_from, date_to)
        return [self._row_to_invoice(row) for row in cur.fetchall()]

    def _list_sales_minimal(self, date_from: date, date_to: date) -> list[SalesInvoice]:
        sql = """
            SELECT T.TrN_TrNID, T.TrN_NumerPelny, T.TrN_DataDok, NULL, T.TrN_Termin,
                   T.TrN_RazemNetto, T.TrN_RazemVAT, T.TrN_RazemBrutto,
                   ISNULL(T.TrN_Korekta, 0),
                   NULL,
                   K.Knt_KntId, K.Knt_Kod, NULL, K.Knt_Nip,
                   K.Knt_Nazwa1, NULL, NULL,
                   NULL, NULL, NULL,
                   NULL, NULL,
                   ISNULL(T.TrN_Bufor, 0),
                   'PLN', NULL, NULL, NULL, NULL, NULL, 0, 0
            FROM CDN.TraNag T
            LEFT JOIN CDN.Kontrahenci K
                   ON K.Knt_KntId = T.TrN_PodID AND T.TrN_PodmiotTyp = 1
            WHERE T.TrN_TypDokumentu = ?
              AND ISNULL(T.TrN_Anulowany, 0) = 0
              AND T.TrN_DataDok BETWEEN ? AND ?
            ORDER BY T.TrN_DataDok DESC, T.TrN_TrNID DESC
        """
        cur = self.conn.cursor()
        cur.execute(sql, (TYP_FAKTURA_SPRZEDAZY, date_from, date_to))
        return [self._row_to_invoice(row) for row in cur.fetchall()]

    def _row_to_invoice(self, row) -> SalesInvoice:
        (
            trn_id, numer, data_wys, data_ope, termin,
            netto, vat, brutto, korekta, fpl_nazwa,
            knt_id, knt_kod, nip_kraj, nip, n1, n2, n3,
            ulica, nr_domu, nr_lokalu, miasto, kod_p, bufor,
            waluta, kurs_l, kurs_m, ksef_nr, ksef_status, ksef_ref, no_ksef, split_pay,
        ) = row
        buyer = None
        if knt_id is not None:
            buyer = Contractor(
                knt_id=knt_id,
                kod=_s(knt_kod),
                nip=_s(nip),
                nip_kraj=_s(nip_kraj).upper(),
                nazwa=" ".join(p for p in (_s(n1), _s(n2), _s(n3)) if p),
                ulica=_s(ulica),
                nr_domu=_s(nr_domu),
                nr_lokalu=_s(nr_lokalu),
                miasto=_s(miasto),
                kod_pocztowy=_s(kod_p),
            )
        return SalesInvoice(
            trn_id=trn_id,
            numer_pelny=numer,
            data_wystawienia=_as_date(data_wys),
            data_sprzedazy=_as_date(data_ope),
            termin=_as_date(termin),
            razem_netto=Decimal(str(netto or 0)),
            razem_vat=Decimal(str(vat or 0)),
            razem_brutto=Decimal(str(brutto or 0)),
            korekta=bool(korekta),
            waluta=_s(waluta) or "PLN",
            kurs=Decimal(str(kurs_l)) if kurs_l else None,
            kurs_l=Decimal(str(kurs_m)) if kurs_m else None,
            forma_platnosci=fpl_nazwa,
            buyer=buyer,
            bufor=bool(bufor),
            # TrN_NrKSeF jest NOT NULL - brak numeru to pusty string
            optima_ksef_number=_s(ksef_nr) or None,
            optima_ksef_status=(str(ksef_status) if ksef_status is not None else None),
            optima_ksef_ref=_s(ksef_ref) or None,
            optima_nie_wysylaj=bool(no_ksef),
            split_payment=bool(split_pay),
        )

    def get_lines(self, trn_id: int) -> list[SalesLine]:
        # Cena jednostkowa netto (P_9A) liczona z wartosci netto pozycji, nie z kolumny
        # cenowej: TrE_Cena nie istnieje w Optimie 2026, a kolumny cenowe TraElem bywaja
        # brutto (faktura liczona od brutto) lub sprzed rabatu - P_9A musi byc spojne z P_11.
        cols = self.traelem_columns()
        jm_col = self._pick(UNIT_CANDIDATES, cols)
        split_col = self._pick(LINE_SPLIT_PAY_CANDIDATES, cols)
        jm_sql = jm_col or "NULL"
        split_sql = f"ISNULL({split_col}, 0)" if split_col else "0"
        sql = f"""
            SELECT TrE_TwrNazwa, TrE_Ilosc, {jm_sql}, TrE_Stawka, TrE_Flaga,
                   TrE_WartoscNetto, {split_sql}
            FROM CDN.TraElem
            WHERE TrE_TrNId = ?
            ORDER BY TrE_Lp, TrE_TrEId
        """
        cur = self.conn.cursor()
        cur.execute(sql, (trn_id,))
        lines = []
        for i, row in enumerate(cur.fetchall(), start=1):
            nazwa, ilosc, jm, stawka, flaga, netto, split_pay = row
            ilosc = Decimal(str(ilosc or 0))
            netto = Decimal(str(netto or 0))
            cena = (netto / ilosc).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) if ilosc else netto
            lines.append(
                SalesLine(
                    lp=i,
                    nazwa=_s(nazwa),
                    ilosc=ilosc,
                    jednostka=_s(jm) or "szt.",
                    cena=cena,
                    stawka=Decimal(str(stawka or 0)),
                    flaga=int(flaga or 0),
                    wartosc_netto=netto,
                    zal15=bool(split_pay),
                )
            )
        return lines

    def get_invoice(self, trn_id: int, date_hint_from: date, date_hint_to: date) -> SalesInvoice | None:
        for inv in self.list_sales(date_hint_from, date_hint_to):
            if inv.trn_id == trn_id:
                inv.lines = self.get_lines(trn_id)
                return inv
        return None


def _s(value) -> str:
    """Tekst z Optimy: kolumny nvarchar NOT NULL trzymaja '' zamiast NULL."""
    return (value or "").strip()


def _as_date(value) -> date | None:
    if value is None:
        return None
    # kolumny dat Optimy sa typu DATETIME; datetime jest podklasa date, wiec najpierw
    # obcinamy czas - inaczej isoformat() daje '...T00:00:00' (niepoprawne TData w FA(3))
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])
