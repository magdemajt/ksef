from app.optima.introspect import diff_probes


def test_diff_shows_changed_tranag_columns_and_new_ksef_rows():
    before = {
        "tranag": {"TrN_TrNID": "5", "TrN_KSeFNumer": None, "TrN_Opis": "x"},
        "dokumenty_ksef": [{"DKF_Id": "8", "DKF_NumerKSeF": "OLD"}],
    }
    after = {
        "tranag": {"TrN_TrNID": "5", "TrN_KSeFNumer": "5265877635-20261001-ABC", "TrN_Opis": "x"},
        "dokumenty_ksef": [
            {"DKF_Id": "9", "DKF_NumerKSeF": "5265877635-20261001-ABC"},
            {"DKF_Id": "8", "DKF_NumerKSeF": "OLD"},
        ],
    }
    changes = diff_probes(before, after)
    assert changes["changed"] == [
        {"table": "TraNag", "column": "TrN_KSeFNumer", "before": None, "after": "5265877635-20261001-ABC"}
    ]
    assert changes["dokumenty_ksef_new"] == [after["dokumenty_ksef"][0]]


def test_diff_includes_linked_vat_register_row():
    before = {"tranag": {"TrN_NrKSeF": ""}, "linked": {"VatNag": [{"VaN_NrKSeF": ""}]}}
    after = {"tranag": {"TrN_NrKSeF": "X"}, "linked": {"VatNag": [{"VaN_NrKSeF": "X"}]}}
    assert diff_probes(before, after)["changed"] == [
        {"table": "TraNag", "column": "TrN_NrKSeF", "before": "", "after": "X"},
        {"table": "VatNag", "column": "VaN_NrKSeF", "before": "", "after": "X"},
    ]
