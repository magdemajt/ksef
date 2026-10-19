from datetime import date, datetime
from decimal import Decimal as D

from app.fa3.builder import build_fa3
from app.fa3.mapper import map_invoice
from app.fa3.validator import validate
from app.optima.reader import SalesReader, _as_date
from tests.unit.test_mapper import SELLER, make_invoice


def test_as_date_truncates_optima_datetime():
    # Optima trzyma daty w kolumnach DATETIME - mssql_python zwraca datetime
    assert _as_date(datetime(2026, 10, 19, 0, 0)) == date(2026, 10, 19)
    assert type(_as_date(datetime(2026, 10, 19))) is date
    assert _as_date("2026-10-19 00:00:00") == date(2026, 10, 19)
    assert _as_date(None) is None


def test_invoice_with_datetime_dates_from_optima_is_valid_xsd():
    inv = make_invoice(
        data_wystawienia=_as_date(datetime(2026, 10, 19)),
        data_sprzedazy=_as_date(datetime(2026, 10, 19)),
        termin=_as_date(datetime(2026, 11, 14)),
        razem_brutto=D("123.00"),
    )
    xml = build_fa3(map_invoice(inv, **SELLER))
    assert validate(xml) == []
    assert b"<P_1>2026-10-19</P_1>" in xml and b"<Termin>2026-11-14</Termin>" in xml


def _select_width(sql: str) -> int:
    """Liczba kolumn SELECT (przecinki na zerowym poziomie nawiasow)."""
    body = sql.split("SELECT", 1)[1].split("FROM CDN.TraNag", 1)[0]
    depth, count = 0, 1
    for ch in body:
        depth += ch == "("
        depth -= ch == ")"
        count += ch == "," and depth == 0
    return count


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        self.conn.sqls.append(sql)
        if "INFORMATION_SCHEMA" in sql:
            self.rows = [(c,) for c in self.conn.columns.get(params[0], [])]
        elif self.conn.fail_full and "TrN_PodNipE" in sql:
            raise RuntimeError("Invalid column name 'TrN_PodNipE'")
        else:
            self.rows = [self.conn.row]

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, row, fail_full=False):
        self.row, self.fail_full, self.sqls = row, fail_full, []
        self.columns = {"TraNag": ["TrN_Waluta", "TrN_KursL", "TrN_KursM", "TrN_NrKSeF",
                                   "TrN_KSeFStatus", "TrN_KSeFReferenceNumber",
                                   "TrN_NieWysylajDokumentuDoKSeF", "TrN_SplitPay"]}

    def cursor(self):
        return _Cur(self)


OPTIMA_ROW = (
    1032, "FS/11/2026", datetime(2026, 10, 19), datetime(2026, 9, 30), datetime(2026, 11, 14),
    D("25707.23"), D("5912.66"), D("31619.89"), 0, "przelew",
    7, "KLIENT", "PL", "5261040158", "Klient", "Sp. z o.o.", "",
    "Testowa ", "5", "", "Kraków", "30-001", 0,
    "PLN", D(1), D(1), "", "Nie wysłano", None, 0, 1,
)


def test_full_and_fallback_select_have_same_width():
    conn = _Conn(OPTIMA_ROW, fail_full=True)
    SalesReader(conn).list_sales(date(2026, 10, 1), date(2026, 10, 31))
    full, minimal = [s for s in conn.sqls if "FROM CDN.TraNag T" in s]
    assert _select_width(full) == _select_width(minimal) == len(OPTIMA_ROW)


def test_row_uses_document_snapshot_and_flags():
    inv = SalesReader(_Conn(OPTIMA_ROW)).list_sales(date(2026, 10, 1), date(2026, 10, 31))[0]
    assert inv.data_wystawienia == date(2026, 10, 19)
    assert inv.data_sprzedazy == date(2026, 9, 30)
    assert inv.buyer.nazwa == "Klient Sp. z o.o."
    assert inv.buyer.ulica == "Testowa" and inv.buyer.nip_kraj == "PL"
    assert inv.optima_ksef_number is None  # TrN_NrKSeF = '' (NOT NULL)
    assert inv.split_payment is True and inv.optima_nie_wysylaj is False
