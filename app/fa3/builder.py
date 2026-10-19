"""Budowa XML FA(3) z struktury posredniej (lxml, kolejnosc elementow wg XSD)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from lxml import etree

from app.fa3.mapper import RATE_BUCKETS, Fa3Invoice

NS = "http://crd.gov.pl/wzor/2025/06/25/13775/"
E = "{%s}" % NS


def _sub(parent: etree._Element, tag: str, text: str | Decimal | int | None = None, **attrs):
    el = etree.SubElement(parent, E + tag, attrib=attrs or None)
    if text is not None:
        el.text = str(text)
    return el


def _amount(value: Decimal) -> str:
    return f"{value:.2f}"


def build_fa3(inv: Fa3Invoice, system_info: str = "ksef-bridge") -> bytes:
    root = etree.Element(E + "Faktura", nsmap={None: NS})

    # -- Naglowek --
    naglowek = _sub(root, "Naglowek")
    _sub(
        naglowek,
        "KodFormularza",
        "FA",
        kodSystemowy="FA (3)",
        wersjaSchemy="1-0E",
    )
    _sub(naglowek, "WariantFormularza", 3)
    _sub(
        naglowek,
        "DataWytworzeniaFa",
        datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    _sub(naglowek, "SystemInfo", system_info)

    # -- Podmiot1 (sprzedawca) --
    podmiot1 = _sub(root, "Podmiot1")
    dane1 = _sub(podmiot1, "DaneIdentyfikacyjne")
    _sub(dane1, "NIP", inv.seller_nip)
    _sub(dane1, "Nazwa", inv.seller_name)
    adres1 = _sub(podmiot1, "Adres")
    _sub(adres1, "KodKraju", inv.seller_country)
    _sub(adres1, "AdresL1", inv.seller_address_l1)
    if inv.seller_address_l2:
        _sub(adres1, "AdresL2", inv.seller_address_l2)

    # -- Podmiot2 (nabywca) --
    podmiot2 = _sub(root, "Podmiot2")
    dane2 = _sub(podmiot2, "DaneIdentyfikacyjne")
    _sub(dane2, "NIP", inv.buyer_nip)
    _sub(dane2, "Nazwa", inv.buyer_name)
    if inv.buyer_address_l1:
        adres2 = _sub(podmiot2, "Adres")
        _sub(adres2, "KodKraju", inv.buyer_country)
        _sub(adres2, "AdresL1", inv.buyer_address_l1)
        if inv.buyer_address_l2:
            _sub(adres2, "AdresL2", inv.buyer_address_l2)
    # Znaczniki obowiazkowe w FA(3): 2 = faktura nie dotyczy jednostki JST / czlonka grupy VAT
    _sub(podmiot2, "JST", 2)
    _sub(podmiot2, "GV", 2)

    # -- Fa --
    fa = _sub(root, "Fa")
    _sub(fa, "KodWaluty", inv.kod_waluty)
    _sub(fa, "P_1", inv.p_1)
    _sub(fa, "P_2", inv.p_2)
    if inv.p_6:
        _sub(fa, "P_6", inv.p_6)

    foreign = inv.kod_waluty != "PLN"
    # Sekwencje sum per stawka - kolejnosc P_13_1..P_13_11 wg XSD
    ordered = sorted(
        inv.buckets.items(), key=lambda kv: list(RATE_BUCKETS).index(kv[0])
    )
    for symbol, bucket in ordered:
        p13, p14, has_w = RATE_BUCKETS[symbol]
        _sub(fa, p13, _amount(bucket.net))
        if p14 is not None:
            _sub(fa, p14, _amount(bucket.vat))
            if foreign and has_w and symbol in inv.vat_pln:
                _sub(fa, p14 + "W", _amount(inv.vat_pln[symbol]))

    _sub(fa, "P_15", _amount(inv.p_15))

    # -- Adnotacje (wszystkie znaczniki "nie dotyczy") --
    adnotacje = _sub(fa, "Adnotacje")
    _sub(adnotacje, "P_16", 2)
    _sub(adnotacje, "P_17", 2)
    _sub(adnotacje, "P_18", 2)
    _sub(adnotacje, "P_18A", 1 if inv.mpp else 2)
    zwolnienie = _sub(adnotacje, "Zwolnienie")
    _sub(zwolnienie, "P_19N", 1)
    nst = _sub(adnotacje, "NoweSrodkiTransportu")
    _sub(nst, "P_22N", 1)
    _sub(adnotacje, "P_23", 2)
    pmarzy = _sub(adnotacje, "PMarzy")
    _sub(pmarzy, "P_PMarzyN", 1)

    _sub(fa, "RodzajFaktury", "VAT")

    # -- FaWiersz --
    for line in inv.lines:
        w = _sub(fa, "FaWiersz")
        _sub(w, "NrWierszaFa", line.nr)
        _sub(w, "P_7", line.nazwa)
        _sub(w, "P_8A", line.jednostka)
        _sub(w, "P_8B", _strip_decimal(line.ilosc))
        _sub(w, "P_9A", _amount(line.cena_netto))
        _sub(w, "P_11", _amount(line.wartosc_netto))
        _sub(w, "P_12", line.stawka_symbol)
        if line.zal15:
            _sub(w, "P_12_Zal_15", 1)
        # KursWalutyZ w naglowku Fa dotyczy wylacznie faktur zaliczkowych (art. 106b ust. 1
        # pkt 4) - dla zwyklej faktury walutowej kurs podaje sie na pozycji
        if foreign and inv.kurs_waluty is not None:
            _sub(w, "KursWaluty", _strip_decimal(inv.kurs_waluty))

    # -- Platnosc --
    if inv.termin_platnosci or inv.forma_platnosci or inv.rachunek_nr:
        platnosc = _sub(fa, "Platnosc")
        if inv.termin_platnosci:
            tp = _sub(platnosc, "TerminPlatnosci")
            _sub(tp, "Termin", inv.termin_platnosci)
        if inv.forma_platnosci:
            _sub(platnosc, "FormaPlatnosci", inv.forma_platnosci)
        if inv.rachunek_nr:
            rb = _sub(platnosc, "RachunekBankowy")
            _sub(rb, "NrRB", inv.rachunek_nr)
            if inv.nazwa_banku:
                _sub(rb, "NazwaBanku", inv.nazwa_banku)

    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def _strip_decimal(value: Decimal) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"
