from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.fa3.builder import build_fa3
from app.fa3.mapper import MappingError, map_invoice
from app.fa3.validator import validate
from app.state.repo import BatchRepo
from app.web.deps import AppContext, ctx

router = APIRouter()

STATUS_LABELS = {
    "previewed": "podglad",
    "queued": "w kolejce",
    "sent": "wyslano",
    "accepted": "przyjeta w KSeF",
    "duplicate": "duplikat w KSeF",
    "rejected": "odrzucona",
    "error": "blad",
}


def _default_dates() -> tuple[str, str]:
    today = date.today()
    return (today - timedelta(days=31)).isoformat(), today.isoformat()


@router.get("/sales")
def sales_list(
    request: Request,
    c: str | None = None,
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    context: AppContext = Depends(ctx),
):
    company_id = c or context.default_company_id()
    company = context.company(company_id)
    df, dt = _default_dates()
    date_from, date_to = date_from or df, date_to or dt
    env = context.cfg.ksef.environment

    reader = context.sales_reader(company_id)
    invoices = reader.list_sales(date.fromisoformat(date_from), date.fromisoformat(date_to))
    local = context.sent.statuses_for(company_id, env)

    rows = []
    for inv in invoices:
        row = {"inv": inv, "selectable": True, "badge": "blocked", "label": "niewyslana",
               "detail": None, "ksef_number": None, "upo": False}
        if inv.optima_ksef_number:
            row.update(selectable=False, badge="blocked", label="wyslana z Optimy",
                       ksef_number=inv.optima_ksef_number)
        elif inv.korekta:
            row.update(selectable=False, badge="blocked", label="korekta (poza v1)")
        elif inv.buyer is None or len(inv.buyer.nip_normalized) != 10:
            row.update(selectable=False, badge="blocked", label="brak PL NIP nabywcy")
        st = local.get(inv.trn_id)
        if st is not None:
            row.update(
                badge=st["status"], label=STATUS_LABELS.get(st["status"], st["status"]),
                detail=st["error_detail"], ksef_number=st["ksef_number"] or row["ksef_number"],
                upo=bool(st["upo_path"]),
            )
            if st["status"] in ("accepted", "sent", "queued", "duplicate"):
                row["selectable"] = False
        rows.append(row)

    return request.app.state.templates.TemplateResponse(
        request,
        "sales/list.html",
        {
            "context": context, "company_id": company_id, "company": company,
            "active": "sales", "rows": rows, "date_from": date_from, "date_to": date_to,
        },
    )


def _build_previews(context: AppContext, company_id: str, trn_ids: list[int],
                    date_from: str, date_to: str):
    company = context.company(company_id)
    reader = context.sales_reader(company_id)
    df = date.fromisoformat(date_from) - timedelta(days=366)
    dt = date.fromisoformat(date_to) + timedelta(days=31)
    ok_rows, bad_rows = [], []
    totals = {"netto": Decimal(0), "brutto": Decimal(0)}
    for trn_id in trn_ids:
        inv = reader.get_invoice(trn_id, df, dt)
        if inv is None:
            bad_rows.append({"numer": f"TrN_TrNID={trn_id}", "error": "Nie znaleziono w bazie"})
            continue
        try:
            fa3 = map_invoice(
                inv, seller_nip=company.nip, seller_name=company.name,
                seller_address_l1=company.address_line1,
                seller_address_l2=company.address_line2,
                seller_country=company.country_code,
            )
            xml = build_fa3(fa3)
            errors = validate(xml)
            if errors:
                raise MappingError("; ".join(errors[:3]))
        except MappingError as exc:
            bad_rows.append({"numer": inv.numer_pelny, "error": str(exc)})
            continue
        ok_rows.append({
            "trn_id": trn_id, "numer": inv.numer_pelny,
            "buyer_nip": inv.buyer.nip_normalized, "brutto": inv.razem_brutto,
            "waluta": inv.waluta,
        })
        totals["netto"] += inv.razem_netto
        totals["brutto"] += inv.razem_brutto
    return ok_rows, bad_rows, totals


