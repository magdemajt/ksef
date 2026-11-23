import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.ksef.ratelimit import LIMITS, RateLimiter, RateLimitExceeded
from app.state import db as state_db
from app.state.repo import BatchRepo, DownloadRepo, SentRepo


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    return state_db.connect(tmp_path / "state.db")


class TestRateLimiter:
    def test_budget_exhaustion(self, conn):
        limiter = RateLimiter(conn)
        limit, _ = LIMITS["invoice_metadata_or_export"]
        for _ in range(limit):
            limiter.acquire("f1", "invoice_metadata_or_export")
        assert limiter.remaining("f1", "invoice_metadata_or_export") == 0
        with pytest.raises(RateLimitExceeded):
            limiter.acquire("f1", "invoice_metadata_or_export")

    def test_per_company_isolation(self, conn):
        limiter = RateLimiter(conn)
        limiter.acquire("f1", "invoice_metadata_or_export")
        assert limiter.remaining("f2", "invoice_metadata_or_export") == 20

    def test_window_expiry(self, conn):
        limiter = RateLimiter(conn)
        old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        conn.execute(
            "INSERT INTO rate_limits (company_id, endpoint, ts) VALUES (?,?,?)",
            ("f1", "invoice_metadata_or_export", old),
        )
        assert limiter.remaining("f1", "invoice_metadata_or_export") == 20

    def test_survives_reconnect(self, conn, tmp_path):
        RateLimiter(conn).acquire("f1", "invoice_send")
        conn2 = state_db.connect(tmp_path / "state.db")
        assert RateLimiter(conn2).remaining("f1", "invoice_send") == 179


class TestBatchRepo:
    def test_single_use(self, conn):
        repo = BatchRepo(conn)
        token = repo.create("f1", "test", [1, 2], {"brutto": "100"})
        assert repo.consume(token) is not None
        assert repo.consume(token) is None  # drugi raz odrzucony

    def test_expired_rejected(self, conn):
        repo = BatchRepo(conn)
        token = repo.create("f1", "test", [1], {})
        conn.execute(
            "UPDATE pending_batches SET expires_at=? WHERE confirm_token=?",
            ((datetime.now(UTC) - timedelta(minutes=1)).isoformat(), token),
        )
        assert repo.consume(token) is None

    def test_unknown_rejected(self, conn):
        assert BatchRepo(conn).consume("nie-istnieje") is None


class TestSentRepo:
    def test_upsert_and_unique_per_env(self, conn):
        repo = SentRepo(conn)
        repo.upsert("f1", 10, "test", status="queued", numer_pelny="FA/1")
        repo.upsert("f1", 10, "test", status="accepted", ksef_number="X")
        rows = repo.statuses_for("f1", "test")
        assert rows[10]["status"] == "accepted"
        assert rows[10]["numer_pelny"] == "FA/1"
        # inne srodowisko = osobny wpis
        repo.upsert("f1", 10, "prod", status="queued", numer_pelny="FA/1")
        assert len(repo.statuses_for("f1", "prod")) == 1


class TestDownloadRepo:
    def test_insert_if_new_idempotent(self, conn):
        repo = DownloadRepo(conn)
        assert repo.insert_if_new("f1", "test", "KSEF-1", invoice_number="FA/1") is True
        assert repo.insert_if_new("f1", "test", "KSEF-1", invoice_number="FA/1") is False
        assert len(repo.list_for("f1", "test")) == 1

    def test_every_gui_status_is_accepted_by_schema(self, conn):
        """Status spoza CHECK w schema.sql wywala eksport bledem 500 (regresja 0.1.15)."""
        from app.web.routes.purchases import STATUS_LABELS

        repo = DownloadRepo(conn)
        repo.insert_if_new("firma_a", "test", "KSEF-STATUS-1")
        row_id = repo.list_for("firma_a", "test")[0]["id"]
        for status in STATUS_LABELS:
            repo.set_status(row_id, status)
        repo.set_status(row_id, "new", exported_at="2026-11-17T20:00:00+00:00", error_detail=None)
        row = repo.get(row_id)
        assert row["status"] == "new" and row["exported_at"]


class TestSharedConnection:
    """Jedno polaczenie SQLite dla wszystkich watkow aplikacji."""

    def test_confirm_token_is_consumed_by_exactly_one_of_two_concurrent_requests(self, conn):
        batches = BatchRepo(conn)
        for _ in range(30):
            token = batches.create("f1", "test", [1, 2], {})
            start = threading.Barrier(2)
            got = []

            def confirm(token=token, start=start, got=got):
                start.wait()
                got.append(batches.consume(token))

            threads = [threading.Thread(target=confirm) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            assert sum(row is not None for row in got) == 1

    def test_failed_transaction_does_not_roll_back_another_threads_write(self, conn):
        downloads = DownloadRepo(conn)
        downloads.insert_if_new("f1", "test", "K1", invoice_number="FA/1")
        row_id = downloads.list_for("f1", "test")[0]["id"]
        inside = threading.Event()
        release = threading.Event()

        def failing_request():
            try:
                with conn:
                    conn.execute(
                        "UPDATE downloaded_invoices SET seller_name='w trakcie' WHERE id=?",
                        (row_id,),
                    )
                    inside.set()
                    release.wait(2)
                    raise RuntimeError("blad w polowie transakcji")
            except RuntimeError:
                pass

        request = threading.Thread(target=failing_request)
        request.start()
        assert inside.wait(2)
        # watek kolejki zapisuje pobrana fakture, gdy tamta transakcja jeszcze trwa
        worker = threading.Thread(
            target=lambda: downloads.set_status(row_id, "new", xml_path="/x.xml")
        )
        worker.start()
        release.set()
        request.join()
        worker.join()
        row = downloads.get(row_id)
        assert row["xml_path"] == "/x.xml"
        assert row["seller_name"] is None  # nieudana transakcja cofnieta w calosci

    def test_reading_limits_leaves_no_open_transaction(self, conn):
        limiter = RateLimiter(conn)
        limiter.acquire("f1", "invoice_send")
        limiter.remaining("f1", "invoice_send")
        limiter.resets_at("f1", "invoice_send")
        assert not conn.in_transaction
