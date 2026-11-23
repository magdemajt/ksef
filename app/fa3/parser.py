"""Parser przychodzacych faktur FA(3)/FA(2) z KSeF -> dane do rejestru VAT zakupu.

Rejestr VAT potrzebuje naglowka + rozbicia po stawkach (nie pozycji), wiec
czytamy sumy P_13_x/P_14_x. Gdy ich brak (schemat tego nie wymusza przy fakturach
zaliczkowych itp.), agregujemy z wierszy FaWiersz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from lxml import etree

# Obslugujemy oba namespaces: FA(3) i FA(2) (stare faktury w obiegu)
NS_FA3 = "http://crd.gov.pl/wzor/2025/06/25/13775/"
NS_FA2 = "http://crd.gov.pl/wzor/2023/06/29/12648/"

# koszyk -> (symbol stawki, stawka procentowa dla wyliczen)
BUCKET_TO_RATE: dict[str, tuple[str, Decimal | None]] = {
    "P_13_1": ("23", Decimal(23)),
    "P_13_2": ("8", Decimal(8)),
    "P_13_3": ("5", Decimal(5)),
    "P_13_4": ("4", Decimal(4)),  # ryczalt taksowek osobowych
    "P_13_5": ("oo", None),
    "P_13_6_1": ("0", Decimal(0)),
    "P_13_6_2": ("0 WDT", Decimal(0)),
    "P_13_6_3": ("0 EX", Decimal(0)),
    "P_13_7": ("zw", None),
    "P_13_8": ("np", None),
    "P_13_9": ("np II", None),
    "P_13_10": ("oo", None),
    "P_13_11": ("marza", None),
}
BUCKET_TO_P14 = {
    "P_13_1": "P_14_1",
    "P_13_2": "P_14_2",
    "P_13_3": "P_14_3",
    "P_13_4": "P_14_4",
    "P_13_5": "P_14_5",
}


class ParseError(ValueError):
    pass


@dataclass
class VatLine:
    stawka_symbol: str
    stawka: Decimal | None
    netto: Decimal
    vat: Decimal


@dataclass
class InvoiceLine:
    """Pozycja faktury (FaWiersz, a dla zaliczkowych ZamowienieWiersz) - tylko do podgladu."""

    lp: str
    nazwa: str
    jednostka: str = ""
    ilosc: Decimal | None = None
    cena_netto: Decimal | None = None  # P_9A
    cena_brutto: Decimal | None = None  # P_9B (faktury liczone od brutto)
    rabat: Decimal | None = None  # P_10
    wartosc_netto: Decimal | None = None  # P_11
    wartosc_brutto: Decimal | None = None  # P_11A
    stawka: str = ""  # P_12
    before_correction: bool = False  # StanPrzed = 1 (wiersz korekty "przed")
    from_order: bool = False  # wiersz zamowienia z faktury zaliczkowej


@dataclass
class PurchaseInvoice:
    ksef_number: str | None
    invoice_number: str
    issue_date: str
    seller_nip: str
    seller_name: str
    seller_address: str
    buyer_nip: str
    currency: str
    total_gross: Decimal
    total_net: Decimal
    total_vat: Decimal
    payment_due: str | None
    bank_account: str | None
    vat_lines: list[VatLine] = field(default_factory=list)
    invoice_type: str = "VAT"
    sale_date: str | None = None  # P_6 (data dostawy/wykonania uslugi)
    seller_address_l1: str = ""
    seller_address_l2: str = ""
    payment_form: str | None = None  # Platnosc/FormaPlatnosci (kod 1-7)
    cash_method: bool = False  # Adnotacje/P_16 = 1
    split_payment: bool = False  # Adnotacje/P_18A = 1
    corrected_number: str | None = None  # DaneFaKorygowanej/NrFaKorygowanej
    lines: list[InvoiceLine] = field(default_factory=list)


def _find_ns(root: etree._Element) -> str:
    ns = etree.QName(root).namespace
    if ns not in (NS_FA3, NS_FA2):
        raise ParseError(f"Nieznany namespace faktury: {ns}")
    return ns


def _text(el: etree._Element | None, default: str = "") -> str:
    if el is None:
        return default
    # itertext: komentarz wewnatrz elementu (<P_15><!-- x -->177.00</P_15>) nie gubi wartosci
    return "".join(el.itertext()).strip() or default


def _dec(el: etree._Element | None) -> Decimal | None:
    txt = _text(el)
    return Decimal(txt) if txt else None


def parse_invoice(xml: bytes, ksef_number: str | None = None) -> PurchaseInvoice:
    try:
        root = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        raise ParseError(f"Niepoprawny XML: {exc}") from exc
    ns = _find_ns(root)

    def q(path: str) -> str:
        return "/".join(f"{{{ns}}}{part}" for part in path.split("/"))

    fa = root.find(q("Fa"))
    if fa is None:
        raise ParseError("Brak elementu Fa")

    p1 = root.find(q("Podmiot1"))
    p2 = root.find(q("Podmiot2"))
    if p1 is None or p2 is None:
        raise ParseError("Brak Podmiot1/Podmiot2")

    seller_nip = _text(p1.find(q("DaneIdentyfikacyjne/NIP")))
    seller_name = _text(p1.find(q("DaneIdentyfikacyjne/Nazwa")))
    seller_addr_parts = [
        _text(p1.find(q("Adres/AdresL1"))),
        _text(p1.find(q("Adres/AdresL2"))),
    ]
    buyer_nip = _text(p2.find(q("DaneIdentyfikacyjne/NIP")))

    currency = _text(fa.find(q("KodWaluty")), "PLN")
    invoice_number = _text(fa.find(q("P_2")))
    issue_date = _text(fa.find(q("P_1")))
    total_gross = _dec(fa.find(q("P_15"))) or Decimal(0)
    invoice_type = _text(fa.find(q("RodzajFaktury")), "VAT")

    vat_lines: list[VatLine] = []
    for bucket, (symbol, rate) in BUCKET_TO_RATE.items():
        net = _dec(fa.find(q(bucket)))
        if net is None:
            continue
        vat = Decimal(0)
        p14 = BUCKET_TO_P14.get(bucket)
        if p14 is not None:
            vat = _dec(fa.find(q(p14))) or Decimal(0)
        vat_lines.append(VatLine(stawka_symbol=symbol, stawka=rate, netto=net, vat=vat))

    if not vat_lines:
        # awaryjnie agreguj z wierszy
        agg: dict[str, VatLine] = {}
        for w in fa.findall(q("FaWiersz")):
            if _text(w.find(q("StanPrzed"))) == "1":
                continue  # korekta: wiersz "przed" nie jest sprzedaza
            symbol = _text(w.find(q("P_12")), "?")
            netto = _dec(w.find(q("P_11"))) or Decimal(0)
            rate = None
            try:
                rate = Decimal(symbol.split()[0])
            except Exception:
                pass
            line = agg.setdefault(symbol, VatLine(symbol, rate, Decimal(0), Decimal(0)))
            line.netto += netto
        for line in agg.values():
            if line.stawka is not None:
                line.vat = (line.netto * line.stawka / 100).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
        vat_lines = list(agg.values())
        # Kwoty wyliczone z wierszy to przyblizenie (ceny brutto, zaliczki, korekty) - do rejestru
        # VAT moga trafic tylko wtedy, gdy zgadzaja sie z kwota naleznosci z faktury.
        summed = sum((v.netto + v.vat for v in vat_lines), Decimal(0))
        if summed != total_gross:
            raise ParseError(
                f"Faktura nie ma podsumowania stawek VAT (P_13_x), a suma wierszy {summed} "
                f"rozni sie od kwoty naleznosci {total_gross} - wprowadz ja recznie"
            )

    lines = [
        InvoiceLine(
            lp=_text(w.find(q("NrWierszaFa"))) or str(i),
            nazwa=_text(w.find(q("P_7"))),
            jednostka=_text(w.find(q("P_8A"))),
            ilosc=_dec(w.find(q("P_8B"))),
            cena_netto=_dec(w.find(q("P_9A"))),
            cena_brutto=_dec(w.find(q("P_9B"))),
            rabat=_dec(w.find(q("P_10"))),
            wartosc_netto=_dec(w.find(q("P_11"))),
            wartosc_brutto=_dec(w.find(q("P_11A"))),
            stawka=_text(w.find(q("P_12"))),
            before_correction=_text(w.find(q("StanPrzed"))) == "1",
        )
        for i, w in enumerate(fa.findall(q("FaWiersz")), start=1)
    ]
    if not lines:
        # faktura zaliczkowa: pozycje sa w zamowieniu
        lines = [
            InvoiceLine(
                lp=_text(w.find(q("NrWierszaZam"))) or str(i),
                nazwa=_text(w.find(q("P_7Z"))),
                jednostka=_text(w.find(q("P_8AZ"))),
                ilosc=_dec(w.find(q("P_8BZ"))),
                cena_netto=_dec(w.find(q("P_9AZ"))),
                wartosc_netto=_dec(w.find(q("P_11NettoZ"))),
                stawka=_text(w.find(q("P_12Z"))),
                before_correction=_text(w.find(q("StanPrzedZ"))) == "1",
                from_order=True,
            )
            for i, w in enumerate(fa.findall(q("Zamowienie/ZamowienieWiersz")), start=1)
        ]

    # Programy wystawcow czesto wypisuja wszystkie koszyki P_13_x z zerami (i wiersze
    # o wartosci 0) - do rejestru VAT trafiaja tylko stawki z kwota.
    nonzero = [v for v in vat_lines if v.netto or v.vat]
    vat_lines = nonzero or vat_lines[:1]

    total_net = sum((v.netto for v in vat_lines), Decimal(0))
    total_vat = sum((v.vat for v in vat_lines), Decimal(0))

    payment_due = None
    bank_account = None
    payment_form = None
    platnosc = fa.find(q("Platnosc"))
    if platnosc is not None:
        payment_due = _text(platnosc.find(q("TerminPlatnosci/Termin"))) or None
        bank_account = _text(platnosc.find(q("RachunekBankowy/NrRB"))) or None
        payment_form = _text(platnosc.find(q("FormaPlatnosci"))) or None

    return PurchaseInvoice(
        ksef_number=ksef_number,
        invoice_number=invoice_number,
        issue_date=issue_date,
        seller_nip=seller_nip,
        seller_name=seller_name,
        seller_address=", ".join(p for p in seller_addr_parts if p),
        buyer_nip=buyer_nip,
        currency=currency,
        total_gross=total_gross,
        total_net=total_net,
        total_vat=total_vat,
        payment_due=payment_due,
        bank_account=bank_account,
        vat_lines=vat_lines,
        invoice_type=invoice_type,
        sale_date=_text(fa.find(q("P_6"))) or None,
        seller_address_l1=seller_addr_parts[0],
        seller_address_l2=seller_addr_parts[1],
        payment_form=payment_form,
        cash_method=_text(fa.find(q("Adnotacje/P_16"))) == "1",
        split_payment=_text(fa.find(q("Adnotacje/P_18A"))) == "1",
        corrected_number=_text(fa.find(q("DaneFaKorygowanej/NrFaKorygowanej"))) or None,
        lines=lines,
    )
