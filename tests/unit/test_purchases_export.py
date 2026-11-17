"""Eksport zakupow do XML przez trase GUI - caly przebieg na atrapie bazy Optimy."""

from types import SimpleNamespace

import pytest
from lxml import etree

from app.config import CompanyConfig
from app.state import db as state_db
from app.state.repo import DownloadRepo
from app.web.routes.purchases import purchases_export
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
        "firma_a", "test", KSEF_NUMBER, xml_path=str(xml_path), invoice_number="FA/RT/1"
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
