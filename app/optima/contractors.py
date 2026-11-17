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
