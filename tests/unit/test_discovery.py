from app.optima.discovery import (
    DiscoveredDatabase,
    DiscoveryResult,
    discover_databases,
    match_registry_to_dbs,
)


class TestMatchRegistry:
    COLUMNS = ["Baz_BazId", "Baz_Nazwa", "Baz_NazwaBazy", "Baz_Opis"]
    ROWS = [
        (1, "Firma Handlowa ABC Sp. z o.o.", "CDN_ABC", "glowna"),
        (2, "Uslugi XYZ S.A.", "CDN_XYZ", ""),
        (3, "Archiwalna", "CDN_STARA", None),
    ]

    def test_maps_db_column_and_name_column(self):
        mapping = match_registry_to_dbs(
            self.COLUMNS, self.ROWS, {"CDN_ABC", "CDN_XYZ"}
        )
        assert mapping == {
            "CDN_ABC": "Firma Handlowa ABC Sp. z o.o.",
            "CDN_XYZ": "Uslugi XYZ S.A.",
        }

    def test_case_insensitive_db_match(self):
        mapping = match_registry_to_dbs(self.COLUMNS, self.ROWS, {"cdn_abc"})
        assert mapping == {"cdn_abc": "Firma Handlowa ABC Sp. z o.o."}

    def test_no_overlap_returns_empty(self):
        assert match_registry_to_dbs(self.COLUMNS, self.ROWS, {"CDN_INNA"}) == {}

    def test_empty_inputs(self):
        assert match_registry_to_dbs([], [], set()) == {}
        assert match_registry_to_dbs(self.COLUMNS, [], {"CDN_ABC"}) == {}


class FakeCursor:
    """Stub kursora: databases + INFORMATION_SCHEMA + CDN.Bazy."""

    def __init__(self):
        self.description = None
        self._result = None
        # baza -> zbior tabel CDN
        self.tables = {
            "CDN_ABC": {"TraNag", "TraElem"},
            "CDN_KNF_Konfiguracja": {"Bazy", "Konfig"},
            "Inna_Aplikacja": set(),
            "CDN_Zablokowana": None,  # None = symulacja braku dostepu
        }
        self.bazy_rows = [(1, "Firma ABC", "CDN_ABC")]
        self.bazy_cols = ["Baz_Id", "Baz_Nazwa", "Baz_NazwaBazy"]

    def execute(self, sql, params=()):
        if "sys.databases" in sql:
            self._result = [(name,) for name in self.tables]
        elif "INFORMATION_SCHEMA.TABLES" in sql:
            db = sql.split("[")[1].split("]")[0]
            if self.tables.get(db) is None:
                raise PermissionError(f"brak dostepu do {db}")
            self._result = [(1 if params[0] in self.tables[db] else 0,)]
        elif "CDN.Bazy" in sql:
            self.description = [(c,) for c in self.bazy_cols]
            self._result = list(self.bazy_rows)
        else:
            raise AssertionError(f"nieoczekiwane SQL: {sql}")

    def fetchone(self):
        return self._result[0]

    def fetchall(self):
        return self._result


class FakeConn:
    def __init__(self):
        self._cursor = FakeCursor()

    def cursor(self):
        return self._cursor


class TestDiscoverDatabases:
    def test_classification_and_enrichment(self):
        result = discover_databases(FakeConn())
        by_name = {d.name: d for d in result.databases}
        assert by_name["CDN_ABC"].kind == "company"
        assert by_name["CDN_ABC"].company_name == "Firma ABC"
        assert by_name["CDN_KNF_Konfiguracja"].kind == "config"
        assert by_name["Inna_Aplikacja"].kind == "other"
        assert by_name["CDN_Zablokowana"].kind == "no_access"
        assert result.company_databases == [by_name["CDN_ABC"]]

    def test_result_helper(self):
        r = DiscoveryResult(databases=[
            DiscoveredDatabase("A", "company"), DiscoveredDatabase("B", "config"),
        ])
        assert [d.name for d in r.company_databases] == ["A"]
