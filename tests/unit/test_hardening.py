"""Bledy znalezione przegladem calej aplikacji: kazdy test opisuje sytuacje, ktora je wywolywala."""

import threading
from decimal import Decimal as D

import pytest
from lxml import etree

from app.fa3.builder import build_fa3
from app.fa3.mapper import MappingError, map_invoice, polish_nip
from app.fa3.parser import ParseError, parse_invoice
from app.fa3.validator import validate
from app.optima.contractors import ksef_number_known, ksef_numbers_known
from app.optima.db import _connection_string
from app.optima.models import Contractor, SalesLine
from app.optima.writer import _iso_date
from tests.unit.test_mapper import SELLER, make_invoice
from tests.unit.test_parser_and_validator import _built_invoice_xml

NS = "{http://crd.gov.pl/wzor/2025/06/25/13775/}"


def _purchase_xml(edit) -> bytes:
    """Faktura 100 netto (23%) + 50 netto (8%), brutto 177 - po zmianach wprowadzonych przez edit."""
    root = etree.fromstring(_built_invoice_xml())
    edit(root.find(f"{NS}Fa"))
    return etree.tostring(root)


def _drop_summaries(fa) -> None:
    for el in list(fa):
        if etree.QName(el).localname.startswith(("P_13_", "P_14_")):
            fa.remove(el)


class TestParserTotals:
    def test_totals_from_lines_are_used_when_they_match_amount_due(self):
        parsed = parse_invoice(_purchase_xml(_drop_summaries))
        assert (parsed.total_net, parsed.total_vat, parsed.total_gross) == (
            D("150.00"), D("27.00"), D("177.00"),
        )

    def test_lines_that_do_not_add_up_to_amount_due_are_not_guessed(self):
        def gross_priced_lines(fa):
            _drop_summaries(fa)
            for line in fa.findall(f"{NS}FaWiersz"):
                line.remove(line.find(f"{NS}P_11"))  # wystawca podal tylko wartosci brutto

        with pytest.raises(ParseError, match="wprowadz ja recznie"):
            parse_invoice(_purchase_xml(gross_priced_lines))

    def test_correction_rows_before_change_are_not_counted_as_sales(self):
        def correction(fa):
            _drop_summaries(fa)
            first = fa.find(f"{NS}FaWiersz")
            etree.SubElement(first, f"{NS}StanPrzed").text = "1"
            fa.find(f"{NS}P_15").text = "54.00"  # zostaje tylko wiersz 50 netto, 8%

        parsed = parse_invoice(_purchase_xml(correction))
        assert (parsed.total_net, parsed.total_vat) == (D("50.00"), D("4.00"))

    def test_vat_from_lines_is_computed_per_rate_not_per_line(self):
        def many_small_lines(fa):
            _drop_summaries(fa)
            lines = fa.findall(f"{NS}FaWiersz")
            fa.remove(lines[1])
            lines[0].find(f"{NS}P_11").text = "0.10"
            for _ in range(9):
                lines[0].addnext(etree.fromstring(etree.tostring(lines[0])))
            fa.find(f"{NS}P_15").text = "1.23"  # 10 x 0.10 netto; VAT 0.23, nie 10 x 0.02

        parsed = parse_invoice(_purchase_xml(many_small_lines))
        assert (parsed.total_net, parsed.total_vat) == (D("1.00"), D("0.23"))

    def test_comment_inside_amount_does_not_zero_it(self):
        def commented(fa):
            for tag in ("P_15", "P_13_1"):
                el = fa.find(f"{NS}{tag}")
                value, el.text = el.text, None
                el.append(etree.Comment(" kwota "))
                el[0].tail = value

        parsed = parse_invoice(_purchase_xml(commented))
        assert (parsed.total_net, parsed.total_gross) == (D("150.00"), D("177.00"))

    def test_taxi_flat_rate_bucket_is_four_percent(self):
        def taxi(fa):
            fa.find(f"{NS}P_13_2").tag = f"{NS}P_13_4"
            fa.find(f"{NS}P_14_2").tag = f"{NS}P_14_4"

        parsed = parse_invoice(_purchase_xml(taxi))
        assert [(v.stawka_symbol, v.stawka) for v in parsed.vat_lines][-1] == ("4", D(4))


