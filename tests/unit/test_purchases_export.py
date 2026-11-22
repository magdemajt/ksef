"""Eksport zakupow do XML przez trase GUI - caly przebieg na atrapie bazy Optimy."""

from types import SimpleNamespace

import pytest
from lxml import etree

from app.config import CompanyConfig
from app.main import build_templates
from app.state import db as state_db
from app.state.repo import DownloadRepo
from app.web.routes.purchases import (
    purchase_detail,
    purchases_check,
    purchases_export,
    purchases_list,
)
from tests.unit.test_parser_and_validator import _built_invoice_xml

KSEF_NUMBER = "5265877635-20260803-1D0168800247-33"


class FakeCursor:
    """Baza Optimy bez kontrahentow i form platnosci; COUNT(*) = liczba duplikatow."""

    def __init__(self, duplicates: int):
        self.duplicates = duplicates
        self.description = []
        self._sql = ""

    def execute(self, sql, params=()):
        self._sql = sql

    def fetchone(self):
        return (self.duplicates,) if "COUNT" in self._sql else None

    def fetchall(self):
        return []


@pytest.fixture
def setup(tmp_path):
    conn = state_db.connect(tmp_path / "state.db")
    downloads = DownloadRepo(conn)
    xml_path = tmp_path / "inv.xml"
    xml_path.write_bytes(_built_invoice_xml())
    downloads.insert_if_new(
        "firma_a", "test", KSEF_NUMBER, xml_path=str(xml_path), invoice_number="FA/RT/1",
        seller_nip="5265877635",
    )
    row_id = downloads.list_for("firma_a", "test")[0]["id"]
    company = CompanyConfig(
        nip="5261040158", name="Nabywca", database="CDN_X", offline_target_id="KSIEG"
    )

    def make_context(duplicates: int):
        return SimpleNamespace(
            check_csrf=lambda token: None,
            company=lambda cid: company,
            cfg=SimpleNamespace(ksef=SimpleNamespace(environment="test")),
            downloads=downloads,
            optima=SimpleNamespace(
                read_connection=lambda comp: SimpleNamespace(cursor=lambda: FakeCursor(duplicates))
            ),
            audit=SimpleNamespace(log=lambda *a, **k: None),
        )

    return downloads, row_id, make_context


def test_xml_export_returns_optima_file_and_keeps_row_selectable(setup):
    downloads, row_id, make_context = setup
    response = purchases_export(
        request=None, c="firma_a", csrf="x", mode="xml", ids=[row_id], context=make_context(0)
    )
    assert response.media_type == "application/xml"
    root = etree.fromstring(response.body)
    section = root[0]
    assert section.findtext("{*}BAZA_DOC_ID") == "KSIEG"
    assert section.findtext("{*}REJESTR_ZAKUPU_VAT/{*}NR_KSEF") == KSEF_NUMBER
    row = downloads.get(row_id)
    assert row["status"] == "new"
    assert row["exported_at"]


def test_xml_export_skips_invoice_already_in_optima(setup, monkeypatch):
    downloads, row_id, make_context = setup
    rendered = {}
    monkeypatch.setattr(
        "app.web.routes.purchases._render_list",
        lambda request, context, c, sync_result=None: rendered.setdefault("msg", sync_result),
    )
    purchases_export(
        request=None, c="firma_a", csrf="x", mode="xml", ids=[row_id], context=make_context(1)
    )
    assert "Nic do eksportu" in rendered["msg"]
    assert downloads.get(row_id)["status"] == "already_in_optima"


def test_xml_export_requires_target_base_id(setup, monkeypatch):
    downloads, row_id, make_context = setup
    context = make_context(0)
    context.company("firma_a").offline_target_id = ""
    rendered = {}
    monkeypatch.setattr(
        "app.web.routes.purchases._render_list",
        lambda request, context, c, sync_result=None: rendered.setdefault("msg", sync_result),
    )
    purchases_export(request=None, c="firma_a", csrf="x", mode="xml", ids=[row_id], context=context)
    assert "identyfikator bazy" in rendered["msg"]
    assert downloads.get(row_id)["exported_at"] is None


def test_detail_page_lists_invoice_positions(setup):
    _, row_id, make_context = setup
    context = make_context(0)
    context.cfg.companies = {"firma_a": context.company("firma_a")}
    captured = {}

    def template_response(request, name, variables):
        captured["html"] = templates.env.get_template(name).render(request=request, **variables)

    templates = build_templates()
    request = SimpleNamespace(
        url=SimpleNamespace(path=f"/purchases/{row_id}"),
        app=SimpleNamespace(state=SimpleNamespace(
            templates=SimpleNamespace(TemplateResponse=template_response)
        )),
    )
    purchase_detail(request=request, invoice_id=row_id, c="firma_a", context=context)
    html = captured["html"]
    assert "Pozycje (2)" in html
    for expected in ("<td>A</td>", "<td>szt.</td>", "100.00", "50.00", "<td>23</td>", "<td>8</td>"):
        assert expected in html


