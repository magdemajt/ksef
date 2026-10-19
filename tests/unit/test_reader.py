from datetime import date, datetime
from decimal import Decimal as D

from app.fa3.builder import build_fa3
from app.fa3.mapper import map_invoice
from app.fa3.validator import validate
from app.optima.reader import _as_date
from tests.unit.test_mapper import SELLER, make_invoice


def test_as_date_truncates_optima_datetime():
    # Optima trzyma daty w kolumnach DATETIME - mssql_python zwraca datetime
    assert _as_date(datetime(2026, 10, 19, 0, 0)) == date(2026, 10, 19)
    assert type(_as_date(datetime(2026, 10, 19))) is date
    assert _as_date("2026-10-19 00:00:00") == date(2026, 10, 19)
    assert _as_date(None) is None


def test_invoice_with_datetime_dates_from_optima_is_valid_xsd():
    inv = make_invoice(
        data_dok=_as_date(datetime(2026, 10, 19)),
        data_wys=_as_date(datetime(2026, 10, 19)),
        termin=_as_date(datetime(2026, 11, 14)),
        razem_brutto=D("123.00"),
    )
    xml = build_fa3(map_invoice(inv, **SELLER))
    assert validate(xml) == []
    assert b"<P_1>2026-10-19</P_1>" in xml and b"<Termin>2026-11-14</Termin>" in xml