class TestSalesXml:
    def test_control_character_in_name_is_reported_with_field_name(self):
        inv = make_invoice(
            lines=[SalesLine(1, "Usluga\x0bX", D(1), "szt.", D("100.00"), D(23), 2, D("100.00"))]
        )
        with pytest.raises(MappingError, match="P_7 zawiera znak niedozwolony"):
            build_fa3(map_invoice(inv, **SELLER))

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("526-104-01-58", "5261040158"),
            ("PL 5261040158", "5261040158"),
            ("pl526 104 01 58", "5261040158"),
            ("SK2021234567", None),  # slowacki numer VAT to nie polski NIP 2021234567
            ("CZ1234567890", None),
            ("526104015", None),
            ("", None),
            (None, None),
        ],
    )
    def test_only_polish_nip_is_accepted_as_buyer_nip(self, raw, expected):
        assert polish_nip(raw) == expected

    def test_foreign_vat_id_without_country_field_is_blocked(self):
        buyer = Contractor(
            knt_id=1, kod="SK", nip="SK2021234567", nazwa="Odberatel",
            ulica="Testowa", nr_domu="5", miasto="Bratislava", kod_pocztowy="00-001",
        )
        with pytest.raises(MappingError, match="NIP nabywcy"):
            map_invoice(make_invoice(buyer=buyer), **SELLER)

    def test_invalid_document_is_never_reported_valid_under_parallel_validation(self):
        valid = _built_invoice_xml()
        invalid = valid.replace(b"<P_2>FA/RT/1</P_2>", b"")
        assert validate(invalid) != []
        stop = threading.Event()

        def keep_validating_valid():
            while not stop.is_set():
                validate(valid)

        others = [threading.Thread(target=keep_validating_valid) for _ in range(3)]
        for thread in others:
            thread.start()
        try:
            empty = sum(validate(invalid) == [] for _ in range(300))
        finally:
            stop.set()
            for thread in others:
                thread.join()
        assert empty == 0


class TestOptimaAccess:
    def test_password_with_separator_characters_survives_connection_string(self):
        text = _connection_string("srv\\OPTIMA", "CDN_X", "ksef_reader", "p;a}s{s=1", True)
        assert "UID={ksef_reader};PWD={p;a}}s{s=1};Encrypt=yes" in text
        # dodatkowo parserem sterownika, jesli ta wersja go udostepnia (prywatne API)
        parser = pytest.importorskip("mssql_python.connection_string_parser")
        try:
            parsed = parser._ConnectionStringParser()._parse(text)
        except (AttributeError, TypeError):
            pytest.skip("inna wersja mssql_python - brak _ConnectionStringParser._parse")
        assert (parsed["uid"], parsed["pwd"]) == ("ksef_reader", "p;a}s{s=1")

    def test_duplicate_check_fails_loudly_on_query_error(self):
        class Conn:
            def __init__(self, error):
                self.error = error

            def cursor(self):
                conn = self

                class Cur:
                    description = ()

                    def execute(self, sql, params=()):
                        raise conn.error

                    def close(self):
                        pass

                return Cur()

        missing = Conn(RuntimeError("Invalid object name 'CDN.DokumentyKSeF'"))
        assert ksef_number_known(missing, "K1") is False
        assert ksef_numbers_known(missing, ["K1"]) == set()
        # zajete polaczenie / timeout to nie "Optima nie zna tej faktury"
        busy = Conn(RuntimeError("Connection is busy with results for another command"))
        with pytest.raises(RuntimeError, match="busy"):
            ksef_number_known(busy, "K1")
        with pytest.raises(RuntimeError, match="busy"):
            ksef_numbers_known(busy, ["K1"])

    def test_payment_due_is_bound_as_date_not_text(self):
        assert _iso_date("2026-11-05").isoformat() == "2026-11-05"
        assert _iso_date("2026-11-05T00:00:00") is not None
        assert _iso_date(None) is None and _iso_date("wkrotce") is None


# -- trasy GUI --

import logging.config  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from fastapi import HTTPException  # noqa: E402

from app.config import CompanyConfig  # noqa: E402
from app.state import db as state_db  # noqa: E402
from app.state.repo import AuditRepo, BatchRepo, DownloadRepo, SentRepo  # noqa: E402
from app.web.routes.purchases import purchase_xml  # noqa: E402
from app.web.routes.sales import job_status, sales_send  # noqa: E402


