from datetime import date
from decimal import Decimal as D

from app.fa3.builder import build_fa3
from app.fa3.mapper import map_invoice
from app.fa3.parser import parse_invoice
from app.fa3.validator import validate
from app.optima.models import Contractor, SalesInvoice, SalesLine
from app.optima.writer import export_as_xml


def _built_invoice_xml() -> bytes:
    buyer = Contractor(
        knt_id=1, kod="B", nip="5261040158", nazwa="Nabywca",
        ulica="Testowa", nr_domu="5", miasto="Warszawa", kod_pocztowy="00-001",
    )
    inv = SalesInvoice(
        trn_id=1, numer_pelny="FA/RT/1", data_dok=date(2026, 8, 1), data_wys=None,
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

    def test_praca_rozproszona_export(self):
        parsed = parse_invoice(_built_invoice_xml(), "KSEF-RT-1")
        out = export_as_xml([parsed], "ZAKUP")
        text = out.decode()
        assert "<REJESTR_ZAKUPU_VAT>" in text
        assert "<NUMER_FAKTURY>FA/RT/1</NUMER_FAKTURY>" in text
        assert "<WARTOSC_NETTO>100.00</WARTOSC_NETTO>" in text
        assert "<STATUS_STAWKI_VAT>opodatkowana</STATUS_STAWKI_VAT>" in text


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
