"""CLI: ksef-bridge <polecenie>."""

from __future__ import annotations

import getpass
import json
import logging
import random
import sys

import typer

from app.config import KsefEnv, load_config
from app.secrets import SecretStore

app = typer.Typer(help="Integrator Comarch ERP Optima <-> KSeF 2.0")
secrets_app = typer.Typer(help="Zarzadzanie sekretami (keyring)")
app.add_typer(secrets_app, name="secrets")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _ctx():
    from app.web.deps import AppContext

    return AppContext.create(load_config())


@app.command()
def serve():
    """Uruchom web GUI (host/port z config.toml)."""
    import uvicorn

    from app.main import create_app

    cfg = load_config()
    uvicorn.run(create_app(cfg), host=cfg.web.host, port=cfg.web.port)


@app.command()
def diagnose(company: str = typer.Option(None, "--company", "-f")):
    """Diagnostyka zywej bazy Optimy (read-only): kolumny KSeF, identity, triggery, stawki."""
    from app.optima.introspect import run_diagnose

    context = _ctx()
    company_id = company or context.default_company_id()
    conn = context.optima.read_connection(context.company(company_id))
    report = run_diagnose(conn)
    context.sync_state.set(company_id, "diagnose_json", json.dumps(report, default=str))
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))

    guess = report.get("lp_numbering_guess")
    configured = context.company(company_id).lp_numbering
    if guess and guess != configured:
        print(
            f"\nUWAGA: numeracja Lp w bazie wyglada na '{guess}', "
            f"a w config.toml ustawiono '{configured}' - popraw konfiguracje!",
            file=sys.stderr,
        )


@app.command()
def snapshot(
    label: str = typer.Option(..., "--label", help="np. before / after"),
    company: str = typer.Option(None, "--company", "-f"),
):
    """Zrzut watermarkow wszystkich tabel CDN.* (workflow insert_spec, patrz README)."""
    from app.optima.introspect import snapshot as take_snapshot

    context = _ctx()
    company_id = company or context.default_company_id()
    conn = context.optima.read_connection(context.company(company_id))
    out = context.cfg.state.invoice_store.parent / f"snapshot-{company_id}-{label}.json"
    path = take_snapshot(conn, out, label)
    print(f"Snapshot zapisany: {path}")


@app.command()
def diff(company: str = typer.Option(None, "--company", "-f")):
    """Roznica miedzy snapshotami before/after -> szkic insert_spec."""
    from app.optima.introspect import diff as run_diff

    context = _ctx()
    company_id = company or context.default_company_id()
    base = context.cfg.state.invoice_store.parent
    before = base / f"snapshot-{company_id}-before.json"
    after = base / f"snapshot-{company_id}-after.json"
    if not before.exists() or not after.exists():
        raise typer.BadParameter(
            f"Wymagane pliki: {before} i {after} (ksef-bridge snapshot --label before/after)"
        )
    conn = context.optima.read_connection(context.company(company_id))
    changes = run_diff(conn, before, after)
    out = base / f"diff-{company_id}.json"
    out.write_text(json.dumps(changes, indent=2, ensure_ascii=False, default=str), "utf-8")
    print(f"Diff zapisany: {out}")
    print("Tabele ze zmianami:", ", ".join(changes) or "(brak)")
    print(
        "\nNastepny krok: przejrzyj diff, ustal kolumny-literaly i zapisz je jako "
        f"{base / f'insert_spec-{company_id}.json'} w formacie:\n"
        '{"VatNag": {"literals": {...}}, "VatTab": {"literals": {...}}, '
        '"BnkZdarzenia": {"literals": {...}}}'
    )


@app.command()
def testdata(
    nip: str = typer.Option(None, help="NIP testowy (domyslnie losowy)"),
    company: str = typer.Option(None, "--company", "-f"),
):
    """Bootstrap srodowiska TEST: rejestruje osobe/NIP i mintuje token KSeF."""
    context = _ctx()
    if context.cfg.ksef.environment != KsefEnv.TEST:
        raise typer.BadParameter("testdata dziala tylko przy environment=test")
    company_id = company or context.default_company_id()
    gateway = context.gateway(company_id)

    nip = nip or _random_nip()
    pesel = _random_pesel()
    print(f"Rejestruje osobe testowa: NIP={nip} PESEL={pesel}")
    gateway.testdata_create_person(nip, pesel, "ksef-bridge test")

    print("Uwierzytelniam certyfikatem testowym (self-signed)...")
    authed = gateway.test_authenticate_with_test_certificate(nip)
    print("Mintuje token KSeF...")
    token = gateway.mint_ksef_token(authed)
    store = SecretStore()
    store.set(SecretStore.ksef_token_key(company_id, KsefEnv.TEST), token)
    print(
        f"Token zapisany w keyring ({SecretStore.ksef_token_key(company_id, KsefEnv.TEST)}).\n"
        f"UWAGA: zaktualizuj nip w [companies.{company_id}] na {nip}, "
        f"bo token dziala w kontekscie tego NIP."
    )


@app.command()
def sync(company: str = typer.Option(None, "--company", "-f")):
    """Reczna synchronizacja faktur zakupu z KSeF."""
    context = _ctx()
    company_id = company or context.default_company_id()
    stats = context.download_service(company_id).sync()
    print(f"Synchronizacja: {stats}")


@app.command("auth-test")
def auth_test(company: str = typer.Option(None, "--company", "-f")):
    """Test uwierzytelnienia w KSeF."""
    context = _ctx()
    company_id = company or context.default_company_id()
    context.gateway(company_id).auth_ok()
    print("Uwierzytelnienie OK")


@secrets_app.command("set")
def secrets_set(
    key: str = typer.Argument(
        ..., help="np. sql-read-password | sql-write-password | ksef-token | p12-pass"
    ),
    company: str = typer.Option(None, "--company", "-f"),
):
    """Zapisz sekret w keyring (wartosc podawana interaktywnie)."""
    cfg = load_config()
    env = cfg.ksef.environment
    company_id = company or next(iter(cfg.companies))
    full_key = {
        "ksef-token": SecretStore.ksef_token_key(company_id, env),
        "ksef-refresh": SecretStore.ksef_refresh_key(company_id, env),
        "p12-pass": SecretStore.p12_pass_key(company_id),
    }.get(key, key)
    value = getpass.getpass(f"Wartosc dla {full_key!r}: ")
    if not value:
        raise typer.BadParameter("Pusta wartosc")
    SecretStore().set(full_key, value)
    print(f"Zapisano {full_key!r} w keyring")


def _random_nip() -> str:
    # poprawna suma kontrolna + prefiks akceptowany przez KSeF:
    # pierwsza cyfra 1-9, w cyfrach 2-3 co najmniej jedna niezerowa
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    while True:
        digits = [random.randint(1, 9), random.randint(0, 9), random.randint(1, 9)]
        digits += [random.randint(0, 9) for _ in range(6)]
        check = sum(w * d for w, d in zip(weights, digits)) % 11
        if check != 10:
            return "".join(map(str, digits)) + str(check)


def _random_pesel() -> str:
    # PESEL syntetyczny z poprawna suma kontrolna (rocznik 1980-1999)
    from datetime import date, timedelta

    start = date(1980, 1, 1)
    d = start + timedelta(days=random.randint(0, 7300))
    base = f"{d.year % 100:02d}{d.month:02d}{d.day:02d}" + "".join(
        str(random.randint(0, 9)) for _ in range(4)
    )
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    check = (10 - sum(w * int(c) for w, c in zip(weights, base)) % 10) % 10
    return base + str(check)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
