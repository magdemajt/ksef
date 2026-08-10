"""Magazyn sekretow: keyring (Secret Service) z fallbackiem na zmienne srodowiskowe.

Klucze sekretow:
    ksef-token:{company}:{env}    token KSeF danej firmy dla danego srodowiska
    ksef-refresh:{company}:{env}  refresh token po udanym uwierzytelnieniu
    p12-pass:{company}            haslo do pliku .p12 certyfikatu KSeF
    sql-read-password             haslo loginu ksef_reader
    sql-write-password            haslo loginu ksef_writer

Fallback env: KSEF_BRIDGE_SECRET_<KLUCZ z ':' i '-' zamienionymi na '_'> (uppercase),
np. ksef-token:firma_a:test -> KSEF_BRIDGE_SECRET_KSEF_TOKEN_FIRMA_A_TEST.
"""

from __future__ import annotations

import os

SERVICE_NAME = "ksef-bridge"


class SecretNotFoundError(RuntimeError):
    def __init__(self, key: str):
        env_name = _env_name(key)
        super().__init__(
            f"Brak sekretu {key!r}. Ustaw go poleceniem: ksef-bridge secrets set {key} "
            f"albo zmienna srodowiskowa {env_name}."
        )
        self.key = key


def _env_name(key: str) -> str:
    return "KSEF_BRIDGE_SECRET_" + key.replace(":", "_").replace("-", "_").upper()


def _keyring():
    # Import leniwy: keyring przy imporcie dotyka DBus, co w srodowiskach headless
    # potrafi byc wolne lub rzucac ostrzezenia.
    import keyring

    return keyring


class SecretStore:
    def get(self, key: str) -> str:
        env_val = os.environ.get(_env_name(key))
        if env_val:
            return env_val
        try:
            val = _keyring().get_password(SERVICE_NAME, key)
        except Exception:
            val = None
        if not val:
            raise SecretNotFoundError(key)
        return val

    def get_optional(self, key: str) -> str | None:
        try:
            return self.get(key)
        except SecretNotFoundError:
            return None

    def set(self, key: str, value: str) -> None:
        _keyring().set_password(SERVICE_NAME, key, value)

    def delete(self, key: str) -> None:
        try:
            _keyring().delete_password(SERVICE_NAME, key)
        except Exception:
            pass

    # -- pomocnicze nazwy kluczy --

    @staticmethod
    def ksef_token_key(company_id: str, env: str) -> str:
        return f"ksef-token:{company_id}:{env}"

    @staticmethod
    def ksef_refresh_key(company_id: str, env: str) -> str:
        return f"ksef-refresh:{company_id}:{env}"

    @staticmethod
    def p12_pass_key(company_id: str) -> str:
        return f"p12-pass:{company_id}"
