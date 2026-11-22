"""Pobieranie faktur zakupu: kolejka XML i limity KSeF (16 pobran/min, 64/h)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ksef2.core.exceptions import KSeFRateLimitError

from app.ksef.downloader import DownloadService
from app.ksef.models import MetadataPage, PurchaseInvoiceMeta
from app.ksef.ratelimit import DOWNLOAD_ENDPOINTS, LIMITS, RateLimiter, RateLimitExceeded
from app.state import db as state_db
from app.state.repo import AuditRepo, DownloadRepo, SyncStateRepo
from tests.unit.test_parser_and_validator import _built_invoice_xml

PER_MINUTE = LIMITS["invoice_download_minute"][0]
PER_HOUR = LIMITS["invoice_download_hour"][0]


def _meta(i: int) -> PurchaseInvoiceMeta:
    return PurchaseInvoiceMeta(
        ksef_number=f"5265877635-20260803-{i:012d}-33", invoice_number=f"FA/{i}",
        seller_nip="5265877635", seller_name="Sprzedawca", issue_date="2026-08-01",
        storage_date=f"2026-08-03T10:{i // 60:02d}:{i % 60:02d}+00:00",
        net=150.0, vat=27.0, gross=177.0, currency="PLN",
    )


class FakeGateway:
    """KSeF z jedna strona metadanych; pobrania pilnuje ten sam limiter co w aplikacji."""

    def __init__(self, limiter: RateLimiter, invoices: int):
        self.limiter = limiter
        self.page = MetadataPage(invoices=[_meta(i) for i in range(invoices)])
        self.downloaded: list[str] = []
        self.server_refuses_with: KSeFRateLimitError | None = None
        self.xml = _built_invoice_xml()

    def query_purchase_metadata(self, date_from, date_to=None, page_offset=0):
        return self.page

    def download_invoice(self, ksef_number: str) -> bytes:
        self.limiter.acquire_all("firma_a", DOWNLOAD_ENDPOINTS)
        if self.server_refuses_with is not None:
            raise self.server_refuses_with
        self.downloaded.append(ksef_number)
        return self.xml


@pytest.fixture
def env(tmp_path):
    conn = state_db.connect(tmp_path / "state.db")
    limiter = RateLimiter(conn)
    downloads = DownloadRepo(conn)

    def build(invoices: int):
        gateway = FakeGateway(limiter, invoices)
        service = DownloadService(
            company_id="firma_a", company=None, env="test", gateway=gateway, limiter=limiter,
            downloads=downloads, sync_state=SyncStateRepo(conn), audit=AuditRepo(conn),
            invoice_store=tmp_path / "invoices", backfill_start="2026-08-01",
        )
        return service, gateway

    return SimpleNamespace(conn=conn, downloads=downloads, limiter=limiter, build=build)


def _minute_passes(conn) -> None:
    """Przesuwa zuzyte jednostki o 61 s wstecz - okno minutowe sie zwalnia, godzinowe nie."""
    for row in conn.execute("SELECT rowid, ts FROM rate_limits").fetchall():
        moved = datetime.fromisoformat(row["ts"]) - timedelta(seconds=61)
        conn.execute("UPDATE rate_limits SET ts=? WHERE rowid=?", (moved.isoformat(), row["rowid"]))
    conn.commit()


def _statuses(downloads) -> list[tuple[str, bool]]:
    return [(r["status"], bool(r["xml_path"])) for r in downloads.list_for("firma_a", "test")]


def test_sync_downloads_up_to_minute_limit_and_queues_the_rest(env):
    service, gateway = env.build(40)
    stats = service.sync()
    assert stats["new"] == 40
    assert stats["pending"] == 40 - PER_MINUTE
    assert len(gateway.downloaded) == PER_MINUTE
    # nic nie jest bledem: reszta czeka jako "new" bez pliku XML
    assert sorted(_statuses(env.downloads)) == sorted(
        [("new", True)] * PER_MINUTE + [("new", False)] * (40 - PER_MINUTE)
    )


def test_queue_drains_minute_by_minute_until_done(env):
    service, gateway = env.build(40)
    service.sync()
    rounds = 0
    while env.downloads.pending_xml("firma_a", "test"):
        _minute_passes(env.conn)
        env.conn.execute("DELETE FROM sync_state WHERE key LIKE 'download_blocked_until%'")
        env.conn.commit()
        service.fetch_pending()
        rounds += 1
        assert rounds < 10
    assert len(set(gateway.downloaded)) == 40
    assert all(r["net"] == 150.0 for r in env.downloads.list_for("firma_a", "test"))


def test_queue_waits_for_the_limit_window_instead_of_retrying(env):
    service, gateway = env.build(40)
    service.sync()
    before = len(gateway.downloaded)
    assert service.fetch_pending() == {"fetched": 0, "pending": 40 - PER_MINUTE}
    assert len(gateway.downloaded) == before
    assert env.limiter.remaining("firma_a", "invoice_download_hour") == PER_HOUR - PER_MINUTE


def test_hourly_limit_is_not_exceeded(env):
    service, gateway = env.build(PER_HOUR + 20)
    service.sync()
    for _ in range(12):
        _minute_passes(env.conn)
        env.conn.execute("DELETE FROM sync_state WHERE key LIKE 'download_blocked_until%'")
        env.conn.commit()
        service.fetch_pending()
    assert len(gateway.downloaded) == PER_HOUR
    assert len(env.downloads.pending_xml("firma_a", "test")) == 20


def test_server_429_keeps_invoice_queued_and_pauses_downloads(env):
    service, gateway = env.build(3)
    gateway.server_refuses_with = KSeFRateLimitError(33, "KSeF rate limit exceeded")
    stats = service.sync()
    assert stats["pending"] == 3
    assert _statuses(env.downloads) == [("new", False)] * 3
    # jedno odbicie od serwera wystarczy - kolejnych faktur nie probujemy w tej rundzie
    assert env.limiter.remaining("firma_a", "invoice_download_minute") == PER_MINUTE - 1
    blocked = service._blocked_until()
    assert timedelta(seconds=30) < blocked - datetime.now(UTC) < timedelta(seconds=40)

    gateway.server_refuses_with = None
    service.sync_state.set("firma_a", "download_blocked_until:test", datetime.now(UTC).isoformat())
    assert service.fetch_pending() == {"fetched": 3, "pending": 0}


def test_rows_marked_as_error_by_older_version_are_picked_up(env):
    service, _ = env.build(0)
    env.downloads.insert_if_new("firma_a", "test", _meta(1).ksef_number, invoice_number="FA/1")
    row_id = env.downloads.list_for("firma_a", "test")[0]["id"]
    env.downloads.set_status(
        row_id, "error",
        error_detail="RATE_LIMIT_ERROR/429: KSeF rate limit exceeded: Przekroczono limit 16",
    )
    assert service.fetch_pending() == {"fetched": 1, "pending": 0}
    row = env.downloads.get(row_id)
    assert (row["status"], row["error_detail"], bool(row["xml_path"])) == ("new", None, True)


def test_unparsable_invoice_is_not_downloaded_again(env):
    service, gateway = env.build(1)
    gateway.xml = b"<not-an-invoice/>"
    service.sync()
    row = env.downloads.list_for("firma_a", "test")[0]
    assert row["status"] == "error" and row["xml_path"]
    assert env.downloads.pending_xml("firma_a", "test") == []


def test_acquire_all_consumes_nothing_when_one_budget_is_spent(env):
    for _ in range(PER_HOUR):
        env.limiter.acquire("firma_a", "invoice_download_hour")
    with pytest.raises(RateLimitExceeded):
        env.limiter.acquire_all("firma_a", DOWNLOAD_ENDPOINTS)
    assert env.limiter.remaining("firma_a", "invoice_download_minute") == PER_MINUTE
