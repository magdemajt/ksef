from app.optima.introspect import _match_address_columns, find_company_address


class _Cur:
    def __init__(self, tables):
        self.tables = tables

    def execute(self, sql, params=()):
        if "INFORMATION_SCHEMA" in sql:
            cols = self.tables.get(params[0], {}).get("cols", [])
            self.description, self.rows = [("COLUMN_NAME",)], [(c,) for c in cols]
        else:
            table = sql.split("CDN.[", 1)[1].split("]", 1)[0]
            spec = self.tables[table]
            self.description = [(c,) for c in spec["cols"]]
            self.rows = spec["rows"]

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, tables):
        self.tables = tables

    def cursor(self):
        return _Cur(self.tables)


def test_suffix_match_ignores_correspondence_and_personal_address():
    found = _match_address_columns(
        ["Pcz_KorUlica", "Pcz_Ulica", "Pcz_OsMiasto", "Pcz_Miasto", "Pcz_NipE", "Pcz_Nip"]
    )
    assert found == {"ulica": "Pcz_Ulica", "miasto": "Pcz_Miasto", "nip": "Pcz_NipE"}


def test_finds_stamp_address_and_skips_tables_without_address():
    conn = _Conn({
        "Pieczatki": {
            "cols": ["Pcz_PczID", "Pcz_Nazwa1", "Pcz_Nip", "Pcz_Ulica", "Pcz_NrDomu",
                     "Pcz_NrLokalu", "Pcz_KodPocztowy", "Pcz_Miasto"],
            "rows": [
                (1, "Ensigner Tech", "526-587-76-35", "Brzezie", "12", "", "32-080", "Zabierzów"),
                (2, "", "", "", "", "", "", ""),
            ],
        },
        "Firma": {"cols": ["Fir_Numer", "Fir_Wartosc"], "rows": []},
    })
    assert find_company_address(conn) == [{
        "source": "CDN.Pieczatki", "nazwa": "Ensigner Tech", "nip": "526-587-76-35",
        "address_line1": "Brzezie 12", "address_line2": "32-080 Zabierzów",
    }]


def test_village_without_street_uses_postcode_line():
    conn = _Conn({"Pieczatki": {
        "cols": ["Pcz_Miasto", "Pcz_KodPocztowy"], "rows": [("Brzezie", "32-080")],
    }})
    assert find_company_address(conn)[0]["address_line1"] == "32-080 Brzezie"
