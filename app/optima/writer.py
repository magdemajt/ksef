"""Zapis faktur zakupu do rejestru VAT Optimy.

Dwie sciezki:
    1. export_as_xml()  - XML "pracy rozproszonej" do recznego importu w Optimie
                          (bezpieczny fallback, dostepny zawsze),
    2. export_direct()  - bezposredni INSERT VatNag+VatTab+BnkZdarzenia w transakcji;
                          odblokowany dopiero gdy writes_enabled=true ORAZ istnieje
                          zweryfikowany insert_spec.json (workflow snapshot/diff, M5).
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from lxml import etree

from app.config import CompanyConfig, LpNumbering
from app.fa3.parser import PurchaseInvoice
from app.optima.contractors import find_by_nip
from app.optima.models import Contractor

logger = logging.getLogger(__name__)


class WriteBlockedError(RuntimeError):
    pass


class ContractorMissingError(RuntimeError):
    def __init__(self, nip: str, name: str):
        super().__init__(
            f"Brak kontrahenta o NIP {nip} ({name}) w Optimie - zaloz kartoteke i ponow eksport"
        )
        self.nip = nip
        self.name = name


class VerificationError(RuntimeError):
    pass


# -- sciezka 1: XML pracy rozproszonej --


def export_as_xml(invoices: list[PurchaseInvoice], register: str, target_db_id: str = "") -> bytes:
    """Generuje plik XML dla Narzedzia -> Praca rozproszona -> Import (Rejestr zakupow VAT)."""
    root = etree.Element("ROOT")
    for inv in invoices:
        rej = etree.SubElement(root, "REJESTR_ZAKUPU_VAT")
        _t(rej, "ID_BAZY_ZRODLO", "KSEF-BRIDGE")
        if target_db_id:
            _t(rej, "ID_BAZY_DOCELOWA", target_db_id)
        _t(rej, "REJESTR", register)
        _t(rej, "DATA_WYSTAWIENIA", inv.issue_date)
        _t(rej, "DATA_WPLYWU", inv.issue_date)
        _t(rej, "NUMER_FAKTURY", inv.invoice_number)
        _t(rej, "KOREKTA", "Nie")
        kontrahent = etree.SubElement(rej, "KONTRAHENT")
        _t(kontrahent, "NIP", inv.seller_nip)
        _t(kontrahent, "NAZWA1", inv.seller_name[:50])
        if inv.seller_address:
            _t(kontrahent, "ULICA", inv.seller_address[:50])
        kwoty = etree.SubElement(rej, "KWOTY")
        for line in inv.vat_lines:
            kwota = etree.SubElement(kwoty, "KWOTA")
            if line.stawka is not None:
                _t(kwota, "STAWKA_VAT", f"{line.stawka:.2f}")
                _t(kwota, "STATUS_STAWKI_VAT", "opodatkowana")
            else:
                _t(kwota, "STAWKA_VAT", "0.00")
                _t(
                    kwota,
                    "STATUS_STAWKI_VAT",
                    "zwolniona" if line.stawka_symbol == "zw" else "nie podlega",
                )
            _t(kwota, "WARTOSC_NETTO", f"{line.netto:.2f}")
            _t(kwota, "KWOTA_VAT", f"{line.vat:.2f}")
        if inv.payment_due:
            _t(rej, "TERMIN_PLATNOSCI", inv.payment_due)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _t(parent, tag, text):
    el = etree.SubElement(parent, tag)
    el.text = str(text)
    return el


# -- sciezka 2: bezposredni INSERT --


class PurchaseWriter:
    def __init__(self, conn, company: CompanyConfig, insert_spec_path: Path):
        self.conn = conn
        self.company = company
        self.insert_spec_path = insert_spec_path
        self._spec: dict | None = None

    def spec(self) -> dict:
        if self._spec is None:
            if not self.insert_spec_path.exists():
                raise WriteBlockedError(
                    f"Brak {self.insert_spec_path} - najpierw wykonaj workflow snapshot/diff "
                    f"(ksef-bridge snapshot/diff) na kopii bazy i zatwierdz specyfikacje INSERT-ow"
                )
            self._spec = json.loads(self.insert_spec_path.read_text(encoding="utf-8"))
        return self._spec

    def ensure_enabled(self) -> None:
        if not self.company.writes_enabled:
            raise WriteBlockedError(
                "writes_enabled=false dla tej firmy - uzyj eksportu XML pracy rozproszonej "
                "albo wlacz zapisy po zakonczeniu M5"
            )
        self.spec()

    def export_direct(self, inv: PurchaseInvoice, acquisition_date: date) -> int:
        """Wstawia fakture do rejestru VAT zakupu. Zwraca VaN_VaNID.

        Wzorzec dat jak w natywnym transferze KSeF->rejestr w Optimie:
        data wplywu = data przyjecia w KSeF, data wystawienia = P_1 faktury.
        """
        self.ensure_enabled()
        contractor = find_by_nip(self.conn, inv.seller_nip)
        if contractor is None:
            raise ContractorMissingError(inv.seller_nip, inv.seller_name)

        issue = date.fromisoformat(inv.issue_date)
        rokmies = acquisition_date.year * 100 + acquisition_date.month
        cur = self.conn.cursor()
        try:
            van_id = self._insert_vatnag(cur, inv, contractor, issue, acquisition_date, rokmies)
            self._insert_vattab(cur, van_id, inv)
            self._insert_platnosc(cur, van_id, inv, contractor)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._verify(van_id, inv)
        return van_id

    def _next_lp(self, cur, rokmies: int) -> int:
        if self.company.lp_numbering == LpNumbering.MONTH:
            period_pred = "VaN_RokMies = ?"
            period_val = rokmies
        else:
            period_pred = "VaN_RokMies / 100 = ?"
            period_val = rokmies // 100
        cur.execute(
            f"SELECT ISNULL(MAX(VaN_Lp),0) + 1 FROM CDN.VatNag WITH (UPDLOCK, HOLDLOCK) "
            f"WHERE VaN_Typ = 1 AND VaN_Rejestr = ? AND {period_pred}",
            (self.company.purchase_register, period_val),
        )
        return cur.fetchone()[0]

    def _insert_vatnag(
        self, cur, inv: PurchaseInvoice, contractor: Contractor,
        issue: date, acq: date, rokmies: int,
    ) -> int:
        spec_cols: dict = self.spec().get("VatNag", {})
        lp = self._next_lp(cur, rokmies)
        values: dict[str, object] = {
            "VaN_Typ": 1,
            "VaN_Rejestr": self.company.purchase_register,
            "VaN_Lp": lp,
            "VaN_Dokument": inv.invoice_number,
            "VaN_DataZap": acq,
            "VaN_DataWys": issue,
            "VaN_DataOpe": issue,
            "VaN_RokMies": rokmies,
            "VaN_DeklRokMies": rokmies,
            "VaN_Termin": inv.payment_due or acq,
            "VaN_PodmiotTyp": 1,
            "VaN_PodID": contractor.knt_id,
            "VaN_KntNazwa1": inv.seller_name[:200],
            "VaN_KntNipE": inv.seller_nip,
            "VaN_Waluta": inv.currency,
            "VaN_RazemNetto": inv.total_net,
            "VaN_RazemVAT": inv.total_vat,
            "VaN_RazemBrutto": inv.total_gross,
        }
        # kolumny obowiazkowe wykryte przez snapshot/diff, ktorych nie wyliczamy z faktury
        for col, literal in spec_cols.get("literals", {}).items():
            values.setdefault(col, literal)
        cols = list(values)
        placeholders = ",".join("?" * len(cols))
        cur.execute(
            f"INSERT INTO CDN.VatNag ({','.join(cols)}) OUTPUT INSERTED.VaN_VaNID "
            f"VALUES ({placeholders})",
            tuple(values[c] for c in cols),
        )
        return cur.fetchone()[0]

    def _insert_vattab(self, cur, van_id: int, inv: PurchaseInvoice) -> None:
        spec_cols: dict = self.spec().get("VatTab", {})
        for i, line in enumerate(inv.vat_lines, start=1):
            values: dict[str, object] = {
                "VaT_VaNID": van_id,
                "VaT_Numer": i,
                "VaT_Stawka": line.stawka if line.stawka is not None else Decimal(0),
                "VaT_StawkaSymbol": line.stawka_symbol,
                "VaT_Netto": line.netto,
                "VaT_VATWal": line.vat,
                # domysly konfigurowalne w insert_spec (RodzajZakupu: towary, Odliczenia: Tak)
            }
            for col, literal in spec_cols.get("literals", {}).items():
                values.setdefault(col, literal)
            cols = list(values)
            placeholders = ",".join("?" * len(cols))
            cur.execute(
                f"INSERT INTO CDN.VatTab ({','.join(cols)}) VALUES ({placeholders})",
                tuple(values[c] for c in cols),
            )

    def _insert_platnosc(self, cur, van_id: int, inv: PurchaseInvoice, contractor: Contractor):
        spec_cols: dict = self.spec().get("BnkZdarzenia", {})
        values: dict[str, object] = {
            "BZd_DokumentTyp": 2,
            "BZd_DokumentId": van_id,
            "BZd_PodmiotTyp": 1,
            "BZd_PodmiotID": contractor.knt_id,
            "BZd_Kwota": inv.total_gross,
            "BZd_Termin": inv.payment_due or datetime.now(UTC).date(),
        }
        for col, literal in spec_cols.get("literals", {}).items():
            values.setdefault(col, literal)
        cols = list(values)
        placeholders = ",".join("?" * len(cols))
        cur.execute(
            f"INSERT INTO CDN.BnkZdarzenia ({','.join(cols)}) VALUES ({placeholders})",
            tuple(values[c] for c in cols),
        )

    def _verify(self, van_id: int, inv: PurchaseInvoice) -> None:
        """Po commicie: re-SELECT i porownanie sum co do grosza."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT VaN_RazemNetto, VaN_RazemVAT, VaN_RazemBrutto FROM CDN.VatNag "
            "WHERE VaN_VaNID = ?",
            (van_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise VerificationError(f"Wpis VaN_VaNID={van_id} zniknal po commicie")
        netto, vat, brutto = (Decimal(str(v or 0)) for v in row)
        cur.execute("SELECT ISNULL(SUM(VaT_Netto),0) FROM CDN.VatTab WHERE VaT_VaNID = ?", (van_id,))
        tab_netto = Decimal(str(cur.fetchone()[0] or 0))
        problems = []
        if abs(netto - inv.total_net) > Decimal("0.01"):
            problems.append(f"netto naglowka {netto} != faktura {inv.total_net}")
        if abs(vat - inv.total_vat) > Decimal("0.01"):
            problems.append(f"VAT naglowka {vat} != faktura {inv.total_vat}")
        if abs(brutto - inv.total_gross) > Decimal("0.01"):
            problems.append(f"brutto naglowka {brutto} != faktura {inv.total_gross}")
        if abs(tab_netto - inv.total_net) > Decimal("0.01"):
            problems.append(f"suma VatTab {tab_netto} != faktura {inv.total_net}")
        if problems:
            raise VerificationError(
                f"Weryfikacja wpisu VaN_VaNID={van_id} nie przeszla: {'; '.join(problems)}. "
                f"Wpis NIE zostal usuniety - wymaga recznej kontroli w Optimie."
            )