def test_detail_page_of_other_company_redirects_to_its_list(setup):
    _, row_id, make_context = setup
    response = purchase_detail(
        request=None, invoice_id=row_id, c="firma_b", context=make_context(0)
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/purchases?c=firma_b"


class RegisterCursor:
    """Rejestr VAT Optimy: wpisy {(numer faktury, NIP): VaN_VaNID}, opcjonalnie numery KSeF."""

    def __init__(self, entries, ksef_numbers=None, native_ksef=(), has_ksef_column=True):
        self.entries = entries
        self.ksef_numbers = ksef_numbers or {}
        self.native_ksef = native_ksef
        self.has_ksef_column = has_ksef_column
        self._result = None

    def execute(self, sql, params=()):
        if "VaN_NrKSeF" in sql:
            if not self.has_ksef_column:
                raise RuntimeError("Invalid column name 'VaN_NrKSeF'")
            found = self.ksef_numbers.get(params[0])
        elif "DokumentyKSeF" in sql:
            self._result = (1 if params[0] in self.native_ksef else 0,)
            return
        else:
            found = self.entries.get(tuple(params))
        self._result = (found,) if found is not None else None

    def fetchone(self):
        return self._result


def _with_register(context, **register):
    context.optima = SimpleNamespace(
        read_connection=lambda comp: SimpleNamespace(cursor=lambda: RegisterCursor(**register))
    )
    return context


@pytest.fixture
def listed(monkeypatch):
    rendered = {}
    monkeypatch.setattr(
        "app.web.routes.purchases._render_list",
        lambda request, context, c, sync_result=None: rendered.setdefault("msg", sync_result),
    )
    return rendered


def _mark_in_xml_file(downloads, row_id):
    downloads.set_status(row_id, "new", exported_at="2026-11-20T10:00:00+00:00")


@pytest.mark.parametrize("has_ksef_column", [True, False])
def test_list_marks_xml_invoice_found_in_vat_register(setup, listed, has_ksef_column):
    downloads, row_id, make_context = setup
    _mark_in_xml_file(downloads, row_id)
    context = _with_register(
        make_context(0), entries={("FA/RT/1", "5265877635"): 55}, has_ksef_column=has_ksef_column
    )
    purchases_list(request=None, c="firma_a", context=context)
    row = downloads.get(row_id)
    assert (row["status"], row["van_id"]) == ("exported", 55)
    assert "1 faktur" in listed["msg"]


def test_list_matches_register_entry_by_ksef_number(setup, listed):
    downloads, row_id, make_context = setup
    _mark_in_xml_file(downloads, row_id)
    context = _with_register(make_context(0), entries={}, ksef_numbers={KSEF_NUMBER: 77})
    purchases_list(request=None, c="firma_a", context=context)
    assert downloads.get(row_id)["van_id"] == 77


def test_list_keeps_xml_invoice_not_yet_imported(setup, listed):
    downloads, row_id, make_context = setup
    _mark_in_xml_file(downloads, row_id)
    purchases_list(request=None, c="firma_a", context=_with_register(make_context(0), entries={}))
    assert downloads.get(row_id)["status"] == "new"
    assert listed["msg"] is None


def test_list_survives_optima_being_unreachable(setup, listed):
    downloads, row_id, make_context = setup
    _mark_in_xml_file(downloads, row_id)
    context = make_context(0)

    def refuse(company):
        raise ConnectionError("SQL Server niedostepny")

    context.optima = SimpleNamespace(read_connection=refuse)
    purchases_list(request=None, c="firma_a", context=context)
    assert downloads.get(row_id)["status"] == "new"
    assert "msg" in listed


def test_list_does_not_query_optima_without_xml_invoices(setup, listed):
    _, _, make_context = setup
    context = make_context(0)
    context.optima = None  # kazde uzycie bazy skonczyloby sie bledem
    purchases_list(request=None, c="firma_a", context=context)
    assert listed["msg"] is None


def test_check_marks_invoice_entered_in_optima_by_other_means(setup, listed):
    downloads, row_id, make_context = setup
    context = _with_register(make_context(0), entries={}, native_ksef=(KSEF_NUMBER,))
    purchases_check(request=None, c="firma_a", csrf="x", context=context)
    assert downloads.get(row_id)["status"] == "already_in_optima"
    assert "juz w Optimie 1" in listed["msg"]


def test_check_reports_optima_connection_failure(setup, listed):
    downloads, row_id, make_context = setup
    context = make_context(0)

    def refuse(company):
        raise ConnectionError("SQL Server niedostepny")

    context.optima = SimpleNamespace(read_connection=refuse)
    purchases_check(request=None, c="firma_a", csrf="x", context=context)
    assert "SQL Server niedostepny" in listed["msg"]
    assert downloads.get(row_id)["status"] == "new"
