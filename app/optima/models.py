"""Modele domenowe danych czytanych z bazy Optimy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


def format_address(
    ulica: str = "", nr_domu: str = "", nr_lokalu: str = "", kod_pocztowy: str = "",
    miasto: str = "", wojewodztwo: str = "", gmina: str = "", powiat: str = "",
    poczta: str = "",
) -> tuple[str, str]:
    """Adres (AdresL1, AdresL2) w ukladzie faktur wysylanych z modulu KSeF Optimy:

        L1: "Zrodlana 19" / "ul. Krakusa 11/2" (bez ulicy: "Brzezie 19")
        L2: "32-080 Brzezie, malopolskie, gmina Zabierzow, powiat krakowski, poczta Zabierzow"

    Gmina pomijana, gdy rowna miejscowosci (tak drukuje Optima, np. Krakow).
    """
    numer = nr_domu + (f"/{nr_lokalu}" if nr_domu and nr_lokalu else "")
    line1 = " ".join(p for p in (ulica or (miasto if numer else ""), numer) if p)
    line2 = ", ".join(p for p in (
        " ".join(p for p in (kod_pocztowy, miasto) if p),
        wojewodztwo,
        f"gmina {gmina}" if gmina and gmina != miasto else "",
        f"powiat {powiat}" if powiat else "",
        f"poczta {poczta}" if poczta else "",
    ) if p)
    if not line1:
        line1, line2 = line2, ""
    return line1, line2


@dataclass
class Contractor:
    knt_id: int
    kod: str
    nip: str
    nazwa: str
    ulica: str = ""
    nr_domu: str = ""
    nr_lokalu: str = ""
    miasto: str = ""
    kod_pocztowy: str = ""
    kraj: str = "PL"
    wojewodztwo: str = ""
    powiat: str = ""
    gmina: str = ""
    poczta: str = ""
    # prefiks kraju NIP z dokumentu (TrN_PodNipKraj); "" lub "PL" = nabywca krajowy
    nip_kraj: str = ""

    def address_lines(self) -> tuple[str, str]:
        return format_address(
            self.ulica, self.nr_domu, self.nr_lokalu, self.kod_pocztowy, self.miasto,
            self.wojewodztwo, self.gmina, self.powiat, self.poczta,
        )

    @property
    def nip_normalized(self) -> str:
        digits = "".join(ch for ch in (self.nip or "") if ch.isdigit())
        # polski NIP z prefiksem PL zapisany w kartotece - prefiks odpada przy filtrze cyfr
        return digits


@dataclass
class SalesLine:
    lp: int
    nazwa: str
    ilosc: Decimal
    jednostka: str
    cena: Decimal
    stawka: Decimal
    flaga: int
    wartosc_netto: Decimal
    # towar/usluga z zalacznika nr 15 (TrE_SplitPay) -> FaWiersz/P_12_Zal_15
    zal15: bool = False


@dataclass
class SalesInvoice:
    trn_id: int
    numer_pelny: str
    data_wystawienia: date  # TrN_DataWys -> P_1
    data_sprzedazy: date | None  # TrN_DataOpe -> P_6 (gdy rozna od P_1)
    termin: date | None
    razem_netto: Decimal
    razem_vat: Decimal
    razem_brutto: Decimal
    korekta: bool
    waluta: str
    kurs: Decimal | None  # kurs waluty (ile PLN za kurs_l jednostek waluty); None dla PLN
    kurs_l: Decimal | None
    forma_platnosci: str | None
    buyer: Contractor | None
    # dokument w buforze Optimy (TrN_Bufor=1) = draft; widoczny, nigdy nie wysylany
    bufor: bool = False
    lines: list[SalesLine] = field(default_factory=list)
    # status KSeF odczytany z kolumn Optimy (jesli istnieja) - blokuje podwojna wysylke
    optima_ksef_number: str | None = None
    optima_ksef_status: str | None = None
    # numer referencyjny wysylki z modulu KSeF Optimy - wysylka w toku lub nieudana
    optima_ksef_ref: str | None = None
    # znacznik "Nie wysylaj dokumentu do KSeF" na dokumencie Optimy
    optima_nie_wysylaj: bool = False
    # mechanizm podzielonej platnosci wyliczony przez Optime (TrN_SplitPay) -> P_18A
    split_payment: bool = False
    # rachunek firmy z platnosci dokumentu (BnkZdarzenia -> BnkRachunki) -> Platnosc/RachunekBankowy
    rachunek_nr: str | None = None
    nazwa_banku: str | None = None
