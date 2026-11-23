"""Zapytania do Optimy nie zostawiaja otwartych wynikow na polaczeniu.

SQL Server (bez MARS) obsluguje jeden otwarty wynik na polaczenie: kursor z nieprzeczytanymi
wierszami blokuje kolejne zapytanie bledem "Connection is busy with results for another command".
"""

import pytest

from app.optima.contractors import (
    find_by_nip,
    find_vat_entries,
    ksef_number_known,
    ksef_numbers_known,
    payment_form_guids,
    vat_register_has,
)
from app.optima.db import fetch_all

GUID = "0A1B2C3D-1111-2222-3333-444455556666"


class StrictCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = []
        self._rows = []

    def execute(self, sql, params=()):
        busy = [c for c in self.conn.cursors if c is not self and c._rows]
        if busy:
            raise RuntimeError("Connection is busy with results for another command")
        self.conn.executed.append(sql)
        columns, rows = self.conn.answer(sql, params)
        self.description = [(name,) for name in columns]
        self._rows = list(rows)

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def close(self):
        self._rows = []
        self.conn.closed_cursors += 1


class StrictConnection:
    """Polaczenie jak SQL Server: nowe zapytanie przy nieprzeczytanym wyniku innego kursora = blad.

    Kursory nie sa zwalniane przez odsmiecanie - liczy sie tylko jawne przeczytanie/zamkniecie.
    """

    def __init__(self, contractor_cards: int = 1, has_ksef_table: bool = True):
        self.contractor_cards = contractor_cards
        self.has_ksef_table = has_ksef_table
        self.cursors: list[StrictCursor] = []
        self.executed: list[str] = []
        self.closed_cursors = 0

    def cursor(self):
        cur = StrictCursor(self)
        self.cursors.append(cur)
        return cur

    def answer(self, sql, params):
        if "DokumentyKSeF" in sql:
            if not self.has_ksef_table:
                raise RuntimeError("Invalid object name 'CDN.DokumentyKSeF'")
            return (["x"], [(0,)]) if "COUNT" in sql else (["DKF_NumerKSeF"], [("K1",)])
        if "COUNT(*) FROM CDN.VatNag" in sql:
            return ["x"], [(1,)]
        if "VatNag" in sql:
            return ["a", "b", "c"], []
        if "Knt_Kod" in sql:
            return ["id", "kod", "nip", "n1", "n2", "n3"], [
                (10 + i, f"DOSTAWCA{i}", "526-587-76-35", "Dostawca", None, None)
                for i in range(self.contractor_cards)
            ]
        if "SELECT * FROM CDN.Kontrahenci" in sql:
            return ["Knt_KntId", "Knt_GUID"], [(params[0], GUID)]
        if "FormyPlatnosci" in sql:
            return ["FPl_Nazwa", "FPl_GUID"], [("przelew", GUID), ("gotowka", GUID)]
        raise AssertionError(f"Nieoczekiwane zapytanie: {sql}")


def test_fetch_all_reads_everything_and_closes_cursor_even_on_error():
    conn = StrictConnection()
    cols, rows = fetch_all(conn, "SELECT * FROM CDN.FormyPlatnosci")
    assert cols == ["FPl_Nazwa", "FPl_GUID"] and len(rows) == 2
    with pytest.raises(AssertionError):
        fetch_all(conn, "SELECT cos_innego")
    assert conn.closed_cursors == 2


def test_contractor_with_several_cards_does_not_block_later_queries():
    conn = StrictConnection(contractor_cards=3)
    contractor = find_by_nip(conn, "5265877635")
    # pierwsza karta (najnizszy Knt_KntId), a GUID czytany juz po zamknieciu pierwszego wyniku
    assert (contractor.knt_id, contractor.kod, contractor.guid) == (10, "DOSTAWCA0", GUID)
    # kolejna faktura z tego samego eksportu: oba sprawdzenia musza dojsc do bazy
    assert ksef_number_known(conn, "K2") is False
    assert vat_register_has(conn, "5265877635", "FA/2") is True
    assert sum("COUNT" in sql for sql in conn.executed) == 2


def test_whole_export_sequence_leaves_no_open_results():
    conn = StrictConnection(contractor_cards=2)
    for number in ("FA/1", "FA/2", "FA/3"):
        ksef_number_known(conn, f"K-{number}")
        vat_register_has(conn, "5265877635", number)
        find_by_nip(conn, "5265877635")
    assert payment_form_guids(conn) == {"przelew": GUID, "gotowka": GUID}
    find_vat_entries(conn, [("K1", "5265877635", "FA/1")])
    assert ksef_numbers_known(conn, ["K1", "K2"]) == {"K1"}
    assert all(not cur._rows for cur in conn.cursors)
    assert conn.closed_cursors == len(conn.cursors)


def test_missing_ksef_table_is_still_tolerated():
    conn = StrictConnection(has_ksef_table=False)
    assert ksef_number_known(conn, "K1") is False
    assert ksef_numbers_known(conn, ["K1"]) == set()
    assert vat_register_has(conn, "5265877635", "FA/1") is True
