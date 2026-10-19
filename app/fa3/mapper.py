"""Mapowanie faktury sprzedazy Optimy na strukture posrednia FA(3).

Zadna wartosc nie jest zgadywana: nieznana kombinacja (stawka, flaga),
brak NIP nabywcy, korekta itd. -> MappingError i blokada wysylki.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.optima.models import SalesInvoice

TWO = Decimal("0.01")


class MappingError(ValueError):
    """Faktura nie kwalifikuje sie do automatycznego zmapowania na FA(3)."""


# TrE_Flaga w Optimie: status stawki VAT pozycji.
# Wartosci wstepne wg dokumentacji spolecznosci; `ksef-bridge diagnose` wypisuje
# rzeczywiste kombinacje (stawka, flaga) z bazy - map uzupelniamy po weryfikacji na M2.
FLAGA_OPODATKOWANA = 2
FLAGA_ZWOLNIONA = 1
FLAGA_NIE_PODLEGA = 0


def rate_symbol(stawka: Decimal, flaga: int) -> str:
    """Symbol stawki FA(3) (enum TStawkaPodatku) dla pozycji z Optimy."""
    if flaga == FLAGA_ZWOLNIONA:
        return "zw"
    if flaga == FLAGA_NIE_PODLEGA:
        return "np I"
    if flaga == FLAGA_OPODATKOWANA:
        rate = int(stawka)
        if rate in (23, 22, 8, 7, 5, 4, 3):
            return str(rate)
        if rate == 0:
            return "0 KR"  # krajowa dostawa 0%; WDT/eksport poza zakresem v1
        raise MappingError(f"Nieznana stawka VAT: {stawka}% (flaga={flaga})")
    raise MappingError(
        f"Nieznana flaga stawki VAT: {flaga} (stawka={stawka}%). "
        f"Uruchom `ksef-bridge diagnose` i zglos kombinacje do uzupelnienia mapy."
    )


# Symbol stawki -> koszyk sum P_13_x/P_14_x w naglowku Fa
# (nazwa_p13, nazwa_p14 lub None, czy_ma_wariant_W)
RATE_BUCKETS: dict[str, tuple[str, str | None, bool]] = {
    "23": ("P_13_1", "P_14_1", True),
    "22": ("P_13_1", "P_14_1", True),
    "8": ("P_13_2", "P_14_2", True),
    "7": ("P_13_2", "P_14_2", True),
    "5": ("P_13_3", "P_14_3", True),
    "4": ("P_13_4", "P_14_4", True),
    "3": ("P_13_4", "P_14_4", True),
    "0 KR": ("P_13_6_1", None, False),
    "0 WDT": ("P_13_6_2", None, False),
    "0 EX": ("P_13_6_3", None, False),
    "zw": ("P_13_7", None, False),
    "np I": ("P_13_8", None, False),
    "np II": ("P_13_9", None, False),
    "oo": ("P_13_10", None, False),
}

# Forma platnosci FA(3) (TFormaPlatnosci): 1=gotowka, 2=karta, 3=bon, 4=czek,
# 5=kredyt, 6=przelew, 7=mobilna. Mapowanie z nazw form platnosci Optimy.
PAYMENT_FORM_MAP: dict[str, str] = {
    "gotowka": "1",
    "gotówka": "1",
    "karta": "2",
    "czek": "4",
    "kredyt": "5",
    "przelew": "6",
    "przelew_w": "6",
}


@dataclass
class VatBucket:
    net: Decimal = Decimal(0)
    vat: Decimal = Decimal(0)


@dataclass
class Fa3Line:
    nr: int
    nazwa: str
    jednostka: str
    ilosc: Decimal
    cena_netto: Decimal
    wartosc_netto: Decimal
    stawka_symbol: str
    zal15: bool = False


@dataclass
class Fa3Invoice:
    """Struktura posrednia 1:1 z sekwencja elementow Fa w FA(3)."""

    kod_waluty: str
    p_1: str  # data wystawienia
    p_2: str  # numer
    p_6: str | None  # data sprzedazy (gdy rozna od p_1)
    buckets: dict[str, VatBucket]  # symbol stawki -> sumy (w walucie faktury)
    vat_pln: dict[str, Decimal]  # symbol stawki -> VAT w PLN (tylko waluta != PLN)
    p_15: Decimal  # naleznosc ogolem
    kurs_waluty: Decimal | None
    seller_nip: str
    seller_name: str
    seller_country: str
    seller_address_l1: str
    seller_address_l2: str | None
    buyer_nip: str
    buyer_name: str
    buyer_country: str
    buyer_address_l1: str | None
    buyer_address_l2: str | None
    lines: list[Fa3Line] = field(default_factory=list)
    termin_platnosci: str | None = None
    forma_platnosci: str | None = None
    mpp: bool = False  # Adnotacje/P_18A - mechanizm podzielonej platnosci


def _q2(value: Decimal) -> Decimal:
    return value.quantize(TWO, rounding=ROUND_HALF_UP)


def map_invoice(
    inv: SalesInvoice,
    seller_nip: str,
    seller_name: str,
    seller_address_l1: str,
    seller_address_l2: str,
    seller_country: str = "PL",
) -> Fa3Invoice:
    if not (seller_address_l1 or "").strip():
        raise MappingError(
            "Brak adresu firmy (sprzedawcy) w konfiguracji - uzupelnij w "
            "Ustawienia -> Firmy -> edytuj (przycisk 'Pobierz adres z Optimy')"
        )
    if inv.bufor:
        raise MappingError(
            "Faktura w buforze Optimy (draft) - zatwierdz dokument przed wysylka do KSeF"
        )
    if inv.korekta:
        raise MappingError("Faktury korygujace sa poza zakresem v1")
    if inv.optima_ksef_number:
        raise MappingError(
            f"Faktura ma juz numer KSeF w Optimie ({inv.optima_ksef_number}) - wyslana innym kanalem"
        )
    if inv.optima_nie_wysylaj:
        raise MappingError("W Optimie zaznaczono na fakturze 'Nie wysylaj dokumentu do KSeF'")
    if inv.optima_ksef_ref:
        raise MappingError(
            f"Optima rozpoczela wysylke tej faktury do KSeF (nr referencyjny {inv.optima_ksef_ref}, "
            f"status: {inv.optima_ksef_status or '?'}) - sprawdz status w Optimie"
        )
    if inv.buyer is None:
        raise MappingError("Brak kontrahenta na fakturze")
    if inv.buyer.nip_kraj not in ("", "PL"):
        raise MappingError(
            f"Nabywca z NIP {inv.buyer.nip_kraj}{inv.buyer.nip} - sprzedaz zagraniczna poza zakresem v1"
        )
    buyer_nip = inv.buyer.nip_normalized
    if len(buyer_nip) != 10:
        raise MappingError(
            f"NIP nabywcy {inv.buyer.nip!r} nie jest 10-cyfrowym polskim NIP "
            f"(sprzedaz zagraniczna/konsumencka poza zakresem v1)"
        )
    if not inv.lines:
        raise MappingError("Faktura nie ma pozycji")

    foreign = inv.waluta.upper() != "PLN"
    kurs: Decimal | None = None
    if foreign:
        if not inv.kurs or not inv.kurs_l:
            raise MappingError(
                f"Faktura w walucie {inv.waluta} bez kursu na dokumencie - nie mozna "
                f"wyliczyc VAT w PLN (P_14_xW)"
            )
        kurs = (inv.kurs / inv.kurs_l).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)

    buckets: dict[str, VatBucket] = {}
    vat_pln: dict[str, Decimal] = {}
    lines: list[Fa3Line] = []
    for line in inv.lines:
        symbol = rate_symbol(line.stawka, line.flaga)
        if symbol not in RATE_BUCKETS:
            raise MappingError(f"Stawka {symbol!r} bez koszyka P_13_x")
        bucket = buckets.setdefault(symbol, VatBucket())
        net = _q2(line.wartosc_netto)
        bucket.net += net
        lines.append(
            Fa3Line(
                nr=line.lp,
                nazwa=line.nazwa,
                jednostka=line.jednostka or "szt.",
                ilosc=line.ilosc,
                cena_netto=line.cena,
                wartosc_netto=net,
                stawka_symbol=symbol,
                zal15=line.zal15,
            )
        )

    if "zw" in buckets:
        raise MappingError(
            "Sprzedaz zwolniona z VAT (zw) wymaga podstawy prawnej zwolnienia "
            "(P_19A/B/C) - poza zakresem v1"
        )

    # VAT od sumy netto w stawce (art. 106e ust. 1 pkt 14), nie suma VAT z pozycji -
    # inaczej P_14_x rozjezdza sie o grosze z rejestrem VAT Optimy
    for symbol, bucket in buckets.items():
        if RATE_BUCKETS[symbol][1] is not None:
            bucket.vat = _q2(bucket.net * Decimal(symbol) / Decimal(100))

    addr_l1, addr_l2 = _addr_l1(inv), _addr_l2(inv)
    if not addr_l1:
        addr_l1, addr_l2 = addr_l2, ""
    if not addr_l1:
        raise MappingError(
            f"Brak adresu nabywcy w kartotece kontrahenta {inv.buyer.kod!r} "
            f"(adres jest obowiazkowy na fakturze - art. 106e ust. 1 pkt 3)"
        )

    total_net = sum((b.net for b in buckets.values()), Decimal(0))
    total_vat = sum((b.vat for b in buckets.values()), Decimal(0))
    total_gross = _q2(total_net + total_vat)
    declared_gross = _q2(inv.razem_brutto)
    if abs(total_gross - declared_gross) > Decimal("0.05"):
        raise MappingError(
            f"Suma pozycji ({total_gross}) nie zgadza sie z naglowkiem TraNag "
            f"({declared_gross}) - rozbieznosc > 5 gr, wymaga recznej weryfikacji"
        )

    if foreign:
        for symbol, bucket in buckets.items():
            _, p14, _ = RATE_BUCKETS[symbol]
            if p14 is not None:
                vat_pln[symbol] = _q2(bucket.vat * kurs)

    forma = None
    if inv.forma_platnosci:
        forma = PAYMENT_FORM_MAP.get(inv.forma_platnosci.strip().lower())

    return Fa3Invoice(
        kod_waluty=inv.waluta.upper(),
        p_1=inv.data_wystawienia.isoformat(),
        p_2=inv.numer_pelny,
        p_6=(
            inv.data_sprzedazy.isoformat()
            if inv.data_sprzedazy and inv.data_sprzedazy != inv.data_wystawienia
            else None
        ),
        buckets=buckets,
        vat_pln=vat_pln,
        p_15=declared_gross,
        kurs_waluty=kurs,
        seller_nip=seller_nip,
        seller_name=seller_name,
        seller_country=seller_country,
        seller_address_l1=seller_address_l1,
        seller_address_l2=seller_address_l2 or None,
        buyer_nip=buyer_nip,
        buyer_name=inv.buyer.nazwa,
        buyer_country=inv.buyer.kraj or "PL",
        buyer_address_l1=addr_l1,
        buyer_address_l2=addr_l2 or None,
        lines=lines,
        termin_platnosci=inv.termin.isoformat() if inv.termin else None,
        forma_platnosci=forma,
        mpp=inv.split_payment,
    )


def _addr_l1(inv: SalesInvoice) -> str:
    b = inv.buyer
    parts = [b.ulica, b.nr_domu]
    if b.nr_lokalu:
        parts.append(f"lok. {b.nr_lokalu}")
    return " ".join(p for p in parts if p).strip()


def _addr_l2(inv: SalesInvoice) -> str:
    b = inv.buyer
    return " ".join(p for p in (b.kod_pocztowy, b.miasto) if p).strip()
