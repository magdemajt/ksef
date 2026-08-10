"""Stan konfiguracji poczatkowej - zasila konfigurator /setup i przekierowanie z /.

Kompletnosc = sa sekrety SQL + co najmniej jedna prawdziwa firma (NIP inny niz
placeholder 0000000000) + poswiadczenia KSeF dla ktorejkolwiek prawdziwej firmy.
Wyniki testow polaczen sa utrwalane w sync_state (company_id "_setup"), zeby
checklist pokazywal je po restarcie; zmiana serwera/srodowiska je uniewaznia.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

PLACEHOLDER_NIP = "0000000000"
SETUP_SCOPE = "_setup"


@dataclass
class SetupStatus:
    sql_password_set: bool = False
    sql_tested_for: str | None = None  # serwer, dla ktorego test przeszedl
    current_server: str = ""
    real_companies: list[str] = field(default_factory=list)
    placeholder_companies: list[str] = field(default_factory=list)
    ksef_ready: dict[str, bool] = field(default_factory=dict)  # cid -> ma token/cert
    ksef_tested: dict[str, bool] = field(default_factory=dict)
    environment: str = "test"

    @property
    def sql_ok(self) -> bool:
        return self.sql_password_set and self.sql_tested_for == self.current_server

    @property
    def company_ok(self) -> bool:
        return bool(self.real_companies)

    @property
    def ksef_ok(self) -> bool:
        return any(self.ksef_ready.get(cid) for cid in self.real_companies)

    @property
    def complete(self) -> bool:
        return self.sql_password_set and self.company_ok and self.ksef_ok

    @property
    def current_step(self) -> int:
        if not self.sql_ok:
            return 1
        if not self.company_ok:
            return 2
        if not self.ksef_ok:
            return 3
        return 4


def get_setup_status(context) -> SetupStatus:
    from app.secrets import SecretStore

    cfg = context.cfg
    status = SetupStatus(
        current_server=cfg.optima.server,
        environment=cfg.ksef.environment,
    )
    store = context.secrets
    status.sql_password_set = store.get_optional("sql-read-password") is not None
    status.sql_tested_for = context.sync_state.get(SETUP_SCOPE, "optima_ok")

    for cid, comp in cfg.companies.items():
        if comp.nip == PLACEHOLDER_NIP:
            status.placeholder_companies.append(cid)
        else:
            status.real_companies.append(cid)
        has_token = (
            store.get_optional(SecretStore.ksef_token_key(cid, cfg.ksef.environment))
            is not None
        )
        has_cert = bool(comp.ksef_cert_path) and Path(comp.ksef_cert_path).expanduser().exists()
        status.ksef_ready[cid] = has_token or has_cert
        status.ksef_tested[cid] = (
            context.sync_state.get(SETUP_SCOPE, f"ksef_ok:{cid}") == cfg.ksef.environment
        )
    return status


def mark_sql_tested(context) -> None:
    context.sync_state.set(SETUP_SCOPE, "optima_ok", context.cfg.optima.server)


def mark_ksef_tested(context, company_id: str) -> None:
    context.sync_state.set(
        SETUP_SCOPE, f"ksef_ok:{company_id}", context.cfg.ksef.environment
    )
