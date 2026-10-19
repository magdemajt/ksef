"""Zapis zmian konfiguracji do config.toml z poziomu GUI.

tomlkit zachowuje komentarze i formatowanie istniejacego pliku. Kazdy zapis:
    1. modyfikacja dokumentu tomlkit,
    2. walidacja wyniku przez pydantic (AppConfig) - niepoprawna konfiguracja
       nigdy nie trafia na dysk,
    3. zapis atomowy (plik tymczasowy + rename).
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import tomlkit

from app.config import AppConfig, config_path

COMPANY_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")

# Pola firmy edytowalne z GUI (nazwa pola -> czy wymagane)
COMPANY_FIELDS = {
    "nip": True,
    "name": True,
    "database": True,
    "address_line1": False,
    "address_line2": False,
    "regon": False,
    "country_code": False,
    "auth_method": False,
    "ksef_cert_path": False,
    "purchase_register": False,
    "lp_numbering": False,
}


class ConfigStoreError(ValueError):
    pass


def _load_doc(path: Path) -> tomlkit.TOMLDocument:
    if not path.exists():
        raise ConfigStoreError(f"Brak pliku konfiguracji: {path}")
    return tomlkit.parse(path.read_text(encoding="utf-8"))


def _validate(doc: tomlkit.TOMLDocument) -> None:
    try:
        AppConfig.model_validate(doc.unwrap())
    except Exception as exc:
        raise ConfigStoreError(f"Konfiguracja po zmianie niepoprawna: {exc}") from exc


def _write_atomic(path: Path, doc: tomlkit.TOMLDocument) -> None:
    content = tomlkit.dumps(doc)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".toml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def update_global(ksef: dict, optima: dict, path: Path | None = None) -> None:
    """Aktualizuje sekcje [ksef] i [optima] (tylko przekazane klucze)."""
    path = path or config_path()
    doc = _load_doc(path)
    for section_name, values in (("ksef", ksef), ("optima", optima)):
        section = doc.setdefault(section_name, tomlkit.table())
        for key, value in values.items():
            if value is not None:
                section[key] = value
    _validate(doc)
    _write_atomic(path, doc)


def add_company(company_id: str, fields: dict, path: Path | None = None) -> None:
    path = path or config_path()
    if not COMPANY_ID_RE.match(company_id):
        raise ConfigStoreError(
            "Identyfikator firmy: tylko male litery, cyfry i podkreslniki (max 40 znakow)"
        )
    doc = _load_doc(path)
    companies = doc.setdefault("companies", tomlkit.table())
    if company_id in companies:
        raise ConfigStoreError(f"Firma {company_id!r} juz istnieje w konfiguracji")

    missing = [f for f, required in COMPANY_FIELDS.items() if required and not fields.get(f)]
    if missing:
        raise ConfigStoreError(f"Brak wymaganych pol: {', '.join(missing)}")

    table = tomlkit.table()
    for field in COMPANY_FIELDS:
        value = fields.get(field)
        if value not in (None, ""):
            table[field] = value
    table["writes_enabled"] = False  # zapisy do Optimy zawsze startuja wylaczone
    companies[company_id] = table

    _validate(doc)
    _write_atomic(path, doc)


def update_company(
    company_id: str,
    fields: dict,
    writes_enabled: bool | None = None,
    path: Path | None = None,
) -> None:
    """Aktualizuje istniejaca firme (tylko przekazane, niepuste pola).

    writes_enabled przekazywane osobno i jawnie - to bramka bezposrednich
    INSERT-ow do bazy Optimy (etap M5), nie zwykle pole formularza.
    """
    path = path or config_path()
    doc = _load_doc(path)
    companies = doc.get("companies") or {}
    if company_id not in companies:
        raise ConfigStoreError(f"Nieznana firma: {company_id!r}")
    for field in COMPANY_FIELDS:
        value = fields.get(field)
        if value not in (None, ""):
            companies[company_id][field] = value
    if writes_enabled is not None:
        companies[company_id]["writes_enabled"] = writes_enabled
    _validate(doc)
    _write_atomic(path, doc)
