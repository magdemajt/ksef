"""Gwarancja: faktury w buforze Optimy (draft) nigdy nie sa wysylane do KSeF.

Dwie warstwy:
    1. mapper odrzuca fakture z bufor=True (bramka wspolna dla preview i wysylki),
    2. SendService oznacza ja jako 'rejected' i NIE wola gatewaya
       (zero requestow do KSeF).
"""

from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest

from app.config import CompanyConfig, KsefEnv
from app.fa3.mapper import MappingError, map_invoice
from app.ksef.sender import SendService
from app.optima.models import Contractor, SalesInvoice, SalesLine
from app.state import db as state_db
from app.state.repo import AuditRepo, SentRepo, SessionRepo

SELLER = dict(
    seller_nip="5265877635", seller_name="Sprzedawca",
    seller_address_l1="ul. X 1", seller_address_l2="00-001 W-wa",
)


def make_invoice(bufor: bool) -> SalesInvoice:
    buyer = Contractor(
        knt_id=1, kod="B", nip="5261040158", nazwa="Nabywca",
        ulica="Testowa", nr_domu="5", miasto="Warszawa", kod_pocztowy="00-001",
    )
    return SalesInvoice(
        trn_id=77, numer_pelny="FA/DRAFT/1", data_dok=date(2026, 8, 1), data_wys=None,
        termin=None, razem_netto=D("100.00"), razem_vat=D("23.00"), razem_brutto=D("123.00"),
        korekta=False, waluta="PLN", kurs=None, kurs_l=None, forma_platnosci=None,
        buyer=buyer, bufor=bufor,
        lines=[SalesLine(1, "Usluga", D(1), "szt.", D("100.00"), D(23), 2, D("100.00"))],
    )


class TestMapperRejectsDraft:
    def test_draft_blocked_with_clear_message(self):
        with pytest.raises(MappingError, match="buforze"):
            map_invoice(make_invoice(bufor=True), **SELLER)

    def test_same_invoice_approved_passes(self):
        assert map_invoice(make_invoice(bufor=False), **SELLER)


class RecordingGateway:
    """Falszywy gateway - kazde wywolanie wysylki to porazka testu."""

    def __init__(self):
        self.sent_invoices = []

    def open_online_session(self):
        return object()

    def session_ref(self, session):
        return "SESSION-TEST"

    def send_invoice(self, session, xml):
        self.sent_invoices.append(xml)
        return "INV-REF"

    def wait_for_invoice(self, session, ref, timeout=90):
        raise AssertionError("wait_for_invoice nie powinno byc wolane dla draftu")

    def close_session(self, session):
        pass

    def fetch_upo(self, session_ref, invoice_ref):
        raise AssertionError("fetch_upo nie powinno byc wolane dla draftu")


class FakeReader:
    def __init__(self, invoice: SalesInvoice):
        self.invoice = invoice

    def get_invoice(self, trn_id, date_from, date_to):
        return self.invoice if trn_id == self.invoice.trn_id else None


class TestSendServiceRejectsDraft:
    def _service(self, tmp_path: Path, gateway, invoice) -> tuple[SendService, SentRepo]:
        conn = state_db.connect(tmp_path / "state.db")
        sent = SentRepo(conn)
        company = CompanyConfig(
            nip="5265877635", name="Sprzedawca", database="CDN_X",
            address_line1="ul. X 1", address_line2="00-001 W-wa",
        )
        svc = SendService(
            company_id="firma_a", company=company, env=KsefEnv.TEST,
            gateway=gateway, reader=FakeReader(invoice),
            sent_repo=sent, session_repo=SessionRepo(conn), audit=AuditRepo(conn),
            invoice_store=tmp_path / "invoices",
        )
        svc.UPO_RETRY_DELAY_S = 0.01
        return svc, sent

    def test_draft_marked_rejected_and_never_sent(self, tmp_path):
        gateway = RecordingGateway()
        svc, sent = self._service(tmp_path, gateway, make_invoice(bufor=True))

        svc._send_batch_inner([77], date(2026, 8, 1), date(2026, 8, 31))

        assert gateway.sent_invoices == [], "draft zostal wyslany do KSeF!"
        row = sent.get("firma_a", 77, "test")
        assert row is not None
        assert row["status"] == "rejected"
        assert "buforze" in row["error_detail"]

    def test_approved_invoice_is_sent(self, tmp_path):
        class HappyGateway(RecordingGateway):
            def wait_for_invoice(self, session, ref, timeout=90):
                class Status:
                    class status:
                        code = 200
                        description = "Sukces"
                        details = None
                    ksef_number = "5265877635-20260810-ABCDEF000000-00"
                    reference_number = ref
                return Status()

            def fetch_upo(self, session_ref, invoice_ref):
                return b"<upo/>"

        gateway = HappyGateway()
        svc, sent = self._service(tmp_path, gateway, make_invoice(bufor=False))
        svc._send_batch_inner([77], date(2026, 8, 1), date(2026, 8, 31))
        assert len(gateway.sent_invoices) == 1
        assert sent.get("firma_a", 77, "test")["status"] == "accepted"


class TestSendServiceIsolatesFailures:
    def test_db_error_on_one_invoice_does_not_abort_batch(self, tmp_path):
        good = make_invoice(bufor=False)

        class FlakyReader(FakeReader):
            def get_invoice(self, trn_id, date_from, date_to):
                if trn_id == 1:
                    raise RuntimeError("Invalid column name 'TrE_Cena'")
                return super().get_invoice(trn_id, date_from, date_to)

        class HappyGateway(RecordingGateway):
            def wait_for_invoice(self, session, ref, timeout=90):
                class Status:
                    class status:
                        code = 200
                        description = "Sukces"
                        details = None
                    ksef_number = "5265877635-20260810-ABCDEF000000-00"
                return Status()

            def fetch_upo(self, session_ref, invoice_ref):
                return b"<UPO/>"

        gateway = HappyGateway()
        svc, sent = TestSendServiceRejectsDraft()._service(tmp_path, gateway, good)
        svc.reader = FlakyReader(good)

        svc._send_batch_inner([1, 77], date(2026, 8, 1), date(2026, 8, 31))

        failed = sent.get("firma_a", 1, "test")
        assert failed["status"] == "error"
        assert "TrE_Cena" in failed["error_detail"]
        assert sent.get("firma_a", 77, "test")["status"] == "accepted"
        assert len(gateway.sent_invoices) == 1

    def test_failure_after_submit_says_status_unknown(self, tmp_path):
        class TimeoutGateway(RecordingGateway):
            def wait_for_invoice(self, session, ref, timeout=90):
                raise TimeoutError("brak odpowiedzi")

        svc, sent = TestSendServiceRejectsDraft()._service(
            tmp_path, TimeoutGateway(), make_invoice(bufor=False)
        )
        svc._send_batch_inner([77], date(2026, 8, 1), date(2026, 8, 31))

        row = sent.get("firma_a", 77, "test")
        assert row["status"] == "error"
        assert "status nieznany" in row["error_detail"]
