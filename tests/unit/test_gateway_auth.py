"""Bramka KSeF: ponowne logowanie, gdy token dostepowy wygasl (aplikacja dziala godzinami)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ksef2.core.exceptions import KSeFAuthError

from app.ksef.gateway import KsefGateway


class FakeAuthed:
    """Uwierzytelniony klient ksef2: token z terminem waznosci, pobranie moze dostac 401/403."""

    def __init__(self, valid_for: timedelta, rejects_with: int | None = None):
        self.auth_tokens = SimpleNamespace(
            access_token=SimpleNamespace(token="t", valid_until=datetime.now(UTC) + valid_for)
        )
        self.rejects_with = rejects_with
        self.downloads = 0
        self.invoices = SimpleNamespace(download_invoice=self._download)

    def _download(self, ksef_number: str) -> bytes:
        self.downloads += 1
        if self.rejects_with is not None:
            raise KSeFAuthError(self.rejects_with, "Wymagane jest uwierzytelnienie.")
        return b"<xml/>"


@pytest.fixture
def gateway():
    limiter = SimpleNamespace(acquire_all=lambda *a: None, acquire=lambda *a: None)
    gw = KsefGateway("firma_a", company=None, env="test", secrets=None, limiter=limiter)
    gw.logins = []

    def login(*sessions: FakeAuthed):
        queue = list(sessions)

        def authenticate():
            gw.logins.append(queue[0])
            return queue.pop(0)

        gw._authenticate = authenticate

    gw.login = login
    return gw


def test_valid_token_is_reused(gateway):
    session = FakeAuthed(timedelta(minutes=10))
    gateway.login(session)
    assert gateway.download_invoice("A") == b"<xml/>"
    assert gateway.download_invoice("B") == b"<xml/>"
    assert len(gateway.logins) == 1 and session.downloads == 2


def test_expiring_token_triggers_login_before_the_call(gateway):
    old = FakeAuthed(timedelta(seconds=20), rejects_with=401)  # KSeF by go juz odrzucil
    fresh = FakeAuthed(timedelta(minutes=10))
    gateway.login(old, fresh)
    gateway.authed()
    assert gateway.download_invoice("A") == b"<xml/>"
    assert gateway.logins == [old, fresh]
    assert old.downloads == 0 and fresh.downloads == 1


def test_401_logs_in_again_and_repeats_the_call_once(gateway):
    revoked = FakeAuthed(timedelta(minutes=10), rejects_with=401)
    fresh = FakeAuthed(timedelta(minutes=10))
    gateway.login(revoked, fresh)
    assert gateway.download_invoice("A") == b"<xml/>"
    assert gateway.logins == [revoked, fresh]
    assert revoked.downloads == 1 and fresh.downloads == 1


def test_401_after_fresh_login_is_reported_not_looped(gateway):
    first = FakeAuthed(timedelta(minutes=10), rejects_with=401)
    second = FakeAuthed(timedelta(minutes=10), rejects_with=401)
    gateway.login(first, second)
    with pytest.raises(KSeFAuthError):
        gateway.download_invoice("A")
    assert len(gateway.logins) == 2 and second.downloads == 1


def test_403_is_not_retried(gateway):
    session = FakeAuthed(timedelta(minutes=10), rejects_with=403)
    gateway.login(session)
    with pytest.raises(KSeFAuthError):
        gateway.download_invoice("A")
    assert len(gateway.logins) == 1 and session.downloads == 1
