import tempfile
from pathlib import Path

import pytest

from app.config import AppConfig, CompanyConfig, OptimaSection, StateSection
from app.setup_state import PLACEHOLDER_NIP, get_setup_status, mark_sql_tested
from app.web.deps import AppContext


class FakeSecrets:
    def __init__(self, present: set[str]):
        self.present = present

    def get_optional(self, key: str):
        return "x" if key in self.present else None


def make_ctx(companies: dict, secrets: set[str], tmp: Path) -> AppContext:
    cfg = AppConfig(
        optima=OptimaSection(server="srv,1433"),
        companies=companies,
        state=StateSection(sqlite_path=tmp / "state.db", invoice_store=tmp / "inv"),
    )
    ctx = AppContext.create(cfg)
    ctx.secrets = FakeSecrets(secrets)
    return ctx


@pytest.fixture
def tmp(tmp_path):
    return tmp_path


PLACEHOLDER = CompanyConfig(nip=PLACEHOLDER_NIP, name="Placeholder", database="CDN_X")
REAL = CompanyConfig(nip="5265877635", name="Firma", database="CDN_A")


class TestSetupStatus:
    def test_fresh_install_incomplete(self, tmp):
        ctx = make_ctx({"moja_firma": PLACEHOLDER}, set(), tmp)
        st = get_setup_status(ctx)
        assert not st.complete
        assert st.current_step == 1
        assert st.placeholder_companies == ["moja_firma"]
        assert st.real_companies == []

    def test_sql_saved_but_untested(self, tmp):
        ctx = make_ctx({"moja_firma": PLACEHOLDER}, {"sql-read-password"}, tmp)
        st = get_setup_status(ctx)
        assert st.sql_password_set and not st.sql_ok
        assert st.current_step == 1  # test polaczenia wciaz zalecany

    def test_step_advances_after_sql_test(self, tmp):
        ctx = make_ctx({"moja_firma": PLACEHOLDER}, {"sql-read-password"}, tmp)
        mark_sql_tested(ctx)
        st = get_setup_status(ctx)
        assert st.sql_ok
        assert st.current_step == 2  # brak prawdziwej firmy

    def test_complete_when_all_present(self, tmp):
        ctx = make_ctx(
            {"firma_a": REAL},
            {"sql-read-password", "ksef-token:firma_a:test"},
            tmp,
        )
        st = get_setup_status(ctx)
        assert st.real_companies == ["firma_a"]
        assert st.ksef_ready["firma_a"]
        assert st.complete

    def test_token_for_other_env_not_counted(self, tmp):
        ctx = make_ctx(
            {"firma_a": REAL},
            {"sql-read-password", "ksef-token:firma_a:prod"},  # a srodowisko = test
            tmp,
        )
        st = get_setup_status(ctx)
        assert not st.ksef_ready["firma_a"]
        assert not st.complete

    def test_sql_test_invalidated_by_server_change(self, tmp):
        ctx = make_ctx({"firma_a": REAL}, {"sql-read-password"}, tmp)
        mark_sql_tested(ctx)
        ctx.cfg.optima.server = "inny,1433"
        st = get_setup_status(ctx)
        assert st.sql_password_set and not st.sql_ok
