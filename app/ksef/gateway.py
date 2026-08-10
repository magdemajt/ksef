"""KsefGateway - adapter nad biblioteka ksef2.

Jedyny modul w aplikacji importujacy ksef2. Kazda metoda zwraca typy aplikacji
(app.ksef.models) albo proste wartosci - nigdy typy biblioteki, z wyjatkiem
uchwytu sesji online (opaque handle przekazywany z powrotem do gatewaya).
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path

import ksef2
from cryptography.hazmat.primitives.serialization import pkcs12
from ksef2.clients.authenticated import AuthenticatedClient
from ksef2.clients.online import OnlineSessionClient
from ksef2.domain.models.invoices import InvoicesFilter
from ksef2.domain.models.pagination import InvoiceMetadataParams
from ksef2.domain.models.session import FormSchema

from app.config import AuthMethod, CompanyConfig, KsefEnv
from app.ksef.models import MetadataPage, PurchaseInvoiceMeta
from app.ksef.ratelimit import RateLimiter
from app.secrets import SecretStore

logger = logging.getLogger(__name__)

_ENV_MAP = {
    KsefEnv.TEST: ksef2.Environment.TEST,
    KsefEnv.DEMO: ksef2.Environment.DEMO,
    KsefEnv.PROD: ksef2.Environment.PRODUCTION,
}

# Minimalny odstep miedzy pobraniami pojedynczych faktur (limit KSeF: 8 req/s)
_DOWNLOAD_SPACING_S = 0.15


class KsefAuthConfigError(RuntimeError):
    pass


class KsefGateway:
    def __init__(
        self,
        company_id: str,
        company: CompanyConfig,
        env: KsefEnv,
        secrets: SecretStore,
        limiter: RateLimiter,
    ):
        self.company_id = company_id
        self.company = company
        self.env = env
        self.secrets = secrets
        self.limiter = limiter
        self._client: ksef2.Client | None = None
        self._authed: AuthenticatedClient | None = None
        self._last_download_ts = 0.0

    # -- polaczenie / auth --

    def client(self) -> ksef2.Client:
        if self._client is None:
            self._client = ksef2.Client(_ENV_MAP[self.env])
        return self._client

    def authed(self) -> AuthenticatedClient:
        if self._authed is None:
            self._authed = self._authenticate()
        return self._authed

    def reauthenticate(self) -> None:
        self._authed = None
        self.authed()

    def _authenticate(self) -> AuthenticatedClient:
        method = self.company.auth_method
        nip = self.company.nip
        auth = self.client().authentication
        logger.info(
            "Uwierzytelnianie w KSeF (%s) firma=%s metoda=%s", self.env, self.company_id, method
        )
        if method == AuthMethod.TOKEN:
            token = self.secrets.get(SecretStore.ksef_token_key(self.company_id, self.env))
            return auth.with_token(ksef_token=token, nip=nip)
        if method == AuthMethod.KSEF_CERT:
            cert_path = Path(self.company.ksef_cert_path).expanduser()
            if not self.company.ksef_cert_path or not cert_path.exists():
                raise KsefAuthConfigError(
                    f"auth_method=ksef_cert wymaga istniejacego pliku .p12 "
                    f"(ksef_cert_path={self.company.ksef_cert_path!r})"
                )
            password = self.secrets.get_optional(SecretStore.p12_pass_key(self.company_id))
            key, cert, _chain = pkcs12.load_key_and_certificates(
                cert_path.read_bytes(), password.encode() if password else None
            )
            if key is None or cert is None:
                raise KsefAuthConfigError(f"Plik {cert_path} nie zawiera klucza lub certyfikatu")
            return auth.with_xades(nip=nip, cert=cert, private_key=key)
        raise KsefAuthConfigError(f"Nieobslugiwana metoda auth: {method}")

    def auth_ok(self) -> bool:
        """Test uwierzytelnienia (na potrzeby /settings)."""
        self.authed()
        return True

    # -- bootstrap srodowiska testowego --

    def testdata_create_person(self, nip: str, pesel: str, description: str) -> None:
        if self.env != KsefEnv.TEST:
            raise RuntimeError("Endpointy /testdata dzialaja tylko na srodowisku TEST")
        self.client().testdata.create_person(nip=nip, pesel=pesel, description=description)

    def test_authenticate_with_test_certificate(self, nip: str) -> AuthenticatedClient:
        """Uwierzytelnienie self-signed certyfikatem (tylko TEST) - do mintowania tokena."""
        if self.env != KsefEnv.TEST:
            raise RuntimeError("Certyfikat testowy dziala tylko na srodowisku TEST")
        return self.client().authentication.with_test_certificate(nip=nip)

    def mint_ksef_token(self, authed: AuthenticatedClient | None = None) -> str:
        """Generuje token KSeF (wymaga sesji uwierzytelnionej XAdES-em)."""
        a = authed or self.authed()
        resp = a.tokens.generate(
            permissions=["invoice_read", "invoice_write"],
            description="ksef-bridge",
        )
        return resp.token

    # -- sesja online (wysylka) --

    def open_online_session(self) -> OnlineSessionClient:
        return self.authed().online_session(form_code=FormSchema.FA3)

    def send_invoice(self, session: OnlineSessionClient, xml: bytes) -> str:
        self.limiter.acquire(self.company_id, "invoice_send")
        return session.send_invoice(invoice_xml=xml).reference_number

    def wait_for_invoice(self, session: OnlineSessionClient, invoice_ref: str, timeout: float = 90):
        try:
            return session.wait_for_invoice_ready(
                invoice_reference_number=invoice_ref, timeout=timeout
            )
        except ksef2.KSeFSessionError:
            # ksef2 rzuca przy statusach bledu (440 duplikat, 450 walidacja itd.) -
            # pobieramy autorytatywny status, wolajacy mapuje kod na stan aplikacji
            return session.get_invoice_status(invoice_reference_number=invoice_ref)

    def get_invoice_status(self, session: OnlineSessionClient, invoice_ref: str):
        return session.get_invoice_status(invoice_reference_number=invoice_ref)

    def close_session(self, session: OnlineSessionClient) -> None:
        session.close()

    def get_invoice_upo(self, session: OnlineSessionClient, invoice_ref: str) -> bytes:
        return session.get_invoice_upo_by_reference(invoice_reference_number=invoice_ref)

    def fetch_upo(self, session_ref: str, invoice_ref: str) -> bytes:
        """Pobiera UPO faktury bez uchwytu sesji (np. po restarcie aplikacji).

        UPO jest dostepne dopiero jakis czas po zamknieciu sesji - 404/400 przed
        wygenerowaniem; wyjatek propagujemy, wolajacy decyduje o retry.
        """
        import httpx

        token = self.authed().auth_tokens.access_token.token
        url = f"{self.env.base_url}/sessions/{session_ref}/invoices/{invoice_ref}/upo"
        resp = httpx.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        resp.raise_for_status()
        return resp.content

    def session_ref(self, session: OnlineSessionClient) -> str:
        return session.get_state().reference_number

    # -- pobieranie zakupow --

    def query_purchase_metadata(
        self, date_from: datetime, date_to: datetime | None = None, page_offset: int = 0
    ) -> MetadataPage:
        """Jedna strona metadanych faktur zakupu (my = nabywca). Zuzywa 1/20 budzetu godzinowego."""
        self.limiter.acquire(self.company_id, "invoice_metadata_or_export")
        filters = InvoicesFilter(
            role="buyer",
            date_type="permanent_storage",
            date_from=date_from,
            date_to=date_to or datetime.now(UTC),
            restrict_to_permanent_storage_hwm_date=True,
            amount_type="brutto",
        )
        params = InvoiceMetadataParams(sort_order="asc", page_offset=page_offset, page_size=250)
        resp = self.authed().invoices.query_metadata(filters=filters, params=params)
        page = MetadataPage(
            has_more=bool(getattr(resp, "has_more", False)),
            is_truncated=bool(getattr(resp, "is_truncated", False)),
            hwm_date=getattr(resp, "permanent_storage_hwm_date", None),
        )
        for m in resp.invoices:
            page.invoices.append(
                PurchaseInvoiceMeta(
                    ksef_number=m.ksef_number,
                    invoice_number=m.invoice_number,
                    seller_nip=getattr(m.seller, "nip", "") or "",
                    seller_name=getattr(m.seller, "name", "") or "",
                    issue_date=str(m.issue_date or ""),
                    storage_date=str(m.permanent_storage_date or ""),
                    net=m.net_amount,
                    vat=m.vat_amount,
                    gross=m.gross_amount,
                    currency=str(m.currency) if m.currency else None,
                )
            )
        return page

    def download_invoice(self, ksef_number: str) -> bytes:
        # limit 8/s - wymuszamy odstep miedzy wywolaniami
        elapsed = time.monotonic() - self._last_download_ts
        if elapsed < _DOWNLOAD_SPACING_S:
            time.sleep(_DOWNLOAD_SPACING_S - elapsed)
        self._last_download_ts = time.monotonic()
        return self.authed().invoices.download_invoice(ksef_number=ksef_number)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._authed = None
