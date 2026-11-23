"""Eksport zakupow do XML przez trase GUI - caly przebieg na atrapie bazy Optimy."""

from types import SimpleNamespace
from typing import ClassVar

import pytest
from lxml import etree

from app.config import CompanyConfig
from app.main import build_templates
from app.state import db as state_db
from app.state.repo import DownloadRepo, SyncStateRepo, XmlQueueRepo
from app.web.routes.purchases import (
    purchase_detail,
    purchases_check,
    purchases_export,
    purchases_list,
    purchases_queue,
    purchases_queue_add,
    purchases_queue_control,
    purchases_queue_move,
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
            xml_queue=XmlQueueRepo(conn),
            sync_state=SyncStateRepo(conn),
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

    queries: ClassVar[list[str]] = []  # wspolna lista zapytan - do liczenia rund do bazy

    def __init__(self, entries, ksef_numbers=None, native_ksef=(), has_ksef_column=True):
        self.entries = entries
        self.ksef_numbers = ksef_numbers or {}
        self.native_ksef = native_ksef
        self.has_ksef_column = has_ksef_column
        self._rows = []

    def execute(self, sql, params=()):
        RegisterCursor.queries.append(sql)
        if "VaN_NrKSeF" in sql:
            if not self.has_ksef_column:
                raise RuntimeError("Invalid column name 'VaN_NrKSeF'")
            self._rows = [(k, v) for k, v in self.ksef_numbers.items() if k in params]
        elif "DokumentyKSeF" in sql:
            self._rows = [(n,) for n in self.native_ksef if n in params]
        else:
            # Optima trzyma NIP z kreskami - dopasowanie musi to zniesc
            self._rows = [
                (doc, f"{nip[:3]}-{nip[3:]}", van_id)
                for (doc, nip), van_id in self.entries.items() if doc in params
            ]

    def fetchall(self):
        return self._rows


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


def _list_html(setup, query: dict) -> str:
    """Lista zakupow wyrenderowana prawdziwym szablonem dla zadanych parametrow adresu."""
    downloads, _, make_context = setup
    for number, issued, stored in (
        ("FA/WRZ", "2026-09-30", "2026-10-02T08:00:00+00:00"),
        ("FA/PAZ", "2026-10-15", "2026-10-15T09:00:00+00:00"),
        ("FA/LIS", "2026-11-03", "2026-11-03T10:00:00+00:00"),
    ):
        downloads.insert_if_new(
            "firma_a", "test", f"KSEF-{number}", invoice_number=number, issue_date=issued,
            storage_date=stored, xml_path="/x.xml",
        )
    context = make_context(0)
    context.cfg.companies = {"firma_a": context.company("firma_a")}
    context.csrf_token = "x"
    context.default_company_id = lambda: "firma_a"
    context.limiter = SimpleNamespace(remaining=lambda c, e: 20, resets_at=lambda c, e: None)
    context.sync_state = SimpleNamespace(get=lambda c, k: None)
    templates = build_templates()
    captured = {}

    def template_response(request, name, variables):
        captured["html"] = templates.env.get_template(name).render(request=request, **variables)

    request = SimpleNamespace(
        url=SimpleNamespace(path="/purchases"), query_params=query,
        app=SimpleNamespace(state=SimpleNamespace(
            templates=SimpleNamespace(TemplateResponse=template_response)
        )),
    )
    purchases_list(request=request, c="firma_a", context=context)
    return captured["html"]


def _shown(html: str) -> set[str]:
    return {n for n in ("FA/RT/1", "FA/WRZ", "FA/PAZ", "FA/LIS") if f">{n}</a>" in html}


def test_list_without_filter_shows_everything(setup):
    html = _list_html(setup, {})
    assert _shown(html) == {"FA/RT/1", "FA/WRZ", "FA/PAZ", "FA/LIS"}
    assert "pokazano" not in html


def test_list_filters_by_issue_date_range_inclusive(setup):
    html = _list_html(setup, {"date_from": "2026-10-01", "date_to": "2026-10-15"})
    # FA/RT/1 nie ma daty wystawienia - faktury bez daty nie sa ukrywane
    assert _shown(html) == {"FA/PAZ", "FA/RT/1"}
    assert "pokazano 2 z 4" in html
    # akcje na liscie zachowuja filtr
    assert 'action="/purchases/export?date_from=2026-10-01&amp;date_to=2026-10-15' in html


def test_list_filters_by_ksef_storage_date(setup):
    html = _list_html(setup, {"date_field": "storage", "date_from": "2026-10-01",
                              "date_to": "2026-10-31"})
    # FA/WRZ wystawiona we wrzesniu, ale przyjeta w KSeF 2 pazdziernika
    assert _shown(html) == {"FA/WRZ", "FA/PAZ", "FA/RT/1"}


def test_list_filter_with_only_start_date_and_bad_input(setup):
    html = _list_html(setup, {"date_from": "2026-11-01", "date_to": "nie-data"})
    assert _shown(html) == {"FA/LIS", "FA/RT/1"}


def test_export_without_selection_returns_to_filtered_list(setup):
    _, _, make_context = setup
    request = SimpleNamespace(query_params={"date_from": "2026-10-01"})
    response = purchases_export(
        request=request, c="firma_a", csrf="x", mode="xml", ids=[], context=make_context(0)
    )
    assert response.headers["location"] == (
        "/purchases?c=firma_a&date_from=2026-10-01&date_field=issue"
    )


def _order(html: str) -> list[str]:
    names = ("FA/RT/1", "FA/WRZ", "FA/PAZ", "FA/LIS")
    return sorted((n for n in names if f">{n}</a>" in html), key=lambda n: html.index(f">{n}</a>"))


def test_list_default_order_is_newest_in_ksef_first(setup):
    html = _list_html(setup, {})
    # FA/RT/1 nie ma daty przyjecia - faktury bez daty zawsze na koncu
    assert _order(html) == ["FA/LIS", "FA/PAZ", "FA/WRZ", "FA/RT/1"]
    # widok domyslny nie dokleja parametrow do akcji
    assert 'action="/purchases/export"' in html
    # naglowek aktywnej kolumny odwraca kierunek, druga zaczyna od najnowszych
    assert 'href="/purchases?c=firma_a&amp;sort=storage&amp;dir=asc"' in html
    assert 'href="/purchases?c=firma_a&amp;sort=issue&amp;dir=desc"' in html


def test_list_sorts_by_issue_date_ascending(setup):
    html = _list_html(setup, {"sort": "issue", "dir": "asc"})
    assert _order(html) == ["FA/WRZ", "FA/PAZ", "FA/LIS", "FA/RT/1"]
    assert "Data wyst. ▲" in html
    assert 'action="/purchases/export?sort=issue&amp;dir=asc"' in html


def test_list_sort_combines_with_date_filter(setup):
    html = _list_html(setup, {"sort": "issue", "dir": "desc", "date_from": "2026-10-01"})
    assert _order(html) == ["FA/LIS", "FA/PAZ", "FA/RT/1"]
    # sortowanie zachowuje filtr, a "Wyczysc" zachowuje sortowanie
    assert "date_from=2026-10-01&amp;date_field=issue&amp;sort=issue&amp;dir=asc" in html
    assert 'href="/purchases?c=firma_a&amp;sort=issue&amp;dir=desc">Wyczysc' in html


def test_list_ignores_unknown_sort_values(setup):
    html = _list_html(setup, {"sort": "gross; DROP TABLE", "dir": "sideways"})
    assert _order(html) == ["FA/LIS", "FA/PAZ", "FA/WRZ", "FA/RT/1"]


def test_check_uses_constant_number_of_optima_queries(setup, listed):
    downloads, _, make_context = setup
    for i in range(40):
        downloads.insert_if_new(
            "firma_a", "test", f"KSEF-{i}", invoice_number=f"FA/{i}", seller_nip="5265877635"
        )
    context = _with_register(make_context(0), entries={("FA/7", "5265877635"): 70})
    RegisterCursor.queries.clear()
    purchases_check(request=None, c="firma_a", csrf="x", context=context)
    assert len(RegisterCursor.queries) == 3  # nie 2-3 na kazda z 41 faktur
    assert [r["invoice_number"] for r in downloads.list_for("firma_a", "test")
            if r["status"] == "already_in_optima"] == ["FA/7"]


# -- kolejka pobierania XML --


@pytest.fixture
def queue_setup(setup):
    """Trzy faktury bez XML (wrzesien, pazdziernik, listopad) obok jednej z XML z `setup`."""
    downloads, _, make_context = setup
    for number, issued, stored in (
        ("FA/WRZ", "2026-09-30", "2026-10-02T08:00:00+00:00"),
        ("FA/PAZ", "2026-10-15", "2026-10-15T09:00:00+00:00"),
        ("FA/LIS", "2026-11-03", "2026-11-03T10:00:00+00:00"),
    ):
        downloads.insert_if_new(
            "firma_a", "test", f"KSEF-{number}", invoice_number=number, issue_date=issued,
            storage_date=stored,
        )
    context = make_context(0)
    context.cfg.companies = {"firma_a": context.company("firma_a")}
    context.csrf_token = "x"
    context.default_company_id = lambda: "firma_a"
    context.limiter = SimpleNamespace(remaining=lambda c, e: 20, resets_at=lambda c, e: None)
    templates = build_templates()
    captured = {}

    def template_response(request, name, variables):
        captured["html"] = templates.env.get_template(name).render(request=request, **variables)

    def request(**query):
        return SimpleNamespace(
            url=SimpleNamespace(path="/purchases"), query_params=query,
            app=SimpleNamespace(state=SimpleNamespace(
                templates=SimpleNamespace(TemplateResponse=template_response)
            )),
        )

    def order():
        return [item["invoice_number"] for item in context.xml_queue.items("firma_a", "test")]

    def row_id(number):
        return next(
            r["id"] for r in downloads.list_for("firma_a", "test") if r["invoice_number"] == number
        )

    return SimpleNamespace(
        context=context, request=request, captured=captured, order=order, row_id=row_id
    )


def test_list_offers_xml_download_for_invoices_in_view(queue_setup):
    q = queue_setup
    purchases_list(request=q.request(), c="firma_a", context=q.context)
    assert "Pobierz XML faktur z tego widoku (3)" in q.captured["html"]
    assert q.captured["html"].count(">bez XML</span>") == 3
    purchases_list(
        request=q.request(date_from="2026-10-01", date_to="2026-10-31"), c="firma_a",
        context=q.context,
    )
    html = q.captured["html"]
    assert "Pobierz XML faktur z tego widoku (1)" in html
    assert 'action="/purchases/queue/add?date_from=2026-10-01&amp;date_to=2026-10-31' in html


def test_queue_add_takes_only_filtered_invoices_in_list_order(queue_setup):
    q = queue_setup
    purchases_queue_add(
        request=q.request(date_from="2026-10-01", date_to="2026-11-30"), c="firma_a", csrf="x",
        front=False, context=q.context,
    )
    # domyslne sortowanie listy: najnowsze w KSeF na gorze; FA/RT/1 ma juz XML
    assert q.order() == ["FA/LIS", "FA/PAZ"]
    html = q.captured["html"]
    assert "Kolejka XML: dodano 2" in html
    assert html.count(">w kolejce XML</span>") == 2
    # lista wraca z tym samym filtrem; FA/WRZ (poza zakresem) nie trafila do kolejki
    assert "pokazano 3 z 4" in html
    assert "Pobierz XML faktur z tego widoku (2)" in html


def test_queue_add_follows_chosen_sort_and_front_option(queue_setup):
    q = queue_setup
    purchases_queue_add(
        request=q.request(date_from="2026-11-01"), c="firma_a", csrf="x", front=False,
        context=q.context,
    )
    purchases_queue_add(
        request=q.request(date_to="2026-10-31", sort="issue", dir="asc"), c="firma_a", csrf="x",
        front=True, context=q.context,
    )
    assert q.order() == ["FA/WRZ", "FA/PAZ", "FA/LIS"]


def test_queue_add_reports_stopped_queue_and_nothing_to_do(queue_setup):
    q = queue_setup
    purchases_queue_control(
        request=q.request(), c="firma_a", csrf="x", action="stop", context=q.context
    )
    purchases_queue_add(request=q.request(), c="firma_a", csrf="x", front=False, context=q.context)
    assert "kolejka jest zatrzymana" in q.captured["html"]
    purchases_queue_add(
        request=q.request(date_from="2027-01-01"), c="firma_a", csrf="x", front=False,
        context=q.context,
    )
    assert "Wszystkie faktury z tego widoku maja juz XML" in q.captured["html"]


def test_queue_panel_closed_shows_only_status(queue_setup):
    q = queue_setup
    purchases_queue_add(request=q.request(), c="firma_a", csrf="x", front=False, context=q.context)
    purchases_queue(request=q.request(), c="firma_a", open=0, context=q.context)
    html = q.captured["html"]
    assert 'hx-swap-oob="true"' in html and "<strong>3</strong>" in html
    assert "czeka na pobranie" in html
    assert "<table>" not in html and "Zatrzymaj" not in html


def test_queue_panel_open_lists_items_with_controls(queue_setup):
    q = queue_setup
    purchases_queue_add(request=q.request(), c="firma_a", csrf="x", front=False, context=q.context)
    q.context.xml_queue.claim_next("firma_a", "test")
    purchases_queue(request=q.request(), c="firma_a", open=1, context=q.context)
    html = q.captured["html"]
    assert "pobieranie FA/LIS" in html  # naglowek pokazuje fakture w trakcie
    assert html.count('hx-post="/purchases/queue/move"') == 2  # pobieranej nie da sie ruszyc
    assert "Zatrzymaj" in html and "Wyczysc kolejke" in html


def test_queue_move_and_remove_reorder_waiting_items(queue_setup):
    q = queue_setup
    purchases_queue_add(request=q.request(), c="firma_a", csrf="x", front=False, context=q.context)
    assert q.order() == ["FA/LIS", "FA/PAZ", "FA/WRZ"]
    purchases_queue_move(
        request=q.request(), c="firma_a", csrf="x", id=q.row_id("FA/WRZ"), where="top",
        context=q.context,
    )
    assert q.order() == ["FA/WRZ", "FA/LIS", "FA/PAZ"]
    purchases_queue_move(
        request=q.request(), c="firma_a", csrf="x", id=q.row_id("FA/LIS"), where="remove",
        context=q.context,
    )
    assert q.order() == ["FA/WRZ", "FA/PAZ"]
    assert "<table>" in q.captured["html"]  # akcja zwraca rozwiniety panel


def test_queue_control_stops_resumes_and_purges(queue_setup):
    q = queue_setup
    purchases_queue_add(request=q.request(), c="firma_a", csrf="x", front=False, context=q.context)
    purchases_queue_control(
        request=q.request(), c="firma_a", csrf="x", action="stop", context=q.context
    )
    assert "zatrzymana" in q.captured["html"] and "Wznow" in q.captured["html"]
    purchases_queue_control(
        request=q.request(), c="firma_a", csrf="x", action="resume", context=q.context
    )
    assert "Zatrzymaj" in q.captured["html"]
    purchases_queue_control(
        request=q.request(), c="firma_a", csrf="x", action="purge", context=q.context
    )
    assert q.order() == []
    assert "Kolejka jest pusta" in q.captured["html"]
