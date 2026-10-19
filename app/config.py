"""Konfiguracja aplikacji: config.toml + zmienne srodowiskowe KSEF_BRIDGE_*."""

from __future__ import annotations

import os
import tomllib
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

CONFIG_ENV_VAR = "KSEF_BRIDGE_CONFIG"
DEFAULT_CONFIG_PATH = Path("config.toml")


class KsefEnv(StrEnum):
    TEST = "test"
    DEMO = "demo"
    PROD = "prod"

    @property
    def base_url(self) -> str:
        return {
            KsefEnv.TEST: "https://api-test.ksef.mf.gov.pl/v2",
            KsefEnv.DEMO: "https://api-demo.ksef.mf.gov.pl/v2",
            KsefEnv.PROD: "https://api.ksef.mf.gov.pl/v2",
        }[self]


class AuthMethod(StrEnum):
    TOKEN = "token"
    KSEF_CERT = "ksef_cert"


class LpNumbering(StrEnum):
    MONTH = "month"
    YEAR = "year"


class KsefSection(BaseModel):
    environment: KsefEnv = KsefEnv.TEST
    allow_prod_send: bool = False


class OptimaSection(BaseModel):
    server: str
    read_user: str = "ksef_reader"
    write_user: str = "ksef_writer"
    trust_server_certificate: bool = True


class CompanyConfig(BaseModel):
    nip: str
    name: str
    address_line1: str = ""
    address_line2: str = ""
    regon: str = ""  # Stopka/Rejestry/REGON (9 lub 14 cyfr)
    country_code: str = "PL"
    database: str
    auth_method: AuthMethod = AuthMethod.TOKEN
    ksef_cert_path: str = ""
    purchase_register: str = "ZAKUP"
    lp_numbering: LpNumbering = LpNumbering.MONTH
    writes_enabled: bool = False

    @field_validator("nip")
    @classmethod
    def _normalize_nip(cls, v: str) -> str:
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) != 10:
            raise ValueError(f"NIP musi miec 10 cyfr, otrzymano: {v!r}")
        return digits


class SyncSection(BaseModel):
    poll_interval_minutes: int = Field(default=10, ge=5)
    backfill_start_date: str = "2026-01-01"


class StateSection(BaseModel):
    sqlite_path: Path = Path("~/.local/share/ksef-bridge/state.db")
    invoice_store: Path = Path("~/.local/share/ksef-bridge/invoices")

    @field_validator("sqlite_path", "invoice_store")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(os.path.expanduser(str(v)))


class WebSection(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8300


class AppConfig(BaseModel):
    ksef: KsefSection = KsefSection()
    optima: OptimaSection
    companies: dict[str, CompanyConfig]
    sync: SyncSection = SyncSection()
    state: StateSection = StateSection()
    web: WebSection = WebSection()

    @model_validator(mode="after")
    def _at_least_one_company(self) -> AppConfig:
        if not self.companies:
            raise ValueError("Konfiguracja musi zawierac co najmniej jedna sekcje [companies.X]")
        return self

    def company(self, company_id: str) -> CompanyConfig:
        try:
            return self.companies[company_id]
        except KeyError:
            raise KeyError(
                f"Nieznana firma {company_id!r}; dostepne: {', '.join(self.companies)}"
            ) from None


def _apply_env_overrides(data: dict) -> dict:
    """Proste nadpisania srodowiskowe: KSEF_BRIDGE_KSEF__ENVIRONMENT=demo itd.

    Podwojny podkreslnik oddziela poziomy zagniezdzenia.
    """
    prefix = "KSEF_BRIDGE_"
    for key, value in os.environ.items():
        if not key.startswith(prefix) or key == CONFIG_ENV_VAR:
            continue
        path = key[len(prefix):].lower().split("__")
        node = data
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return data


def config_path() -> Path:
    """Sciezka pliku konfiguracji uzywana przez load_config (env albo ./config.toml)."""
    return Path(os.environ.get(CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH))


def load_config(path: Path | None = None) -> AppConfig:
    cfg_path = path or config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Brak pliku konfiguracji: {cfg_path}. Skopiuj config.toml.example do config.toml "
            f"i uzupelnij, albo wskaz plik zmienna {CONFIG_ENV_VAR}."
        )
    with open(cfg_path, "rb") as fh:
        data = tomllib.load(fh)
    return AppConfig.model_validate(_apply_env_overrides(data))
