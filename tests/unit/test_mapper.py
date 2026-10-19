from datetime import date
from decimal import Decimal as D

import pytest

from app.fa3.builder import build_fa3
from app.fa3.mapper import MappingError, map_invoice, rate_symbol
from app.fa3.validator import validate
from app.optima.models import Contractor, SalesInvoice, SalesLine

SELLER = dict(
    seller_nip="5265877635",
    seller_name="Sprzedawca Sp. z o.o.",
    seller_address_l1="ul. Handlowa 1",
    seller_address_l2="00-002 Warszawa",
)


def make_invoice(**overrides) -> SalesInvoice:
    buyer = Contractor(
        knt_id=1, kod="TEST", nip="526-104-01-58", nazwa="Nabywca",
        ulica="Testowa", nr_domu="5", miasto="Warszawa", kod_pocztowy="00-001",
    )
    base = dict(
        trn_id=1, numer_pelny="FA/1/2026", data_dok=date(2026, 8, 1), data_wys=None,
        termin=date(2026, 8, 15), razem_netto=D("100.00"), razem_vat=D("23.00"),
        razem_brutto=D("123.00"), korekta=False, waluta="PLN", kurs=None, kurs_l=None,
        forma_platnosci="przelew", buyer=buyer,
        lines=[SalesLine(1, "Usluga", D(1), "szt.", D("100.00"), D(23), 2, D("100.00"))],
    )
    base.update(overrides)
    return SalesInvoice(**base)


class TestRateSymbol:
    def test_taxed_rates(self):
        assert rate_symbol(D(23), 2) == "23"
        assert rate_symbol(D(8), 2) == "8"
        assert rate_symbol(D(5), 2) == "5"
        assert rate_symbol(D(0), 2) == "0 KR"

    def test_exempt_and_np(self):
        assert rate_symbol(D(0), 1) == "zw"
        assert rate_symbol(D(0), 0) == "np I"

    def test_unknown_flag_raises(self):
        with pytest.raises(MappingError, match="flaga"):
            rate_symbol(D(23), 9)

    def test_unknown_rate_raises(self):
        with pytest.raises(MappingError, match="stawka"):
            rate_symbol(D(17), 2)


class TestMapInvoice:
    def test_plain_pln_valid_xsd(self):
        fa3 = map_invoice(make_invoice(), **SELLER)
        assert validate(build_fa3(fa3)) == []

    def test_foreign_currency_has_vat_pln(self):
        inv = make_invoice(waluta="EUR", kurs=D("4.30"), kurs_l=D(1))
        fa3 = map_invoice(inv, **SELLER)
        assert fa3.vat_pln["23"] == D("98.90")  # 23.00 * 4.30
        xml = build_fa3(fa3)
        assert validate(xml) == []
        assert b"P_14_1W" in xml
        # kurs na pozycji; KursWalutyZ jest wylacznie dla faktur zaliczkowych
        assert b"<KursWaluty>4.3</KursWaluty>" in xml and b"KursWalutyZ" not in xml

    def test_foreign_currency_without_rate_blocked(self):
        inv = make_invoice(waluta="EUR")
        with pytest.raises(MappingError, match="kurs"):
            map_invoice(inv, **SELLER)

    def test_korekta_blocked(self):
        with pytest.raises(MappingError, match="koryguj"):
            map_invoice(make_invoice(korekta=True), **SELLER)

    def test_missing_buyer_nip_blocked(self):
        inv = make_invoice()
        inv.buyer.nip = ""
        with pytest.raises(MappingError, match="NIP"):
            map_invoice(inv, **SELLER)

    def test_already_sent_from_optima_blocked(self):
        inv = make_invoice(optima_ksef_number="1111111111-20260810-ABCDEF000000-00")
        with pytest.raises(MappingError, match="KSeF"):
            map_invoice(inv, **SELLER)

    def test_totals_mismatch_blocked(self):
        inv = make_invoice(razem_brutto=D("999.99"))
        with pytest.raises(MappingError, match="nie zgadza"):
            map_invoice(inv, **SELLER)

    def test_mixed_rates_buckets(self):
        inv = make_invoice(
            razem_brutto=D("187.50"),  # 100*1.23 + 50*1.08 + 10*1.05
            lines=[
                SalesLine(1, "A", D(1), "szt.", D("100.00"), D(23), 2, D("100.00")),
                SalesLine(2, "B", D(1), "szt.", D("50.00"), D(8), 2, D("50.00")),
                SalesLine(3, "C", D(1), "szt.", D("10.00"), D(5), 2, D("10.00")),
            ],
        )
        fa3 = map_invoice(inv, **SELLER)
        assert set(fa3.buckets) == {"23", "8", "5"}
        assert validate(build_fa3(fa3)) == []

    def test_exempt_blocked_without_legal_basis(self):
        inv = make_invoice(
            razem_brutto=D("50.00"),
            lines=[SalesLine(1, "B", D(1), "szt.", D("50.00"), D(0), 1, D("50.00"))],
        )
        with pytest.raises(MappingError, match="zwolniona"):
            map_invoice(inv, **SELLER)

    def test_vat_computed_from_rate_sum_not_per_line(self):
        # 10 x 0.10 netto @23%: per pozycja 10*0.02=0.20, od sumy 1.00*0.23=0.23
        inv = make_invoice(
            razem_netto=D("1.00"), razem_vat=D("0.23"), razem_brutto=D("1.23"),
            lines=[
                SalesLine(i, "X", D(1), "szt.", D("0.10"), D(23), 2, D("0.10"))
                for i in range(1, 11)
            ],
        )
        fa3 = map_invoice(inv, **SELLER)
        assert fa3.buckets["23"].vat == D("0.23")

    def test_buyer_address_includes_house_and_flat(self):
        inv = make_invoice()
        inv.buyer.nr_lokalu = "7"
        fa3 = map_invoice(inv, **SELLER)
        assert fa3.buyer_address_l1 == "Testowa 5 lok. 7"
        assert fa3.buyer_address_l2 == "00-001 Warszawa"

    def test_buyer_without_street_uses_city_line(self):
        inv = make_invoice()
        inv.buyer.ulica = inv.buyer.nr_domu = ""
        fa3 = map_invoice(inv, **SELLER)
        assert fa3.buyer_address_l1 == "00-001 Warszawa"
        assert validate(build_fa3(fa3)) == []

    def test_missing_buyer_address_blocked(self):
        inv = make_invoice()
        inv.buyer.ulica = inv.buyer.nr_domu = inv.buyer.miasto = inv.buyer.kod_pocztowy = ""
        with pytest.raises(MappingError, match="adresu"):
            map_invoice(inv, **SELLER)