@pytest.fixture
def sales(tmp_path):
    conn = state_db.connect(tmp_path / "state.db")
    started = []
    company = CompanyConfig(nip="5261040158", name="Firma", database="CDN_X")
    context = SimpleNamespace(
        check_csrf=lambda token: None,
        company=lambda cid: company,
        cfg=SimpleNamespace(ksef=SimpleNamespace(environment="test", allow_prod_send=False)),
        sent=SentRepo(conn), batches=BatchRepo(conn), audit=AuditRepo(conn),
        csrf_token="x",
    )
    context.send_service = lambda cid: SimpleNamespace(
        send_batch_async=lambda ids, df, dt: started.append(list(ids))
    )
    rendered = {}
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        templates=SimpleNamespace(
            TemplateResponse=lambda request, name, variables, **kw: rendered.update(
                name=name, kw=kw, **variables
            )
        )
    )))

    def send(token, company_id="firma_a"):
        return sales_send(
            request=request, c=company_id, csrf="x", confirm_token=token,
            date_from="2026-11-01", date_to="2026-11-30", nip_confirm="", context=context,
        )

    return SimpleNamespace(
        context=context, started=started, send=send, request=request, rendered=rendered
    )


class TestSalesSend:
    def test_accepted_invoice_is_not_sent_again_from_a_stale_page(self, sales):
        ctx = sales.context
        ctx.sent.upsert("firma_a", 1, "test", status="accepted", numer_pelny="FA/1")
        ctx.sent.upsert("firma_a", 2, "test", status="error", numer_pelny="FA/2",
                        error_detail="Blad krytyczny wysylki - patrz log", error_code=500)
        token = ctx.batches.create("firma_a", "test", [1, 2], {})
        sales.send(token)
        assert sales.started == [[2]]
        assert ctx.sent.get("firma_a", 1, "test")["status"] == "accepted"
        retried = ctx.sent.get("firma_a", 2, "test")
        # ponowna proba zaczyna z czystym opisem - stary blad nie zostaje przy przyjetej fakturze
        assert (retried["status"], retried["error_detail"], retried["error_code"]) == (
            "queued", None, None,
        )

    def test_batch_with_nothing_left_to_send_is_refused(self, sales):
        ctx = sales.context
        ctx.sent.upsert("firma_a", 1, "test", status="sent", numer_pelny="FA/1")
        token = ctx.batches.create("firma_a", "test", [1], {})
        with pytest.raises(HTTPException) as refused:
            sales.send(token)
        assert refused.value.status_code == 409 and sales.started == []

    def test_confirmation_of_another_company_or_environment_is_refused(self, sales):
        ctx = sales.context
        other_company = ctx.batches.create("firma_b", "test", [1], {})
        other_env = ctx.batches.create("firma_a", "prod", [1], {})
        for token in (other_company, other_env):
            with pytest.raises(HTTPException) as refused:
                sales.send(token)
            assert refused.value.status_code == 409
        assert sales.started == []
        assert ctx.sent.get("firma_a", 1, "test") is None

    def test_optima_down_leaves_nothing_queued_and_confirmation_usable(self, sales):
        ctx = sales.context
        token = ctx.batches.create("firma_a", "test", [1], {})
        working = ctx.send_service

        def unreachable(cid):
            raise RuntimeError("SQL Server niedostepny")

        ctx.send_service = unreachable
        with pytest.raises(HTTPException) as refused:
            sales.send(token)
        assert refused.value.status_code == 503
        assert ctx.sent.get("firma_a", 1, "test") is None
        ctx.send_service = working
        sales.send(token)  # to samo potwierdzenie dziala, gdy baza wroci
        assert sales.started == [[1]]

    def test_interrupted_sends_become_retryable_after_restart(self, sales):
        sent = sales.context.sent
        sent.upsert("firma_a", 1, "test", status="queued", numer_pelny="FA/1")
        sent.upsert("firma_a", 2, "test", status="sent", numer_pelny="FA/2")
        sent.upsert("firma_a", 3, "test", status="accepted", numer_pelny="FA/3")
        assert sent.reset_interrupted() == 2
        rows = {i: sent.get("firma_a", i, "test") for i in (1, 2, 3)}
        assert [rows[i]["status"] for i in (1, 2, 3)] == ["error", "error", "accepted"]
        assert "mozna wyslac ponownie" in rows[1]["error_detail"]
        assert "sprawdz w KSeF przed ponowna wysylka" in rows[2]["error_detail"]

    def test_status_polling_stops_when_batch_is_done(self, sales):
        sent = sales.context.sent
        sent.upsert("firma_a", 1, "test", status="sent", numer_pelny="FA/1")
        job_status(request=sales.request, c="firma_a", trn_ids="1", context=sales.context)
        assert sales.rendered["kw"]["status_code"] == 200
        sent.upsert("firma_a", 1, "test", status="accepted")
        job_status(request=sales.request, c="firma_a", trn_ids="1", context=sales.context)
        assert sales.rendered["kw"]["status_code"] == 286
        with pytest.raises(HTTPException) as bad:
            job_status(request=sales.request, c="firma_a", trn_ids="abc", context=sales.context)
        assert bad.value.status_code == 400


