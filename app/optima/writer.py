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
import re
import textwrap
import uuid
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
#
# Format odwzorowany z eksportu Optimy 2026 (Narzedzia -> Praca rozproszona -> Eksport,
# sekcja REJESTRY_ZAKUPU_VAT): kolejnosc elementow, pola opcjonalne i wartosci domyslne
# jak we wpisach, ktore natywny modul KSeF Optimy sam przenosi do rejestru.

OFFLINE_NS = "http://www.comarch.pl/cdn/optima/offline"
OFFLINE_SOURCE_ID = "KSEF"  # BAZA_ZRD_ID - identyfikator tego programu jako zrodla
_GUID_NAMESPACE = uuid.UUID("6f1d3c52-6a0e-4d0a-9d0e-4b5345462d42")
_POSTCODE_CITY_RE = re.compile(r"^(\d{2}-\d{3})\s+(.+)$")
_KSEF_DATE_RE = re.compile(r"^\d{10}-(\d{4})(\d{2})(\d{2})-")
CORRECTION_TYPES = ("KOR", "KOR_ZAL", "KOR_ROZ")


class UnsupportedInvoiceError(ValueError):
    pass


def _guid(*parts: str) -> str:
    """Stabilny GUID - ponowny import tego samego pliku nie tworzy drugiego wpisu."""
    return str(uuid.uuid5(_GUID_NAMESPACE, "|".join(parts))).upper()


def _num(value: Decimal) -> str:
    """Kwota w zapisie Optimy: kropka dziesietna, bez zer koncowych (106, 24.38)."""
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def ksef_received_date(ksef_number: str | None, fallback: str) -> str:
    """Data przyjecia faktury w KSeF - zapisana w numerze KSeF (NIP-RRRRMMDD-...)."""
    match = _KSEF_DATE_RE.match(ksef_number or "")
    return "-".join(match.groups()) if match else fallback


def _yes_no(flag: bool) -> str:
    return "Tak" if flag else "Nie"


def _name_parts(name: str) -> list[str]:
    parts = textwrap.wrap(name.strip(), 50)[:3]
    return parts + [""] * (3 - len(parts))


def _t(parent, tag, text=""):
    el = etree.SubElement(parent, f"{{{OFFLINE_NS}}}{tag}")
    el.text = str(text)
    return el


def export_as_xml(
    invoices: list[PurchaseInvoice],
    register: str,
    target_db_id: str = "",
    *,
    contractors: dict[str, Contractor] | None = None,
    payment_form_ids: dict[str, str] | None = None,
) -> bytes:
    """Plik XML dla Narzedzia -> Praca rozproszona -> Import (rejestr zakupow VAT).

    contractors: znormalizowany NIP -> kartoteka Optimy (akronim i GUID); bez dopasowania
    akronimem jest NIP, a Optima zaklada kartoteke z danych faktury.
    payment_form_ids: nazwa formy platnosci (male litery) -> GUID w bazie Optimy.
    """
    contractors = contractors or {}
    payment_form_ids = payment_form_ids or {}
    root = etree.Element(f"{{{OFFLINE_NS}}}ROOT", nsmap={None: OFFLINE_NS})
    section = _t(root, "REJESTRY_ZAKUPU_VAT")
    section.text = None
    _t(section, "WERSJA", "2.00")
    _t(section, "BAZA_ZRD_ID", OFFLINE_SOURCE_ID)
    _t(section, "BAZA_DOC_ID", target_db_id)
    for inv in invoices:
        _offline_record(section, inv, register, contractors, payment_form_ids)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)


