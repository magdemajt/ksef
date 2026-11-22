import re
from datetime import date
from decimal import Decimal as D

import pytest
from lxml import etree

from app.fa3.builder import build_fa3
from app.fa3.mapper import map_invoice
from app.fa3.parser import parse_invoice
from app.fa3.validator import validate
from app.optima.models import Contractor, SalesInvoice, SalesLine
from app.optima.writer import UnsupportedInvoiceError, export_as_xml

# kolejnosc elementow wpisu REJESTR_ZAKUPU_VAT z eksportu pracy rozproszonej Optimy 2026
OPTIMA_RECORD_ORDER = [
    "ID_ZRODLA", "MODUL", "TYP", "REJESTR", "DATA_WYSTAWIENIA", "DATA_ZAKUPU", "DATA_WPLYWU",
    "TERMIN", "DATA_DATAOBOWIAZKUPODATKOWEGO", "DATA_DATAPRAWAODLICZENIA", "NUMER", "KOREKTA",
    "KOREKTA_NUMER", "WEWNETRZNA", "METODA_KASOWA", "FISKALNA", "DETALICZNA", "EKSPORT",
    "FINALNY", "PODATNIK_CZYNNY", "IDENTYFIKATOR_KSIEGOWY", "TYP_PODMIOTU", "PODMIOT",
    "PODMIOT_ID", "PODMIOT_NIP", "NAZWA1", "NAZWA2", "NAZWA3", "NIP_KRAJ", "NIP", "KRAJ",
    "WOJEWODZTWO", "POWIAT", "GMINA", "ULICA", "NR_DOMU", "NR_LOKALU", "MIASTO",
    "KOD_POCZTOWY", "POCZTA", "DODATKOWE", "PESEL", "ROLNIK", "TYP_PLATNIKA", "PLATNIK",
    "PLATNIK_ID", "PLATNIK_NIP", "PLATNIK_RACHUNEK_NR", "KATEGORIA", "KATEGORIA_ID", "OPIS",
    "FORMA_PLATNOSCI", "FORMA_PLATNOSCI_ID", "DEKLARACJA_VAT7", "DEKLARACJA_VATUE", "WALUTA",
    "KURS_WALUTY", "NOTOWANIE_WALUTY_ILE", "NOTOWANIE_WALUTY_ZA_ILE", "DATA_KURSU",
    "KURS_DO_KSIEGOWANIA", "KURS_WALUTY_2", "NOTOWANIE_WALUTY_ILE_2",
    "NOTOWANIE_WALUTY_ZA_ILE_2", "DATA_KURSU_2", "PLATNOSC_VAT_W_PLN", "AKCYZA_NA_WEGIEL",
    "AKCYZA_NA_WEGIEL_KOLUMNA_KPR", "JPK_FA", "MPP", "NR_KSEF", "KSEF_DATA_PRZYJECIA",
    "DODATKOWY_OPIS", "POZYCJE", "KWOTY_DODATKOWE", "PLATNOSCI", "DOKUMENTY_OBD", "KODY_JPK",
    "ATRYBUTY",
]


def _built_invoice_xml() -> bytes:
    buyer = Contractor(
        knt_id=1, kod="B", nip="5261040158", nazwa="Nabywca",
        ulica="Testowa", nr_domu="5", miasto="Warszawa", kod_pocztowy="00-001",
    )
    inv = SalesInvoice(
        trn_id=1, numer_pelny="FA/RT/1", data_wystawienia=date(2026, 8, 1), data_sprzedazy=None,
        termin=date(2026, 8, 20), razem_netto=D("150.00"), razem_vat=D("27.00"),
        razem_brutto=D("177.00"), korekta=False, waluta="PLN", kurs=None, kurs_l=None,
        forma_platnosci="przelew", buyer=buyer,
        lines=[
            SalesLine(1, "A", D(1), "szt.", D("100.00"), D(23), 2, D("100.00")),
            SalesLine(2, "B", D(1), "szt.", D("50.00"), D(8), 2, D("50.00")),
        ],
    )
    fa3 = map_invoice(
        inv, seller_nip="5265877635", seller_name="Sprzedawca",
        seller_address_l1="ul. X 1", seller_address_l2="00-002 Warszawa",
    )
    return build_fa3(fa3)


