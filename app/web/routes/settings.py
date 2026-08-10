from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import configstore
from app.config import KsefEnv
from app.secrets import SecretStore
from app.web.deps import AppContext, ctx

router = APIRouter()


def _render(request: Request, context: AppContext, company_id: str,
            message: str | None = None, message_ok: bool = True,
            discovery=None, prefill: dict | None = None):
    if company_id not in context.cfg.companies:
        company_id = context.default_company_id()
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "context": context, "company_id": company_id,
            "company": context.company(company_id), "active": "settings",
            "message": message, "message_ok": message_ok,
            "discovery": discovery, "prefill": prefill or {},
        },
    )


@router.get("/settings")
def settings(request: Request, c: str | None = None, msg: str | None = None,
             prefill_db: str | None = None, prefill_name: str | None = None,
             context: AppContext = Depends(ctx)):
    prefill = {}
    if prefill_db:
        suggested_id = "".join(
            ch if ch.isalnum() else "_" for ch in prefill_db.lower().removeprefix("cdn_")
        ).strip("_")
        prefill = {
            "database": prefill_db,
            "name": prefill_name or "",
            "company_id": suggested_id,
        }
    return _render(request, context, c or context.default_company_id(),
                   message=msg, prefill=prefill)


@router.post("/settings/discover")
def discover_databases_route(
    request: Request, c: str = Form(...), csrf: str = Form(...),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    from app.optima.discovery import discover_databases

    try:
        conn = context.optima.master_connection()
    except Exception as exc:  # noqa: BLE001
        return _render(
            request, context, c,
            f"Nie mozna polaczyc z serwerem (baza master): {exc}", False,
        )
    try:
        result = discover_databases(conn)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001, S110
            pass
    configured_dbs = {comp.database for comp in context.cfg.companies.values()}
    context.audit.log(
        "databases_discovered", c, environment=context.cfg.ksef.environment,
        details={"found": [d.name for d in result.company_databases]},
    )
    return _render(
        request, context, c,
        discovery={"result": result, "configured_dbs": configured_dbs},
    )


# -- konfiguracja globalna KSeF / Optima --


@router.post("/settings/ksef")
def save_ksef_config(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    environment: str = Form(...),
    allow_prod_send: str = Form(""),
    optima_server: str = Form(...),
    read_user: str = Form(...),
    write_user: str = Form(...),
    sql_read_password: str = Form(""),
    sql_write_password: str = Form(""),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    try:
        if environment not in [e.value for e in KsefEnv]:
            raise configstore.ConfigStoreError(f"Nieznane srodowisko: {environment}")
        configstore.update_global(
            ksef={"environment": environment, "allow_prod_send": allow_prod_send == "on"},
            optima={
                "server": optima_server.strip(),
                "read_user": read_user.strip(),
                "write_user": write_user.strip(),
            },
        )
        store = SecretStore()
        if sql_read_password:
            store.set("sql-read-password", sql_read_password)
        if sql_write_password:
            store.set("sql-write-password", sql_write_password)
        context.reload()
        context.audit.log(
            "config_updated", c, "config", "ksef+optima", environment,
            {"environment": environment, "server": optima_server},
        )
        return _render(request, context, c, "Konfiguracja zapisana i przeladowana", True)
    except configstore.ConfigStoreError as exc:
        return _render(request, context, c, str(exc), False)


# -- dodawanie firmy --


@router.post("/settings/company")
def add_company(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    company_id: str = Form(...),
    nip: str = Form(...),
    name: str = Form(...),
    database: str = Form(...),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    auth_method: str = Form("token"),
    ksef_cert_path: str = Form(""),
    purchase_register: str = Form("ZAKUP"),
    lp_numbering: str = Form("month"),
    ksef_token: str = Form(""),
    p12_password: str = Form(""),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    company_id = company_id.strip().lower()
    try:
        if auth_method == "ksef_cert" and not ksef_cert_path.strip():
            raise configstore.ConfigStoreError(
                "auth_method=ksef_cert wymaga sciezki do pliku .p12"
            )
        configstore.add_company(
            company_id,
            {
                "nip": nip.strip(),
                "name": name.strip(),
                "database": database.strip(),
                "address_line1": address_line1.strip(),
                "address_line2": address_line2.strip(),
                "auth_method": auth_method,
                "ksef_cert_path": ksef_cert_path.strip(),
                "purchase_register": purchase_register.strip() or "ZAKUP",
                "lp_numbering": lp_numbering,
            },
        )
        store = SecretStore()
        env = context.cfg.ksef.environment
        if ksef_token:
            store.set(SecretStore.ksef_token_key(company_id, env), ksef_token)
        if p12_password:
            store.set(SecretStore.p12_pass_key(company_id), p12_password)
        context.reload()
        context.audit.log(
            "company_added", company_id, "company", company_id,
            context.cfg.ksef.environment, {"nip": nip.strip(), "database": database.strip()},
        )
        return RedirectResponse(
            f"/settings?c={company_id}&msg=Firma+{company_id}+dodana", status_code=303
        )
    except configstore.ConfigStoreError as exc:
        return _render(request, context, c, str(exc), False)


# -- edycja istniejacej firmy --


def _render_edit(request: Request, context: AppContext, company_id: str, edit_id: str,
                 message: str | None = None, message_ok: bool = True):
    if edit_id not in context.cfg.companies:
        return RedirectResponse(f"/settings?c={company_id}", status_code=303)
    return request.app.state.templates.TemplateResponse(
        request,
        "settings_company_edit.html",
        {
            "context": context, "company_id": company_id,
            "company": context.company(company_id), "active": "settings",
            "edit_id": edit_id, "edit_company": context.cfg.companies[edit_id],
            "message": message, "message_ok": message_ok,
        },
    )


@router.get("/settings/company/{edit_id}")
def edit_company_form(request: Request, edit_id: str, c: str | None = None,
                      context: AppContext = Depends(ctx)):
    return _render_edit(request, context, c or context.default_company_id(), edit_id)


@router.post("/settings/company/{edit_id}")
def save_company(
    request: Request,
    edit_id: str,
    c: str = Form(...),
    csrf: str = Form(...),
    nip: str = Form(...),
    name: str = Form(...),
    database: str = Form(...),
    address_line1: str = Form(""),
    address_line2: str = Form(""),
    auth_method: str = Form("token"),
    ksef_cert_path: str = Form(""),
    purchase_register: str = Form("ZAKUP"),
    lp_numbering: str = Form("month"),
    ksef_token: str = Form(""),
    p12_password: str = Form(""),
    writes_enabled: str = Form(""),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    try:
        if auth_method == "ksef_cert" and not ksef_cert_path.strip():
            raise configstore.ConfigStoreError(
                "auth_method=ksef_cert wymaga sciezki do pliku .p12"
            )
        configstore.update_company(
            edit_id,
            {
                "nip": nip.strip(),
                "name": name.strip(),
                "database": database.strip(),
                "address_line1": address_line1.strip(),
                "address_line2": address_line2.strip(),
                "auth_method": auth_method,
                "ksef_cert_path": ksef_cert_path.strip(),
                "purchase_register": purchase_register.strip() or "ZAKUP",
                "lp_numbering": lp_numbering,
            },
            writes_enabled=(writes_enabled == "on"),
        )
        store = SecretStore()
        env = context.cfg.ksef.environment
        if ksef_token:
            store.set(SecretStore.ksef_token_key(edit_id, env), ksef_token)
        if p12_password:
            store.set(SecretStore.p12_pass_key(edit_id), p12_password)
        context.reload()
        context.audit.log(
            "company_updated", edit_id, "company", edit_id,
            context.cfg.ksef.environment,
            {"nip": nip.strip(), "database": database.strip(),
             "writes_enabled": writes_enabled == "on"},
        )
        return _render_edit(request, context, c, edit_id, "Zmiany zapisane", True)
    except configstore.ConfigStoreError as exc:
        return _render_edit(request, context, c, edit_id, str(exc), False)


# -- testy polaczen --


@router.post("/settings/test-optima")
def test_optima(
    request: Request, c: str = Form(...), csrf: str = Form(...),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    try:
        conn = context.optima.read_connection(context.company(c))
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM CDN.TraNag WHERE TrN_TypDokumentu = 302")
        count = cur.fetchone()[0]
        conn.close()
        return _render(request, context, c,
                       f"Polaczenie OK - {count} faktur sprzedazy w bazie", True)
    except Exception as exc:  # noqa: BLE001
        return _render(request, context, c, f"Blad polaczenia z Optima: {exc}", False)


@router.post("/settings/test-ksef")
def test_ksef(
    request: Request, c: str = Form(...), csrf: str = Form(...),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    try:
        context.gateway(c).auth_ok()
        return _render(request, context, c, "Uwierzytelnienie w KSeF OK", True)
    except Exception as exc:  # noqa: BLE001
        return _render(request, context, c, f"Blad uwierzytelnienia KSeF: {exc}", False)
