"""Launcher desktopowy: pierwszy start bez konfiguracji, serwer + przegladarka.

Uzywany jako punkt wejscia zbundlowanej aplikacji (PyInstaller). Zachowanie:
    1. znajdz config.toml: $KSEF_BRIDGE_CONFIG -> ./config.toml -> katalog
       konfiguracji uzytkownika (Linux: ~/.config/ksef-bridge,
       Windows: %APPDATA%/ksef-bridge),
    2. gdy brak - utworz szablon z przykladowa firma (do edycji w GUI),
    3. uruchom serwer i otworz GUI w domyslnej przegladarce.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from pathlib import Path

from app.config import CONFIG_ENV_VAR, DEFAULT_CONFIG_PATH


def user_config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ksef-bridge"


def user_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "ksef-bridge"


def _default_config_text() -> str:
    data = user_data_dir()
    return f"""\
# ksef-bridge - konfiguracja (utworzona automatycznie przy pierwszym starcie)
# Dane firmy i serwera Optimy mozesz edytowac w GUI: zakladka Ustawienia.

[ksef]
environment = "test"
allow_prod_send = false

[optima]
server = "localhost,1433"
read_user = "ksef_reader"
write_user = "ksef_writer"
trust_server_certificate = true

[companies.moja_firma]
# Przykladowa firma - uzupelnij prawdziwe dane w GUI (Ustawienia -> edytuj)
nip = "0000000000"
name = "Moja Firma (uzupelnij dane)"
address_line1 = ""
address_line2 = ""
database = "CDN_Firma"
auth_method = "token"
purchase_register = "ZAKUP"
lp_numbering = "month"
writes_enabled = false

[sync]
poll_interval_minutes = 10
backfill_start_date = "2026-01-01"

[state]
sqlite_path = '{data / "state.db"}'
invoice_store = '{data / "invoices"}'

[web]
host = "127.0.0.1"
port = 8300
"""


def resolve_config() -> Path:
    env_path = os.environ.get(CONFIG_ENV_VAR)
    if env_path:
        return Path(env_path)
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return user_config_dir() / "config.toml"


def ensure_config(path: Path) -> bool:
    """Tworzy domyslna konfiguracje gdy brak. True = pierwszy start."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_default_config_text(), encoding="utf-8")
    return True


def selftest() -> int:
    """Weryfikacja zbundlowanych zasobow (XSD, szablony, schema.sql) - dla CI."""
    from datetime import date
    from decimal import Decimal as D

    from app.fa3.builder import build_fa3
    from app.fa3.mapper import map_invoice
    from app.fa3.parser import parse_invoice
    from app.fa3.validator import validate
    from app.optima.models import Contractor, SalesInvoice, SalesLine

    buyer = Contractor(knt_id=1, kod="T", nip="5261040158", nazwa="Test",
                       ulica="Testowa", nr_domu="1", miasto="Warszawa", kod_pocztowy="00-001")
    inv = SalesInvoice(
        trn_id=1, numer_pelny="FA/SELFTEST/1", data_dok=date.today(), data_wys=None,
        termin=None, razem_netto=D("100.00"), razem_vat=D("23.00"), razem_brutto=D("123.00"),
        korekta=False, waluta="PLN", kurs=None, kurs_l=None, forma_platnosci=None, buyer=buyer,
        lines=[SalesLine(1, "Selftest", D(1), "szt.", D("100.00"), D(23), 2, D("100.00"))],
    )
    xml = build_fa3(map_invoice(inv, "5265877635", "Firma", "ul. X 1", "00-001 W-wa"))
    errors = validate(xml)
    if errors:
        print(f"SELFTEST FAILED: walidacja XSD: {errors}")
        return 1
    parsed = parse_invoice(xml, "SELFTEST")
    if parsed.total_gross != D("123.00"):
        print(f"SELFTEST FAILED: parser: {parsed.total_gross}")
        return 1

    # sterownik SQL: import wymusza zaladowanie natywnego ddbc_bindings (.pyd/.so),
    # ktorego brak w bundlu objawialby sie dopiero przy pierwszym polaczeniu
    try:
        import mssql_python  # noqa: F401
        from mssql_python import connect  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"SELFTEST FAILED: sterownik mssql-python nie laduje sie: {exc}")
        return 1

    from importlib import resources
    if not resources.files("app.state").joinpath("schema.sql").is_file():
        print("SELFTEST FAILED: brak schema.sql w bundlu")
        return 1
    if not resources.files("app.web").joinpath("templates/base.html").is_file():
        print("SELFTEST FAILED: brak szablonow w bundlu")
        return 1
    print("SELFTEST OK: XSD + mapper + parser + zasoby bundla")
    return 0


def main() -> None:
    import uvicorn

    from app.config import load_config
    from app.main import create_app

    if "--selftest" in sys.argv:
        raise SystemExit(selftest())

    cfg_path = resolve_config()
    first_run = ensure_config(cfg_path)
    os.environ[CONFIG_ENV_VAR] = str(cfg_path)
    print(f"Konfiguracja: {cfg_path}" + ("  (utworzona - uzupelnij dane w GUI)" if first_run else ""))

    cfg = load_config(cfg_path)
    url = f"http://{cfg.web.host}:{cfg.web.port}" + ("/setup" if first_run else "/")

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"GUI: {url}  (Ctrl+C zatrzymuje aplikacje)")
    uvicorn.run(create_app(cfg), host=cfg.web.host, port=cfg.web.port, log_level="info")


if __name__ == "__main__":
    main()