def _offline_record(section, inv, register, contractors, payment_form_ids) -> None:
    if (inv.currency or "PLN").upper() != "PLN":
        raise UnsupportedInvoiceError(
            f"Faktura {inv.invoice_number} w walucie {inv.currency} - eksport XML obsluguje tylko PLN"
        )
    nip = "".join(ch for ch in inv.seller_nip if ch.isdigit())
    key = inv.ksef_number or f"{nip}|{inv.invoice_number}"
    known = contractors.get(nip)
    akronim = known.kod if known else nip
    podmiot_id = (known.guid if known and known.guid else "") or _guid("kontrahent", nip)

    issue = inv.issue_date
    sale = inv.sale_date or issue
    received = ksef_received_date(inv.ksef_number, issue)
    deduction = max(sale, received)  # prawo do odliczenia: nie wczesniej niz wplyw faktury
    due = inv.payment_due or issue
    forma = "gotówka" if inv.payment_form == "1" else "przelew"
    forma_id = payment_form_ids.get(forma, "")
    account = "".join(ch for ch in (inv.bank_account or "") if ch.isdigit())
    account = account if len(account) == 26 else ""
    names = _name_parts(inv.seller_name)
    postcode, city = "", inv.seller_address_l2.strip()
    match = _POSTCODE_CITY_RE.match(city)
    if match:
        postcode, city = match.groups()

    rec = _t(section, "REJESTR_ZAKUPU_VAT")
    rec.text = None
    _t(rec, "ID_ZRODLA", _guid("rejestr", key))
    _t(rec, "MODUL", "Rejestr Vat")
    _t(rec, "TYP", "Rejestr zakupu")
    _t(rec, "REJESTR", register)
    _t(rec, "DATA_WYSTAWIENIA", issue)
    _t(rec, "DATA_ZAKUPU", sale)
    _t(rec, "DATA_WPLYWU", received)
    _t(rec, "TERMIN", due)
    _t(rec, "DATA_DATAOBOWIAZKUPODATKOWEGO", sale)
    _t(rec, "DATA_DATAPRAWAODLICZENIA", deduction)
    _t(rec, "NUMER", inv.invoice_number)
    is_correction = inv.invoice_type in CORRECTION_TYPES
    _t(rec, "KOREKTA", _yes_no(is_correction))
    _t(rec, "KOREKTA_NUMER", (inv.corrected_number or "") if is_correction else "")
    _t(rec, "WEWNETRZNA", "Nie")
    _t(rec, "METODA_KASOWA", _yes_no(inv.cash_method))
    _t(rec, "FISKALNA", "Nie")
    _t(rec, "DETALICZNA", "Nie")
    _t(rec, "EKSPORT", "nie")
    _t(rec, "FINALNY", "Nie")
    _t(rec, "PODATNIK_CZYNNY", "Tak")
    _t(rec, "IDENTYFIKATOR_KSIEGOWY")
    _t(rec, "TYP_PODMIOTU", "kontrahent")
    _t(rec, "PODMIOT", akronim)
    _t(rec, "PODMIOT_ID", podmiot_id)
    _t(rec, "PODMIOT_NIP", nip)
    _t(rec, "NAZWA1", names[0])
    _t(rec, "NAZWA2", names[1])
    _t(rec, "NAZWA3", names[2])
    _t(rec, "NIP_KRAJ")
    _t(rec, "NIP", nip)
    _t(rec, "KRAJ", "Polska")
    _t(rec, "WOJEWODZTWO")
    _t(rec, "POWIAT")
    _t(rec, "GMINA")
    _t(rec, "ULICA", inv.seller_address_l1.strip()[:40])
    _t(rec, "NR_DOMU")
    _t(rec, "NR_LOKALU")
    _t(rec, "MIASTO", city[:40])
    _t(rec, "KOD_POCZTOWY", postcode)
    _t(rec, "POCZTA")
    _t(rec, "DODATKOWE")
    _t(rec, "PESEL")
    _t(rec, "ROLNIK", "Nie")
    _t(rec, "TYP_PLATNIKA", "kontrahent")
    _t(rec, "PLATNIK", akronim)
    _t(rec, "PLATNIK_ID", podmiot_id)
    _t(rec, "PLATNIK_NIP", nip)
    if account:
        _t(rec, "PLATNIK_RACHUNEK_NR", account)
    _t(rec, "OPIS")
    _t(rec, "FORMA_PLATNOSCI", forma)
    _t(rec, "FORMA_PLATNOSCI_ID", forma_id)
    _t(rec, "DEKLARACJA_VAT7", deduction[:7])
    _t(rec, "DEKLARACJA_VATUE", "Nie")
    _t(rec, "WALUTA")
    _t(rec, "KURS_WALUTY", "NBP")
    _t(rec, "NOTOWANIE_WALUTY_ILE", "1")
    _t(rec, "NOTOWANIE_WALUTY_ZA_ILE", "1")
    _t(rec, "DATA_KURSU", issue)
    _t(rec, "KURS_DO_KSIEGOWANIA", "Nie")
    _t(rec, "KURS_WALUTY_2", "NBP")
    _t(rec, "NOTOWANIE_WALUTY_ILE_2", "1")
    _t(rec, "NOTOWANIE_WALUTY_ZA_ILE_2", "1")
    _t(rec, "DATA_KURSU_2", issue)
    _t(rec, "PLATNOSC_VAT_W_PLN", "Nie")
    _t(rec, "AKCYZA_NA_WEGIEL", "0")
    _t(rec, "AKCYZA_NA_WEGIEL_KOLUMNA_KPR", "nie księgować")
    _t(rec, "JPK_FA", "Nie")
    _t(rec, "MPP", _yes_no(inv.split_payment))
    _t(rec, "NR_KSEF", inv.ksef_number or "")
    if inv.ksef_number:
        _t(rec, "KSEF_DATA_PRZYJECIA", received)
    _t(rec, "DODATKOWY_OPIS")

    pozycje = _t(rec, "POZYCJE")
    pozycje.text = None
    for lp, line in enumerate(inv.vat_lines, start=1):
        poz = _t(pozycje, "POZYCJA")
        poz.text = None
        _t(poz, "LP", lp)
        if line.stawka is not None:
            _t(poz, "STAWKA_VAT", _num(line.stawka))
            _t(poz, "STATUS_VAT", "opodatkowana")
        else:
            _t(poz, "STAWKA_VAT", "0")
            _t(poz, "STATUS_VAT", "zwolniona" if line.stawka_symbol == "zw" else "nie podlega")
        _t(poz, "NETTO", _num(line.netto))
        _t(poz, "VAT", _num(line.vat))
        _t(poz, "NETTO_SYS", _num(line.netto))
        _t(poz, "VAT_SYS", _num(line.vat))
        _t(poz, "NETTO_SYS2", _num(line.netto))
        _t(poz, "VAT_SYS2", _num(line.vat))
        # domyslne wartosci natywnego transferu KSeF -> rejestr; do zmiany w Optimie
        _t(poz, "RODZAJ_ZAKUPU", "towary")
        _t(poz, "ODLICZENIA_VAT", "tak")
        _t(poz, "KOLUMNA_KPR", "Towary")
        _t(poz, "KOLUMNA_RYCZALT", "3.00")
    _t(rec, "KWOTY_DODATKOWE")

    platnosci = _t(rec, "PLATNOSCI")
    if inv.total_gross:
        platnosci.text = None
        amount = _num(abs(inv.total_gross))
        reference = inv.ksef_number or inv.invoice_number
        plat = _t(platnosci, "PLATNOSC")
        plat.text = None
        _t(plat, "ID_ZRODLA_PLAT", _guid("platnosc", key))
        _t(plat, "TERMIN_PLAT", due)
        _t(plat, "FORMA_PLATNOSCI_PLAT", forma)
        _t(plat, "FORMA_PLATNOSCI_ID_PLAT", forma_id)
        _t(plat, "KWOTA_PLAT", amount)
        _t(plat, "WALUTA_PLAT")
        _t(plat, "KURS_WALUTY_PLAT", "NBP")
        _t(plat, "NOTOWANIE_WALUTY_ILE_PLAT", "1")
        _t(plat, "NOTOWANIE_WALUTY_ZA_ILE_PLAT", "1")
        _t(plat, "KWOTA_PLN_PLAT", amount)
        # korekta in minus = zwrot od dostawcy
        _t(plat, "KIERUNEK", "rozchód" if inv.total_gross > 0 else "przychód")
        _t(plat, "PODLEGA_ROZLICZENIU", "tak")
        _t(plat, "KONTO")
        _t(plat, "NIE_NALICZAJ_ODSETEK", "Nie")
        _t(plat, "PRZELEW_SEPA", "Nie")
        _t(plat, "DATA_KURSU_PLAT", sale)
        _t(plat, "WALUTA_DOK")
        _t(plat, "PLATNOSC_TYP_PODMIOTU", "kontrahent")
        _t(plat, "PLATNOSC_PODMIOT", akronim)
        _t(plat, "PLATNOSC_PODMIOT_ID", podmiot_id)
        _t(plat, "PLATNOSC_PODMIOT_NIP", nip)
        if account:
            _t(plat, "PLATNOSC_PODMIOT_RACHUNEK_NR", account)
        _t(plat, "PLAT_ELIXIR_O1", reference)
        _t(plat, "PLAT_ELIXIR_O2")
        _t(plat, "PLAT_ELIXIR_O3")
        _t(plat, "PLAT_ELIXIR_O4")
        _t(plat, "PLAT_FA_Z_PA", "Nie")
        _t(plat, "PLAT_VAN_FA_Z_PA", "Nie")
        _t(plat, "PLAT_SPLIT_PAYMENT", _yes_no(inv.split_payment))
        _t(plat, "PLAT_SPLIT_KWOTA_VAT", _num(abs(inv.total_vat)))
        _t(plat, "PLAT_SPLIT_NIP", nip)
        _t(plat, "PLAT_SPLIT_NR_DOKUMENTU", reference)
    _t(rec, "DOKUMENTY_OBD")
    _t(rec, "KODY_JPK")
    _t(rec, "ATRYBUTY")


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