def test_purchase_from_other_environment_is_not_served(tmp_path):
    conn = state_db.connect(tmp_path / "state.db")
    downloads = DownloadRepo(conn)
    xml = tmp_path / "inv.xml"
    xml.write_bytes(_built_invoice_xml())
    downloads.insert_if_new("firma_a", "test", "K1", xml_path=str(xml), invoice_number="FA/1")
    row_id = downloads.list_for("firma_a", "test")[0]["id"]

    def context(env):
        return SimpleNamespace(
            downloads=downloads, cfg=SimpleNamespace(ksef=SimpleNamespace(environment=env))
        )

    assert purchase_xml(invoice_id=row_id, c="firma_a", context=context("test")).body
    with pytest.raises(HTTPException):  # karta otwarta przed przelaczeniem test -> prod
        purchase_xml(invoice_id=row_id, c="firma_a", context=context("prod"))


def test_desktop_log_goes_to_a_file(tmp_path):
    from app.desktop import logging_config

    log_path = tmp_path / "data" / "ksef-bridge.log"
    root = logging.getLogger()
    before = (root.level, list(root.handlers))
    try:
        logging.config.dictConfig(logging_config(log_path))
        logging.getLogger("app.ksef.sender").error("Wysylka partii nie powiodla sie")
        logging.getLogger("uvicorn.error").error("Exception in ASGI application")
        for handler in logging.getLogger().handlers + logging.getLogger("uvicorn").handlers:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
    finally:
        for name in ("", "uvicorn", "uvicorn.access"):
            for handler in list(logging.getLogger(name).handlers):
                logging.getLogger(name).removeHandler(handler)
                handler.close()
        root.setLevel(before[0])
        for handler in before[1]:
            root.addHandler(handler)
    assert "Wysylka partii nie powiodla sie" in text
    assert "Exception in ASGI application" in text


def test_wizard_does_not_overwrite_an_existing_company(monkeypatch):
    from app import configstore
    from app.web.routes import setup as setup_routes

    writes = []
    monkeypatch.setattr(configstore, "update_company", lambda *a, **k: writes.append("update"))
    monkeypatch.setattr(configstore, "add_company", lambda *a, **k: writes.append("add"))
    shown = {}
    monkeypatch.setattr(
        setup_routes, "_render",
        lambda request, context, message=None, ok=True, **kw: shown.update(msg=message, ok=ok),
    )
    real = CompanyConfig(nip="5265877635", name="Prawdziwa", database="CDN_A")
    context = SimpleNamespace(
        check_csrf=lambda token: None, cfg=SimpleNamespace(companies={"firma_a": real}),
        reload=lambda: None, audit=SimpleNamespace(log=lambda *a, **k: None),
    )
    setup_routes.setup_company(
        request=None, csrf="x", company_id="firma_a", nip="5261040158", name="Inna",
        database="CDN_B", address_line1="", address_line2="", purchase_register="ZAKUP",
        lp_numbering="month", context=context,
    )
    assert writes == []
    assert shown["ok"] is False and "juz istnieje" in shown["msg"]


def test_gateway_of_previous_environment_is_not_reused(tmp_path):
    from app.web.deps import AppContext

    company = CompanyConfig(nip="5265877635", name="Firma", database="CDN_A")
    cfg = SimpleNamespace(
        ksef=SimpleNamespace(environment="test"), company=lambda cid: company,
    )
    context = AppContext(
        cfg=cfg, conn=state_db.connect(tmp_path / "state.db"), secrets=None, csrf_token="x"
    )
    first = context.gateway("firma_a")
    assert context.gateway("firma_a") is first
    cfg.ksef.environment = "prod"  # zapis ustawien bez wyczyszczenia cache (wyscig z kolejka)
    switched = context.gateway("firma_a")
    assert switched is not first and switched.env == "prod"
