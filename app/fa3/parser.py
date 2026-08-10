"""Parser przychodzacych faktur FA(3)/FA(2) z KSeF -> dane do rejestru VAT zakupu.

Rejestr VAT potrzebuje naglowka + rozbicia po stawkach (nie pozycji), wiec
czytamy sumy P_13_x/P_14_x. Gdy ich brak (schemat tego nie wymusza przy fakturach
zaliczkowych itp.), agregujemy z wierszy FaWiersz.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from lxml import etree

# Obslugujemy oba namespaces: FA(3) i FA(2) (stare faktury w obiegu)
NS_FA3 = "http://crd.gov.pl/wzor/2025/06/25/13775/"
NS_FA2 = "http://crd.gov.pl/wzor/2023/06/29/12648/"

# koszyk -> (symbol stawki, stawka procentowa dla wyliczen)
BUCKET_TO_RATE: dict[str, tuple[str, Decimal | None]] = {
    "P_13_1": ("23", Decimal(23)),
    "P_13_2": ("8", Decimal(8)),
    "P_13_3": ("5", Decimal(5)),
    "P_13_4": ("3", Decimal(3)),
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


def _find_ns(root: etree._Element) -> str:
    ns = etree.QName(root).namespace
    if ns not in (NS_FA3, NS_FA2):
        raise ParseError(f"Nieznany namespace faktury: {ns}")
    return ns


def _text(el: etree._Element | None, default: str = "") -> str:
    return el.text.strip() if el is not None and el.text else default


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
            symbol = _text(w.find(q("P_12")), "?")
            netto = _dec(w.find(q("P_11"))) or Decimal(0)
            rate = None
            try:
                rate = Decimal(symbol.split()[0])
            except Exception:
                pass
            line = agg.setdefault(symbol, VatLine(symbol, rate, Decimal(0), Decimal(0)))
            line.netto += netto
            if rate is not None:
                line.vat += (netto * rate / 100).quantize(Decimal("0.01"))
        vat_lines = list(agg.values())

    total_net = sum((v.netto for v in vat_lines), Decimal(0))
    total_vat = sum((v.vat for v in vat_lines), Decimal(0))

    payment_due = None
    bank_account = None
    platnosc = fa.find(q("Platnosc"))
    if platnosc is not None:
        payment_due = _text(platnosc.find(q("TerminPlatnosci/Termin"))) or None
        bank_account = _text(platnosc.find(q("RachunekBankowy/NrRB"))) or None

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
    )