class TestRoundTrip:
    def test_built_invoice_parses_back(self):
        xml = _built_invoice_xml()
        assert validate(xml) == []
        parsed = parse_invoice(xml, "KSEF-RT-1")
        assert parsed.invoice_number == "FA/RT/1"
        assert parsed.seller_nip == "5265877635"
        assert parsed.buyer_nip == "5261040158"
        assert parsed.total_net == D("150.00")
        assert parsed.total_vat == D("27.00")
        assert parsed.total_gross == D("177.00")
        symbols = {v.stawka_symbol: v for v in parsed.vat_lines}
        assert symbols["23"].netto == D("100.00")
        assert symbols["8"].vat == D("4.00")
        assert parsed.payment_due == "2026-08-20"

    def test_invoice_positions_are_parsed(self):
        parsed = parse_invoice(_built_invoice_xml())
        assert [
            (ln.lp, ln.nazwa, ln.ilosc, ln.jednostka, ln.cena_netto, ln.wartosc_netto, ln.stawka)
            for ln in parsed.lines
        ] == [
            ("1", "A", D(1), "szt.", D("100.00"), D("100.00"), "23"),
            ("2", "B", D(1), "szt.", D("50.00"), D("50.00"), "8"),
        ]

    def test_zero_value_position_stays_in_preview(self):
        """Wiersz 0.00 znika z rozbicia VAT, ale w podgladzie pozycji zostaje (jest na fakturze)."""
        ns = "http://crd.gov.pl/wzor/2025/06/25/13775/"
        root = etree.fromstring(_built_invoice_xml())
        second = root.findall(f"{{{ns}}}Fa/{{{ns}}}FaWiersz")[1]
        second.find(f"{{{ns}}}P_11").text = "0.00"
        parsed = parse_invoice(etree.tostring(root))
        assert [ln.wartosc_netto for ln in parsed.lines] == [D("100.00"), D("0.00")]

    def test_zero_vat_buckets_are_dropped(self):
        """Wystawca wypisuje wszystkie koszyki P_13_x z zerami - zostaje tylko ten z kwota."""
        ns = "http://crd.gov.pl/wzor/2025/06/25/13775/"
        root = etree.fromstring(_built_invoice_xml())
        fa = root.find(f"{{{ns}}}Fa")
        for tag in [f"P_13_{i}" for i in (1, 2, 3, 4, 5)] + ["P_14_1", "P_14_2", "P_14_3"]:
            for el in fa.findall(f"{{{ns}}}{tag}"):
                fa.remove(el)
        anchor = fa.find(f"{{{ns}}}P_15")
        for tag, value in (
            ("P_13_1", "0.00"), ("P_14_1", "0.00"), ("P_13_2", "0.00"), ("P_14_2", "0.00"),
            ("P_13_6_1", "0.00"), ("P_13_7", "785.00"), ("P_13_8", "0.00"),
        ):
            el = etree.Element(f"{{{ns}}}{tag}")
            el.text = value
            anchor.addprevious(el)
        parsed = parse_invoice(etree.tostring(root), "5272974960-20261016-376AFDC00000-11")
        assert [(v.stawka_symbol, v.netto) for v in parsed.vat_lines] == [("zw", D("785.00"))]
        assert parsed.total_net == D("785.00")
        rec = etree.fromstring(export_as_xml([parsed], "ZAKUP", "KSIEG"))[0][3]
        assert [
            (p.findtext("{*}STAWKA_VAT"), p.findtext("{*}STATUS_VAT"), p.findtext("{*}NETTO"))
            for p in rec.find("{*}POZYCJE")
        ] == [("0", "zwolniona", "785")]

    def test_praca_rozproszona_export(self):
        parsed = parse_invoice(_built_invoice_xml(), "5265877635-20260803-1D0168800247-33")
        known = Contractor(
            knt_id=7, kod="SPRZEDAWCA", nip="5265877635", nazwa="Sprzedawca",
            guid="ED81CF2F-9E96-484B-9684-D2D3243DF874",
        )
        out = export_as_xml(
            [parsed], "ZAKUP", "KSIEG",
            contractors={"5265877635": known},
            payment_form_ids={"przelew": "D18D8E4B-C18E-4AB8-8876-6A67813CA4D8"},
        )
        root = etree.fromstring(out)
        ns = {"o": "http://www.comarch.pl/cdn/optima/offline"}
        assert root.tag == "{http://www.comarch.pl/cdn/optima/offline}ROOT"
        section = root.find("o:REJESTRY_ZAKUPU_VAT", ns)
        assert [etree.QName(e).localname for e in section][:4] == [
            "WERSJA", "BAZA_ZRD_ID", "BAZA_DOC_ID", "REJESTR_ZAKUPU_VAT",
        ]
        assert section.findtext("o:BAZA_ZRD_ID", namespaces=ns) == "KSEF"
        assert section.findtext("o:BAZA_DOC_ID", namespaces=ns) == "KSIEG"
        rec = section.find("o:REJESTR_ZAKUPU_VAT", ns)

        def get(path):
            return rec.findtext("/".join(f"o:{part}" for part in path.split("/")), namespaces=ns)

        assert get("REJESTR") == "ZAKUP"
        assert get("NUMER") == "FA/RT/1"
        assert get("DATA_WYSTAWIENIA") == "2026-08-01"
        # wplyw = data przyjecia w KSeF (z numeru KSeF); odliczenie nie wczesniej niz wplyw
        assert get("DATA_WPLYWU") == "2026-08-03"
        assert get("KSEF_DATA_PRZYJECIA") == "2026-08-03"
        assert get("DATA_DATAPRAWAODLICZENIA") == "2026-08-03"
        assert get("DEKLARACJA_VAT7") == "2026-08"
        assert get("TERMIN") == "2026-08-20"
        assert get("NR_KSEF") == "5265877635-20260803-1D0168800247-33"
        assert get("PODMIOT") == "SPRZEDAWCA"
        assert get("PODMIOT_ID") == "ED81CF2F-9E96-484B-9684-D2D3243DF874"
        assert get("NIP") == "5265877635"
        assert get("ULICA") == "ul. X 1"
        assert get("KOD_POCZTOWY") == "00-002"
        assert get("MIASTO") == "Warszawa"
        assert get("FORMA_PLATNOSCI_ID") == "D18D8E4B-C18E-4AB8-8876-6A67813CA4D8"
        lines = rec.findall("o:POZYCJE/o:POZYCJA", ns)
        assert [(p.findtext("o:STAWKA_VAT", namespaces=ns), p.findtext("o:NETTO", namespaces=ns),
                 p.findtext("o:VAT", namespaces=ns)) for p in lines] == [
            ("23", "100", "23"), ("8", "50", "4"),
        ]
        assert get("PLATNOSCI/PLATNOSC/KWOTA_PLAT") == "177"
        assert get("PLATNOSCI/PLATNOSC/KIERUNEK") == "rozchód"
        assert get("PLATNOSCI/PLATNOSC/PLAT_SPLIT_KWOTA_VAT") == "27"

    def test_praca_rozproszona_element_order_matches_optima_export(self):
        """Kolejnosc elementow wpisu jak w eksporcie Optimy 2026 (pola opcjonalne pominiete)."""
        parsed = parse_invoice(_built_invoice_xml(), "5265877635-20260803-1D0168800247-33")
        rec = etree.fromstring(export_as_xml([parsed], "ZAKUP"))[0][3]
        tags = [etree.QName(e).localname for e in rec]
        optional = ("KATEGORIA", "KATEGORIA_ID", "PLATNIK_RACHUNEK_NR")  # brak w fakturze testowej
        expected = [t for t in OPTIMA_RECORD_ORDER if t not in optional]
        assert tags == expected
        assert [etree.QName(e).localname for e in rec.find("{*}POZYCJE")[0]] == [
            "LP", "STAWKA_VAT", "STATUS_VAT", "NETTO", "VAT", "NETTO_SYS", "VAT_SYS",
            "NETTO_SYS2", "VAT_SYS2", "RODZAJ_ZAKUPU", "ODLICZENIA_VAT", "KOLUMNA_KPR",
            "KOLUMNA_RYCZALT",
        ]

    def test_praca_rozproszona_serialization_matches_optima_export(self):
        """Optima rozroznia pusty CDATA od pustego elementu - import walut tego wymaga."""
        parsed = parse_invoice(_built_invoice_xml(), "5265877635-20260803-1D0168800247-33")
        text = export_as_xml([parsed], "ZAKUP", "KSIEG").decode()
        assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\r\n<ROOT xmlns=')
        for fragment in (
            "<BAZA_DOC_ID>KSIEG</BAZA_DOC_ID>",
            "<WALUTA><![CDATA[]]></WALUTA>",
            "<WALUTA_PLAT><![CDATA[]]></WALUTA_PLAT>",
            "<WALUTA_DOK><![CDATA[]]></WALUTA_DOK>",
            "<NUMER><![CDATA[FA/RT/1]]></NUMER>",
            "<KOREKTA>Nie</KOREKTA>",
            "<NETTO>100</NETTO>",
            "<KWOTY_DODATKOWE/>",
            "<DOKUMENTY_OBD/><KODY_JPK/><ATRYBUTY/></REJESTR_ZAKUPU_VAT>",
        ):
            assert fragment in text, fragment
        body = text.split("\r\n", 1)[1]
        assert not re.search(r">\s+<", body)  # bez wciec miedzy elementami

    def test_praca_rozproszona_unknown_contractor_and_stable_ids(self):
        parsed = parse_invoice(_built_invoice_xml(), "5265877635-20260803-1D0168800247-33")
        first = export_as_xml([parsed], "ZAKUP")
        assert first == export_as_xml([parsed], "ZAKUP")  # ponowny eksport = te same GUID-y
        rec = etree.fromstring(first)[0][3]
        assert rec.findtext("{*}PODMIOT") == "5265877635"  # brak kartoteki -> akronim = NIP
        assert len(rec.findtext("{*}PODMIOT_ID")) == 36

    def test_praca_rozproszona_rejects_foreign_currency(self):
        parsed = parse_invoice(_built_invoice_xml(), "KSEF-RT-1")
        parsed.currency = "EUR"
        with pytest.raises(UnsupportedInvoiceError):
            export_as_xml([parsed], "ZAKUP")


class TestValidator:
    def test_bom_rejected(self):
        xml = b"\xef\xbb\xbf" + _built_invoice_xml()
        errors = validate(xml)
        assert any("BOM" in e for e in errors)

    def test_processing_instruction_rejected(self):
        xml = _built_invoice_xml().replace(
            b"<Faktura", b'<?php echo "x"?><Faktura', 1
        )
        errors = validate(xml)
        assert any("processing instruction" in e for e in errors)

    def test_broken_xml_reported(self):
        assert any("Niepoprawny XML" in e for e in validate(b"<nie-zamkniety>"))

    def test_wrong_namespace_reported(self):
        xml = _built_invoice_xml().replace(b"2025/06/25/13775", b"2023/06/29/12648")
        errors = validate(xml)
        assert errors
