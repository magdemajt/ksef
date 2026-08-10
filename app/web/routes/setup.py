"""Konfigurator pierwszego uruchomienia (/setup) - kreator 4 krokow z samouczkiem."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import configstore
from app.config import KsefEnv
from app.secrets import SecretStore
from app.setup_state import (
    PLACEHOLDER_NIP,
    get_setup_status,
    mark_ksef_tested,
    mark_sql_tested,
)
from app.web.deps import AppContext, ctx

router = APIRouter()


def _render(request: Request, context: AppContext, message: str | None = None,
            message_ok: bool = True, discovery=None, step: int | None = None):
    status = get_setup_status(context)
    return request.app.state.templates.TemplateResponse(
        request,
        "setup.html",
        {
            "context": context,
            "company_id": context.default_company_id(),
            "company": context.company(context.default_company_id()),
            "active": "setup",
            "status": status,
            "step": step or status.current_step,
            "message": message, "message_ok": message_ok,
            "discovery": discovery,
            "placeholder_nip": PLACEHOLDER_NIP,
        },
    )


@router.get("/setup")
def setup_view(request: Request, step: int | None = None,
               context: AppContext = Depends(ctx)):
    return _render(request, context, step=step)


# -- krok 1: serwer SQL --


@router.post("/setup/sql")
def setup_sql(
    request: Request,
    csrf: str = Form(...),
    optima_server: str = Form(...),
    read_user: str = Form(...),
    write_user: str = Form(...),
    sql_read_password: str = Form(""),
    sql_write_password: str = Form(""),
    skip_test: str = Form(""),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    try:
        configstore.update_global(
            ksef={},
            optima={
                "server": optima_server.strip(),
                "read_user": read_user.strip(),
                "write_user": write_user.strip(),
            },
        )
    except configstore.ConfigStoreError as exc:
        return _render(request, context, str(exc), False, step=1)
    store = SecretStore()
    if sql_read_password:
        store.set("sql-read-password", sql_read_password)
    if sql_write_password:
        store.set("sql-write-password", sql_write_password)
    context.reload()
    context.audit.log("setup_sql_saved", details={"server": optima_server.strip()})

    if skip_test == "on":
        return _render(
            request, context,
            "Zapisano bez testu - polaczenie oznaczone jako niezweryfikowane", True, step=2,
        )
    try:
        conn = context.optima.master_connection()
        conn.cursor().execute("SELECT 1")
        conn.close()
        mark_sql_tested(context)
        return _render(request, context, "Polaczenie z serwerem SQL dziala", True, step=2)
    except Exception as exc:  # noqa: BLE001
        return _render(
            request, context,
            f"Zapisano, ale test polaczenia nie przeszedl: {exc}", False, step=1,
        )


# -- krok 2: firma (+ wykrywanie baz) --


@router.post("/setup/discover")
def setup_discover(request: Request, csrf: str = Form(...),
                   context: AppContext = Depends(ctx)):
    context.check_csrf(csrf)
    from app.optima.discovery import discover_databases

    try:
        conn = context.optima.master_connection()
    except Exception as exc:  # noqa: BLE001
        return _render(request, context,
                       f"Nie mozna polaczyc z serwerem: {exc}", False, step=2)
    try:
        result = discover_databases(conn)
        mark_sql_tested(context)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
    configured = {comp.database for comp in context.cfg.companies.values()}
    return _render(request, context, step=2,
                   discovery={"result": result, "configured_dbs": configured})


@router.post("/setup/company")
def setup_company(
    request: Request,
    csrf: str = Form(...),
    company_id: str = Form(...),
    nip: str = Form(...),
    name: str = Form(...),
    database: str = Form(...),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    purchase_register: str = Form("ZAKUP"),
    lp_numbering: str = Form("month"),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    company_id = company_id.strip().lower()
    fields = {
        "nip": nip.strip(), "name": name.strip(), "database": database.strip(),
        "address_line1": address_line1.strip(), "address_line2": address_line2.strip(),
        "purchase_register": purchase_register.strip() or "ZAKUP",
        "lp_numbering": lp_numbering,
    }
    try:
        if company_id in context.cfg.companies:
            # uzupelnienie firmy-placeholdera utworzonej przy pierwszym starcie
            configstore.update_company(company_id, fields)
            action = "setup_company_updated"
        else:
            configstore.add_company(company_id, fields)
            action = "setup_company_added"
    except configstore.ConfigStoreError as exc:
        return _render(request, context, str(exc), False, step=2)
    context.reload()
    context.audit.log(action, company_id, "company", company_id,
                      details={"nip": fields["nip"], "database": fields["database"]})
    return _render(request, context, f"Firma {company_id} zapisana", True, step=3)


# -- krok 3: KSeF --


@router.post("/setup/ksef")
def setup_ksef(
    request: Request,
    csrf: str = Form(...),
    environment: str = Form(...),
    target_company: str = Form(...),
    ksef_token: str = Form(""),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    if environment not in [e.value for e in KsefEnv]:
        return _render(request, context, f"Nieznane srodowisko: {environment}", False, step=3)
    try:
        configstore.update_global(ksef={"environment": environment}, optima={})
    except configstore.ConfigStoreError as exc:
        return _render(request, context, str(exc), False, step=3)
    context.reload()
    if target_company not in context.cfg.companies:
        return _render(request, context, f"Nieznana firma: {target_company}", False, step=3)
    if ksef_token:
        SecretStore().set(
            SecretStore.ksef_token_key(target_company, environment), ksef_token
        )
    context.audit.log("setup_ksef_saved", target_company,
                      details={"environment": environment, "token_set": bool(ksef_token)})

    try:
        context.gateway(target_company).auth_ok()
        mark_ksef_tested(context, target_company)
        return _render(request, context,
                       "Uwierzytelnienie w KSeF dziala", True, step=4)
    except Exception as exc:  # noqa: BLE001
        return _render(
            request, context,
            f"Zapisano, ale test uwierzytelnienia nie przeszedl: {exc}", False, step=3,
        )


@router.post("/setup/ksef-testdata")
def setup_ksef_testdata(
    request: Request,
    csrf: str = Form(...),
    target_company: str = Form(...),
    context: AppContext = Depends(ctx),
):
    """Bootstrap srodowiska TEST: rejestruje losowy NIP, mintuje token, podmienia NIP firmy."""
    context.check_csrf(csrf)
    if context.cfg.ksef.environment != KsefEnv.TEST:
        return _render(request, context,
                       "Konto testowe mozna utworzyc tylko na srodowisku TEST", False, step=3)
    if target_company not in context.cfg.companies:
        return _render(request, context, f"Nieznana firma: {target_company}", False, step=3)
    from app.cli import _random_nip, _random_pesel

    nip = _random_nip()
    try:
        gateway = context.gateway(target_company)
        gateway.testdata_create_person(nip, _random_pesel(), "ksef-bridge setup")
        authed = gateway.test_authenticate_with_test_certificate(nip)
        token = gateway.mint_ksef_token(authed)
    except Exception as exc:  # noqa: BLE001
        return _render(request, context,
                       f"Bootstrap srodowiska TEST nie powiodl sie: {exc}", False, step=3)
    SecretStore().set(SecretStore.ksef_token_key(target_company, KsefEnv.TEST), token)
    try:
        # token dziala w kontekscie wygenerowanego NIP - firma musi go uzywac na TEST
        configstore.update_company(target_company, {"nip": nip})
    except configstore.ConfigStoreError as exc:
        return _render(request, context, str(exc), False, step=3)
    context.reload()
    mark_ksef_tested(context, target_company)
    context.audit.log("setup_ksef_testdata", target_company, details={"nip": nip})
    return _render(
        request, context,
        f"Utworzono konto testowe KSeF (NIP {nip}) i zapisano token - gotowe", True, step=4,
    )