@router.post("/sales/preview")
def sales_preview(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    trn_ids: list[int] = Form([]),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    if not trn_ids:
        return RedirectResponse(
            f"/sales?c={c}&date_from={date_from}&date_to={date_to}", status_code=303
        )
    company = context.company(c)
    ok_rows, bad_rows, totals = _build_previews(context, c, trn_ids, date_from, date_to)
    confirm_token = None
    if ok_rows:
        confirm_token = context.batches.create(
            c, context.cfg.ksef.environment,
            [r["trn_id"] for r in ok_rows],
            {"netto": str(totals["netto"]), "brutto": str(totals["brutto"])},
        )
        context.audit.log(
            "batch_previewed", c, "batch", confirm_token, context.cfg.ksef.environment,
            {"trn_ids": [r["trn_id"] for r in ok_rows], "blocked": len(bad_rows)},
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "sales/preview.html",
        {
            "context": context, "company_id": c, "company": company, "active": "sales",
            "ok_rows": ok_rows, "bad_rows": bad_rows, "totals": totals,
            "confirm_token": confirm_token, "date_from": date_from, "date_to": date_to,
            "ttl": BatchRepo.TTL_MINUTES,
        },
    )


@router.post("/sales/send")
def sales_send(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    confirm_token: str = Form(...),
    date_from: str = Form(...),
    date_to: str = Form(...),
    nip_confirm: str = Form(""),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    company = context.company(c)
    env = context.cfg.ksef.environment

    if env == "prod":
        if not context.cfg.ksef.allow_prod_send:
            raise HTTPException(403, "Wysylka produkcyjna zablokowana (allow_prod_send=false)")
        if "".join(ch for ch in nip_confirm if ch.isdigit()) != company.nip:
            raise HTTPException(403, "Przepisany NIP nie zgadza sie - wysylka odrzucona")

    batch = context.batches.consume(confirm_token)
    if batch is None:
        raise HTTPException(
            410, "Potwierdzenie wygaslo lub zostalo juz uzyte - przygotuj wysylke ponownie"
        )
    trn_ids = json.loads(batch["trn_ids_json"])
    for trn_id in trn_ids:
        context.sent.upsert(c, trn_id, env, status="queued", numer_pelny="")
    context.audit.log(
        "batch_confirmed", c, "batch", confirm_token, env, {"trn_ids": trn_ids}
    )
    context.send_service(c).send_batch_async(
        trn_ids, date.fromisoformat(date_from), date.fromisoformat(date_to)
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "sales/job.html",
        {
            "context": context, "company_id": c, "company": company,
            "active": "sales", "trn_ids": trn_ids,
        },
    )


@router.get("/jobs/send", response_class=HTMLResponse)
def job_status(
    request: Request,
    c: str,
    trn_ids: str,
    context: AppContext = Depends(ctx),
):
    ids = [int(x) for x in trn_ids.split(",") if x]
    env = context.cfg.ksef.environment
    rows = []
    done = True
    for trn_id in ids:
        st = context.sent.get(c, trn_id, env)
        if st is None:
            rows.append({"trn_id": trn_id, "numer": "?", "status": "queued",
                         "label": "w kolejce", "ksef_number": None, "error": None, "upo": False})
            done = False
            continue
        status = st["status"]
        if status in ("queued", "sent"):
            done = False
        rows.append({
            "trn_id": trn_id, "numer": st["numer_pelny"], "status": status,
            "label": STATUS_LABELS.get(status, status), "ksef_number": st["ksef_number"],
            "error": st["error_detail"], "upo": bool(st["upo_path"]),
        })
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/send_status.html",
        {"context": context, "company_id": c, "rows": rows, "done": done},
    )


@router.get("/sales/{trn_id}/xml")
def sales_xml(
    trn_id: int,
    c: str,
    date_from: str,
    date_to: str,
    context: AppContext = Depends(ctx),
):
    company = context.company(c)
    reader = context.sales_reader(c)
    inv = reader.get_invoice(
        trn_id,
        date.fromisoformat(date_from) - timedelta(days=366),
        date.fromisoformat(date_to) + timedelta(days=31),
    )
    if inv is None:
        raise HTTPException(404, "Nie znaleziono faktury")
    try:
        fa3 = map_invoice(
            inv, seller_nip=company.nip, seller_name=company.name,
            seller_address_l1=company.address_line1,
            seller_address_l2=company.address_line2,
            seller_country=company.country_code,
        )
        xml = build_fa3(fa3)
    except MappingError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(content=xml, media_type="application/xml")


@router.get("/sales/{trn_id}/upo")
def sales_upo(trn_id: int, c: str, context: AppContext = Depends(ctx)):
    st = context.sent.get(c, trn_id, context.cfg.ksef.environment)
    if st is None:
        raise HTTPException(404, "Brak UPO dla tej faktury")
    if st["upo_path"] and Path(st["upo_path"]).exists():
        content = Path(st["upo_path"]).read_bytes()
    elif st["session_ref"] and st["invoice_ref"]:
        # UPO generuje sie asynchronicznie - sprobuj pobrac teraz
        try:
            content = context.gateway(c).fetch_upo(st["session_ref"], st["invoice_ref"])
        except Exception as exc:
            raise HTTPException(
                404, f"UPO jeszcze niedostepne - sprobuj za chwile ({exc})"
            ) from exc
        upo_path = context.cfg.state.invoice_store / c / f"upo-{st['invoice_ref']}.xml"
        upo_path.parent.mkdir(parents=True, exist_ok=True)
        upo_path.write_bytes(content)
        context.sent.upsert(c, trn_id, context.cfg.ksef.environment, upo_path=str(upo_path))
    else:
        raise HTTPException(404, "Brak UPO dla tej faktury")
    return Response(
        content=content,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="upo-{trn_id}.xml"'},
    )
