"""Pobieranie faktur zakupu: kolejka XML na zadanie i limity KSeF (16 pobran/min, 64/h)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from ksef2.core.exceptions import KSeFAuthError, KSeFRateLimitError

from app.ksef.downloader import (
    AUTH_BACKOFF,
    ERROR_BACKOFF,
    DownloadService,
    blocked_reason,
    set_queue_paused,
)
from app.ksef.models import MetadataPage, PurchaseInvoiceMeta
from app.ksef.ratelimit import DOWNLOAD_ENDPOINTS, LIMITS, RateLimiter, RateLimitExceeded
from app.state import db as state_db
from app.state.repo import AuditRepo, DownloadRepo, SyncStateRepo, XmlQueueRepo
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
        self.server_refuses_with: Exception | None = None
        self.login_fails_with: Exception | None = None
        self.queries: list[int] = []
        self.xml = _built_invoice_xml()

    def ensure_login(self) -> None:
        if self.login_fails_with is not None:
            raise self.login_fails_with

    def query_purchase_metadata(self, date_from, date_to=None, page_offset=0):
        self.queries.append(page_offset)
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
    queue = XmlQueueRepo(conn)

    def build(invoices: int):
        gateway = FakeGateway(limiter, invoices)
        service = DownloadService(
            company_id="firma_a", company=None, env="test", gateway=gateway, limiter=limiter,
            downloads=downloads, queue=queue, sync_state=SyncStateRepo(conn),
            audit=AuditRepo(conn),
            invoice_store=tmp_path / "invoices", backfill_start="2026-08-01",
        )
        return service, gateway

    return SimpleNamespace(
        conn=conn, downloads=downloads, queue=queue, limiter=limiter, build=build
    )


def _minute_passes(conn) -> None:
    """Przesuwa zuzyte jednostki o 61 s wstecz - okno minutowe sie zwalnia, godzinowe nie."""
    for row in conn.execute("SELECT rowid, ts FROM rate_limits").fetchall():
        moved = datetime.fromisoformat(row["ts"]) - timedelta(seconds=61)
        conn.execute("UPDATE rate_limits SET ts=? WHERE rowid=?", (moved.isoformat(), row["rowid"]))
    conn.commit()


def _statuses(downloads) -> list[tuple[str, bool]]:
    return [(r["status"], bool(r["xml_path"])) for r in downloads.list_for("firma_a", "test")]


def _unblock(conn) -> None:
    conn.execute("DELETE FROM sync_state WHERE key LIKE 'download_blocked_until%'")
    conn.commit()


def _ids(env, *numbers: int) -> list[int]:
    """Identyfikatory wierszy faktur FA/<n> w podanej kolejnosci."""
    by_number = {r["invoice_number"]: r["id"] for r in env.downloads.list_for("firma_a", "test")}
    return [by_number[f"FA/{n}"] for n in numbers]


def _all_ids(env) -> list[int]:
    return [r["id"] for r in env.downloads.pending_xml("firma_a", "test")]


def _queue_order(env) -> list[str]:
    return [item["invoice_number"] for item in env.queue.items("firma_a", "test")]


def test_sync_stores_metadata_only_and_downloads_nothing(env):
    service, gateway = env.build(40)
    stats = service.sync()
    assert stats["new"] == 40
    assert stats["pending"] == 40
    assert gateway.downloaded == []
    assert _statuses(env.downloads) == [("new", False)] * 40
    # bez wskazania faktur kolejka jest pusta - w tle nic sie nie pobierze
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 0}
    assert gateway.downloaded == []


def test_only_enqueued_invoices_are_downloaded(env):
    service, gateway = env.build(10)
    service.sync()
    assert service.enqueue(_ids(env, 7, 2, 5)) == {"added": 3, "moved": 0, "queued": 3}
    assert service.process_queue() == {"fetched": 3, "failed": 0, "queued": 0}
    assert [n.split("-")[2].lstrip("0") for n in gateway.downloaded] == ["7", "2", "5"]
    assert sorted(_statuses(env.downloads)) == sorted([("new", True)] * 3 + [("new", False)] * 7)


def test_enqueue_skips_invoices_with_xml_and_those_already_queued(env):
    service, _ = env.build(4)
    service.sync()
    service.enqueue(_ids(env, 0))
    service.process_queue()
    assert service.enqueue(_ids(env, 0, 1, 2)) == {"added": 2, "moved": 0, "queued": 2}
    assert service.enqueue(_ids(env, 1, 2, 3)) == {"added": 1, "moved": 0, "queued": 3}
    assert _queue_order(env) == ["FA/1", "FA/2", "FA/3"]


def test_enqueue_at_front_goes_before_waiting_items(env):
    service, _ = env.build(6)
    service.sync()
    service.enqueue(_ids(env, 0, 1, 2))
    # FA/2 juz czeka - przechodzi na poczatek razem z nowymi, w podanej kolejnosci
    assert service.enqueue(_ids(env, 5, 2, 4), front=True) == {
        "added": 2, "moved": 1, "queued": 5,
    }
    assert _queue_order(env) == ["FA/5", "FA/2", "FA/4", "FA/0", "FA/1"]


def test_queue_drains_minute_by_minute_until_done(env):
    service, gateway = env.build(40)
    service.sync()
    service.enqueue(_all_ids(env))
    assert service.process_queue() == {"fetched": PER_MINUTE, "failed": 0, "queued": 40 - PER_MINUTE}
    rounds = 0
    while env.queue.count("firma_a", "test"):
        _minute_passes(env.conn)
        _unblock(env.conn)
        service.process_queue()
        rounds += 1
        assert rounds < 10
    assert len(set(gateway.downloaded)) == 40
    assert all(r["net"] == 150.0 for r in env.downloads.list_for("firma_a", "test"))


def test_queue_waits_for_the_limit_window_instead_of_retrying(env):
    service, gateway = env.build(40)
    service.sync()
    service.enqueue(_all_ids(env))
    service.process_queue()
    before = len(gateway.downloaded)
    assert not service.queue_ready()
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 40 - PER_MINUTE}
    assert len(gateway.downloaded) == before
    assert env.limiter.remaining("firma_a", "invoice_download_hour") == PER_HOUR - PER_MINUTE
    # pozycja, na ktorej skonczyl sie limit, wrocila do kolejki jako czekajaca
    assert {item["state"] for item in env.queue.items("firma_a", "test")} == {"queued"}


def test_hourly_limit_is_not_exceeded(env):
    service, gateway = env.build(PER_HOUR + 20)
    service.sync()
    service.enqueue(_all_ids(env))
    for _ in range(12):
        _minute_passes(env.conn)
        _unblock(env.conn)
        service.process_queue()
    assert len(gateway.downloaded) == PER_HOUR
    assert env.queue.count("firma_a", "test") == 20


def test_server_429_keeps_invoice_queued_and_pauses_downloads(env):
    service, gateway = env.build(3)
    gateway.server_refuses_with = KSeFRateLimitError(33, "KSeF rate limit exceeded")
    service.sync()
    service.enqueue(_all_ids(env))
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 3}
    assert _statuses(env.downloads) == [("new", False)] * 3
    # jedno odbicie od serwera wystarczy - kolejnych faktur nie probujemy w tej rundzie
    assert env.limiter.remaining("firma_a", "invoice_download_minute") == PER_MINUTE - 1
    blocked = service._blocked_until()
    assert timedelta(seconds=30) < blocked - datetime.now(UTC) < timedelta(seconds=40)

    gateway.server_refuses_with = None
    _unblock(env.conn)
    assert service.process_queue() == {"fetched": 3, "failed": 0, "queued": 0}


def test_failed_download_leaves_the_rest_of_the_queue_alone(env):
    service, gateway = env.build(3)
    service.sync()
    service.enqueue(_ids(env, 0, 1, 2))
    gateway.server_refuses_with = ConnectionError("brak sieci")
    assert service.process_queue() == {"fetched": 0, "failed": 1, "queued": 2}
    failed = env.downloads.get(_ids(env, 0)[0])
    assert (failed["status"], failed["error_detail"]) == ("error", "brak sieci")
    # przerwa po bledzie: awaria sieci nie oznacza bledem calej kolejki w kilka sekund
    wait = service._blocked_until() - datetime.now(UTC)
    assert ERROR_BACKOFF - timedelta(seconds=5) < wait <= ERROR_BACKOFF
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 2}

    gateway.server_refuses_with = None
    _unblock(env.conn)
    # faktura z bledem nadal nie ma XML - mozna ja wstawic do kolejki ponownie
    assert service.enqueue(_ids(env, 0))["added"] == 1
    assert service.process_queue() == {"fetched": 3, "failed": 0, "queued": 0}
    assert _statuses(env.downloads) == [("new", True)] * 3


def test_auth_error_keeps_invoices_queued_and_explains_the_pause(env):
    service, gateway = env.build(3)
    service.sync()
    service.enqueue(_all_ids(env))
    gateway.server_refuses_with = KSeFAuthError(401, "Wymagane jest uwierzytelnienie.")
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 3}
    # to nie wina faktury: zadna nie dostaje bledu ani nie wypada z kolejki
    assert _statuses(env.downloads) == [("new", False)] * 3
    assert {item["state"] for item in env.queue.items("firma_a", "test")} == {"queued"}
    wait = service._blocked_until() - datetime.now(UTC)
    assert AUTH_BACKOFF - timedelta(seconds=5) < wait <= AUTH_BACKOFF
    assert "odmowil dostepu (401)" in blocked_reason(service.sync_state, "firma_a", "test")

    gateway.server_refuses_with = None
    _unblock(env.conn)
    assert service.process_queue() == {"fetched": 3, "failed": 0, "queued": 0}


def test_login_failure_keeps_invoices_queued(env):
    service, gateway = env.build(3)
    service.sync()
    service.enqueue(_all_ids(env))
    gateway.login_fails_with = RuntimeError("Brak sekretu 'ksef-token:firma_a:test'")
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 3}
    assert _statuses(env.downloads) == [("new", False)] * 3
    assert gateway.downloaded == []
    assert env.limiter.remaining("firma_a", "invoice_download_minute") == PER_MINUTE
    reason = blocked_reason(service.sync_state, "firma_a", "test")
    assert "logowanie do KSeF nie powiodlo sie" in reason and "Brak sekretu" in reason


def test_interrupted_file_write_is_not_taken_for_a_downloaded_invoice(env, tmp_path):
    service, gateway = env.build(1)
    service.sync()
    service.enqueue(_all_ids(env))
    service.process_queue()
    stored = list((tmp_path / "invoices" / "firma_a").iterdir())
    assert [p.suffix for p in stored] == [".xml"]  # plik tymczasowy nie zostaje
    assert stored[0].read_bytes() == gateway.xml


def test_sync_pages_through_invoices_sharing_one_storage_date(env):
    service, gateway = env.build(0)
    same_moment = "2026-08-03T10:00:00+00:00"

    def page(start: int, count: int, has_more: bool) -> MetadataPage:
        metas = [_meta(i) for i in range(start, start + count)]
        for meta in metas:
            meta.storage_date = same_moment
        return MetadataPage(invoices=metas, has_more=has_more)

    pages = {0: page(0, 3, True), 1: page(3, 3, True), 2: page(6, 2, False)}
    gateway.query_purchase_metadata = lambda date_from, date_to=None, page_offset=0: (
        gateway.queries.append(page_offset) or pages[page_offset]
    )
    stats = service.sync()
    # po pierwszej stronie kursor staje na wspolnej dacie; ponowione zapytanie zwraca te sama
    # strone (3 znane), wiec dalej idziemy numerami stron zamiast pytac w kolko o to samo
    assert gateway.queries == [0, 0, 1, 2]
    assert stats["new"] == 8 and stats["known"] == 3


def test_sync_keeps_asking_from_cursor_when_dates_advance(env):
    service, gateway = env.build(0)
    first = MetadataPage(invoices=[_meta(i) for i in range(3)], has_more=True)
    second = MetadataPage(invoices=[_meta(i) for i in range(2, 5)], has_more=False)
    answers = [first, second]
    gateway.query_purchase_metadata = lambda date_from, date_to=None, page_offset=0: (
        gateway.queries.append(page_offset) or (answers.pop(0) if answers else MetadataPage())
    )
    stats = service.sync()
    # dalsze zapytania to kolejne (puste) okna dat - zawsze od pierwszej strony
    assert set(gateway.queries) == {0} and len(gateway.queries) >= 2
    assert stats["new"] == 5 and stats["known"] == 1


def test_connection_error_keeps_invoices_queued(env):
    service, gateway = env.build(2)
    service.sync()
    service.enqueue(_all_ids(env))
    gateway.server_refuses_with = httpx.ConnectError("brak sieci")
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 2}
    assert _statuses(env.downloads) == [("new", False)] * 2
    assert "brak polaczenia z KSeF" in blocked_reason(service.sync_state, "firma_a", "test")


def test_limit_pause_clears_earlier_error_reason(env):
    service, gateway = env.build(40)
    service.sync()
    service.enqueue(_all_ids(env))
    gateway.server_refuses_with = httpx.ConnectError("brak sieci")
    service.process_queue()
    gateway.server_refuses_with = None
    _unblock(env.conn)
    service.process_queue()  # konczy sie na limicie minutowym
    assert service._blocked_until() is not None
    assert blocked_reason(service.sync_state, "firma_a", "test") == ""


def test_stopped_queue_downloads_nothing_until_resumed(env):
    service, gateway = env.build(3)
    service.sync()
    service.enqueue(_all_ids(env))
    set_queue_paused(service.sync_state, "firma_a", "test", True)
    assert not service.queue_ready()
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 3}
    assert gateway.downloaded == []
    set_queue_paused(service.sync_state, "firma_a", "test", False)
    assert service.process_queue() == {"fetched": 3, "failed": 0, "queued": 0}


def test_stop_takes_effect_after_the_invoice_being_downloaded(env):
    service, gateway = env.build(3)
    service.sync()
    service.enqueue(_all_ids(env))
    download = gateway.download_invoice

    def stop_during_first_download(ksef_number):
        set_queue_paused(service.sync_state, "firma_a", "test", True)
        return download(ksef_number)

    gateway.download_invoice = stop_during_first_download
    # trwajace pobranie dobiega konca, nastepne juz nie rusza
    assert service.process_queue() == {"fetched": 1, "failed": 0, "queued": 2}


def test_priority_change_decides_what_is_downloaded_next(env):
    service, gateway = env.build(5)
    service.sync()
    service.enqueue(_ids(env, 0, 1, 2, 3, 4))
    (three,) = _ids(env, 3)
    (zero,) = _ids(env, 0)
    assert env.queue.move("firma_a", "test", three, "top")
    assert _queue_order(env) == ["FA/3", "FA/0", "FA/1", "FA/2", "FA/4"]
    assert env.queue.move("firma_a", "test", zero, "down")
    assert _queue_order(env) == ["FA/3", "FA/1", "FA/0", "FA/2", "FA/4"]
    assert env.queue.move("firma_a", "test", zero, "up")
    assert env.queue.move("firma_a", "test", zero, "bottom")
    assert _queue_order(env) == ["FA/3", "FA/1", "FA/2", "FA/4", "FA/0"]
    assert not env.queue.move("firma_a", "test", three, "up")  # juz pierwsza
    assert not env.queue.move("firma_a", "test", zero, "down")  # juz ostatnia
    service.process_queue()
    assert [n.split("-")[2].lstrip("0") or "0" for n in gateway.downloaded] == [
        "3", "1", "2", "4", "0",
    ]


def test_invoice_being_downloaded_cannot_be_moved_or_removed(env):
    service, _ = env.build(3)
    service.sync()
    service.enqueue(_ids(env, 0, 1, 2))
    current = env.queue.claim_next("firma_a", "test")
    (two,) = _ids(env, 2)
    assert not env.queue.move("firma_a", "test", current["invoice_id"], "bottom")
    assert not env.queue.remove_queued("firma_a", "test", current["invoice_id"])
    # "na poczatek" i "wyzej" dotycza tylko czekajacych - pobierana zostaje pierwsza
    assert env.queue.move("firma_a", "test", two, "top")
    assert env.queue.move("firma_a", "test", _ids(env, 1)[0], "up")
    assert not env.queue.move("firma_a", "test", _ids(env, 1)[0], "up")
    assert _queue_order(env) == ["FA/0", "FA/1", "FA/2"]
    assert [i["state"] for i in env.queue.items("firma_a", "test")] == [
        "downloading", "queued", "queued",
    ]
    # ponowne dodanie "na poczatek" tez jej nie rusza
    service.enqueue(_ids(env, 0, 2), front=True)
    assert env.queue.items("firma_a", "test")[0]["state"] == "downloading"


def test_purge_empties_the_queue_and_keeps_downloaded_xml(env):
    service, gateway = env.build(5)
    service.sync()
    service.enqueue(_ids(env, 0))
    service.process_queue()
    service.enqueue(_all_ids(env))
    assert env.queue.purge("firma_a", "test") == 4
    assert service.process_queue() == {"fetched": 0, "failed": 0, "queued": 0}
    assert len(gateway.downloaded) == 1
    assert sorted(_statuses(env.downloads)) == sorted([("new", True)] + [("new", False)] * 4)


def test_interrupted_download_returns_to_queue_after_restart(env):
    service, _ = env.build(2)
    service.sync()
    service.enqueue(_all_ids(env))
    env.queue.claim_next("firma_a", "test")  # proces padl w trakcie pobierania
    env.queue.release_all()
    assert service.process_queue() == {"fetched": 2, "failed": 0, "queued": 0}


def test_queues_of_companies_are_separate(env):
    service, _ = env.build(2)
    service.sync()
    env.downloads.insert_if_new("firma_b", "test", _meta(9).ksef_number, invoice_number="FA/9")
    other = env.downloads.list_for("firma_b", "test")[0]["id"]
    # cudzej faktury nie da sie wstawic do kolejki firmy
    assert service.enqueue([other, *_all_ids(env)])["added"] == 2
    env.queue.add("firma_b", "test", [other])
    assert env.queue.purge("firma_a", "test") == 2
    assert env.queue.count("firma_b", "test") == 1


def test_rows_marked_as_error_by_older_version_can_be_enqueued(env):
    service, _ = env.build(0)
    env.downloads.insert_if_new("firma_a", "test", _meta(1).ksef_number, invoice_number="FA/1")
    row_id = env.downloads.list_for("firma_a", "test")[0]["id"]
    env.downloads.set_status(
        row_id, "error",
        error_detail="RATE_LIMIT_ERROR/429: KSeF rate limit exceeded: Przekroczono limit 16",
    )
    service.enqueue([row_id])
    assert service.process_queue() == {"fetched": 1, "failed": 0, "queued": 0}
    row = env.downloads.get(row_id)
    assert (row["status"], row["error_detail"], bool(row["xml_path"])) == ("new", None, True)


def test_unparsable_invoice_is_not_downloaded_again(env):
    service, gateway = env.build(1)
    gateway.xml = b"<not-an-invoice/>"
    service.sync()
    service.enqueue(_all_ids(env))
    service.process_queue()
    row = env.downloads.list_for("firma_a", "test")[0]
    assert row["status"] == "error" and row["xml_path"]
    assert env.downloads.pending_xml("firma_a", "test") == []
    assert service.enqueue([row["id"]])["added"] == 0


def test_acquire_all_consumes_nothing_when_one_budget_is_spent(env):
    for _ in range(PER_HOUR):
        env.limiter.acquire("firma_a", "invoice_download_hour")
    with pytest.raises(RateLimitExceeded):
        env.limiter.acquire_all("firma_a", DOWNLOAD_ENDPOINTS)
    assert env.limiter.remaining("firma_a", "invoice_download_minute") == PER_MINUTE
