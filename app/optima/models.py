"""Modele domenowe danych czytanych z bazy Optimy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal


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


@dataclass
class SalesInvoice:
    trn_id: int
    numer_pelny: str
    data_dok: date
    data_wys: date | None
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
