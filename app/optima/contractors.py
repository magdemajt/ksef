"""Dopasowanie kontrahenta Optimy po znormalizowanym NIP."""

from __future__ import annotations

from app.optima.models import Contractor


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
    )


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
