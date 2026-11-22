"""Dopasowanie kontrahenta Optimy po znormalizowanym NIP."""

from __future__ import annotations

import logging
import re

from app.optima.models import Contractor

logger = logging.getLogger(__name__)
_GUID_RE = re.compile(r"^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}$")


def normalize_nip(nip: str) -> str:
    digits = "".join(ch for ch in (nip or "") if ch.isdigit())
    return digits


def find_by_nip(conn, nip: str) -> Contractor | None:
    """Szuka aktywnego kontrahenta o podanym NIP (ignoruje kreski/spacje/prefiks PL)."""
    wanted = normalize_nip(nip)
    if len(wanted) != 10:
        return None
    cur = conn.cursor()
    cur.execute(
        """
        SELECT Knt_KntId, Knt_Kod, Knt_Nip, Knt_Nazwa1, Knt_Nazwa2, Knt_Nazwa3
        FROM CDN.Kontrahenci
        WHERE REPLACE(REPLACE(REPLACE(Knt_Nip,'-',''),' ',''),'PL','') = ?
        """,
        (wanted,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    knt_id, kod, knt_nip, n1, n2, n3 = row
    return Contractor(
        knt_id=knt_id,
        kod=kod or "",
        nip=knt_nip or "",
        nazwa=" ".join(p for p in (n1, n2, n3) if p),
        guid=_row_guid(conn, "SELECT * FROM CDN.Kontrahenci WHERE Knt_KntId = ?", (knt_id,)),
    )


def _guid_text(value) -> str:
    text = str(value or "").strip().strip("{}").upper()
    return text if _GUID_RE.match(text) else ""


def _row_guid(conn, query: str, params: tuple) -> str:
    """GUID wiersza - kolumna rozpoznawana po nazwie (*GUID), bo nie jest udokumentowana."""
    try:
        cur = conn.cursor()
        cur.execute(query, params)
        cols = [d[0] for d in cur.description]
        row = cur.fetchone()
    except Exception:
        logger.exception("Odczyt GUID nie powiodl sie")
        return ""
    if row is None:
        return ""
    for col, value in zip(cols, row):
        if col.lower().endswith("guid") and _guid_text(value):
            return _guid_text(value)
    return ""


def payment_form_guids(conn) -> dict[str, str]:
    """Nazwa formy platnosci (male litery) -> GUID; do pliku pracy rozproszonej."""
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM CDN.FormyPlatnosci")
        cols = [d[0] for d in cur.description]
        rows = cur.fetchall()
    except Exception:
        logger.exception("Odczyt form platnosci nie powiodl sie")
        return {}
    name_col = next((c for c in cols if c.lower().split("_", 1)[-1] == "nazwa"), None)
    guid_col = next((c for c in cols if c.lower().endswith("guid")), None)
    if not name_col or not guid_col:
        return {}
    found = {}
    for row in rows:
        values = dict(zip(cols, row))
        name, guid = (values[name_col] or "").strip().lower(), _guid_text(values[guid_col])
        if name and guid:
            found.setdefault(name, guid)
    return found


def ksef_number_known(conn, ksef_number: str) -> bool:
    """Czy Optima juz pobrala te fakture (tabela CDN.DokumentyKSeF)?"""
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT COUNT(*) FROM CDN.DokumentyKSeF WHERE DKF_NumerKSeF = ?",
            (ksef_number,),
        )
    except Exception:
        # starsza Optima bez tabeli DokumentyKSeF
        return False
    return (cur.fetchone() or [0])[0] > 0


def vat_register_has(conn, seller_nip: str, invoice_number: str) -> bool:
    """Czy w rejestrze VAT zakupu jest juz wpis (NIP dostawcy, numer faktury)?"""
    wanted = normalize_nip(seller_nip)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*) FROM CDN.VatNag
        WHERE VaN_Typ = 1 AND VaN_Dokument = ?
          AND REPLACE(REPLACE(REPLACE(ISNULL(VaN_KntNipE,''),'-',''),' ',''),'PL','') = ?
        """,
        (invoice_number, wanted),
    )
    return (cur.fetchone() or [0])[0] > 0


_IN_CHUNK = 500  # SQL Server przyjmuje do 2100 parametrow w zapytaniu


def _chunks(values: list[str]):
    for start in range(0, len(values), _IN_CHUNK):
        yield values[start:start + _IN_CHUNK]


def _doc_key(invoice_number: str, nip: str) -> tuple[str, str]:
    # jak porownanie w SQL Server: bez wielkosci liter i koncowych spacji
    return (invoice_number or "").strip().casefold(), normalize_nip(nip)


def find_vat_entries(conn, invoices: list[tuple[str, str, str]]) -> dict[str, int]:
    """Wpisy rejestru VAT zakupu dla wielu faktur naraz: {numer KSeF: VaN_VaNID}.

    invoices: (numer KSeF, NIP dostawcy, numer faktury). Te same reguly co find_vat_entry,
    ale w 2 zapytaniach na cala liste zamiast 2 na fakture.
    """
    found: dict[str, int] = {}
    ksef_numbers = [ksef for ksef, _, _ in invoices if ksef]
    try:
        for chunk in _chunks(ksef_numbers):
            cur = conn.cursor()
            cur.execute(
                "SELECT VaN_NrKSeF, VaN_VaNID FROM CDN.VatNag WHERE VaN_Typ = 1 "
                f"AND VaN_NrKSeF IN ({','.join('?' * len(chunk))})",
                tuple(chunk),
            )
            for ksef, van_id in cur.fetchall():
                found.setdefault((ksef or "").strip(), int(van_id))
    except Exception:
        # starsza Optima bez VaN_NrKSeF - zostaje dopasowanie po NIP i numerze
        found = {}

    by_doc: dict[tuple[str, str], list[str]] = {}
    for ksef, nip, number in invoices:
        key = _doc_key(number, nip)
        if ksef not in found and all(key):
            by_doc.setdefault(key, []).append(ksef)
    numbers = sorted({number.strip() for _, _, number in invoices if (number or "").strip()})
    for chunk in _chunks(numbers) if by_doc else ():
        cur = conn.cursor()
        cur.execute(
            "SELECT VaN_Dokument, VaN_KntNipE, VaN_VaNID FROM CDN.VatNag WHERE VaN_Typ = 1 "
            f"AND VaN_Dokument IN ({','.join('?' * len(chunk))})",
            tuple(chunk),
        )
        for number, nip, van_id in cur.fetchall():
            for ksef in by_doc.get(_doc_key(number, nip or ""), ()):
                found.setdefault(ksef, int(van_id))
    return found


def ksef_numbers_known(conn, ksef_numbers: list[str]) -> set[str]:
    """Ktore z numerow KSeF Optima juz pobrala (CDN.DokumentyKSeF) - jedno zapytanie na liste."""
    known: set[str] = set()
    try:
        for chunk in _chunks([n for n in ksef_numbers if n]):
            cur = conn.cursor()
            cur.execute(
                "SELECT DKF_NumerKSeF FROM CDN.DokumentyKSeF "
                f"WHERE DKF_NumerKSeF IN ({','.join('?' * len(chunk))})",
                tuple(chunk),
            )
            known.update((row[0] or "").strip() for row in cur.fetchall())
    except Exception:
        # starsza Optima bez tabeli DokumentyKSeF
        return set()
    return known
