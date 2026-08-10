from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.fa3.parser import parse_invoice
from app.ksef.ratelimit import RateLimitExceeded
from app.optima.contractors import ksef_number_known, vat_register_has
from app.optima.writer import (
    ContractorMissingError,
    PurchaseWriter,
    VerificationError,
    WriteBlockedError,
    export_as_xml,
)
from app.web.deps import AppContext, ctx

router = APIRouter()

STATUS_LABELS = {
    "new": "nowa",
    "needs_contractor": "brak kontrahenta",
    "already_in_optima": "juz w Optimie",
    "exported": "wyeksportowana",
    "skipped": "pominieta",
    "error": "blad",
}


def _render_list(request: Request, context: AppContext, company_id: str, sync_result=None):
    company = context.company(company_id)
    env = context.cfg.ksef.environment
    rows = context.downloads.list_for(company_id, env)
    budget = context.limiter.remaining(company_id, "invoice_metadata_or_export")
    resets = context.limiter.resets_at(company_id, "invoice_metadata_or_export")
    last_sync = context.sync_state.get(company_id, f"purchase_hwm:{env}")
    return request.app.state.templates.TemplateResponse(
        request,
        "purchases/list.html",
        {
            "context": context, "company_id": company_id, "company": company,
            "active": "purchases", "rows": rows, "status_labels": STATUS_LABELS,
            "budget": budget,
            "resets_at": resets.astimezone().strftime("%H:%M") if resets else None,
            "last_sync": last_sync, "sync_result": sync_result,
        },
    )


@router.get("/purchases")
def purchases_list(request: Request, c: str | None = None, context: AppContext = Depends(ctx)):
    company_id = c or context.default_company_id()
    return _render_list(request, context, company_id)


@router.post("/purchases/sync")
def purchases_sync(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    try:
        stats = context.download_service(c).sync()
        result = (
            f"Synchronizacja OK: stron {stats['pages']}, nowych {stats['new']}, "
            f"znanych {stats['known']}"
        )
    except RateLimitExceeded as exc:
        result = str(exc)
    except Exception as exc:  # noqa: BLE001
        result = f"Blad synchronizacji: {exc}"
    return _render_list(request, context, c, sync_result=result)


@router.post("/purchases/export")
def purchases_export(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    mode: str = Form(...),
    ids: list[int] = Form([]),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    if not ids:
        return RedirectResponse(f"/purchases?c={c}", status_code=303)
    company = context.company(c)
    env = context.cfg.ksef.environment

    invoices = []
    for invoice_id in ids:
        row = context.downloads.get(invoice_id)
        if row is None or row["company_id"] != c:
            continue
        if not row["xml_path"] or not Path(row["xml_path"]).exists():
            context.downloads.set_status(invoice_id, "error", error_detail="Brak pliku XML")
            continue
        parsed = parse_invoice(Path(row["xml_path"]).read_bytes(), row["ksef_number"])
        invoices.append((row, parsed))

    if mode == "xml":
        xml = export_as_xml([p for _, p in invoices], company.purchase_register)
        context.audit.log(
            "purchases_export_xml", c, environment=env,
            details={"ids": ids, "count": len(invoices)},
        )
        return Response(
            content=xml,
            media_type="application/xml",
            headers={"Content-Disposition": 'attachment; filename="rejestr_zakupu_vat.xml"'},
        )

    if mode == "direct":
        write_conn = context.optima.write_connection(company)
        read_conn = context.optima.read_connection(company)
        spec_path = context.cfg.state.invoice_store.parent / f"insert_spec-{c}.json"
        writer = PurchaseWriter(write_conn, company, spec_path)
        for row, parsed in invoices:
            invoice_id = row["id"]
            # deduplikacja z natywnym obiegiem KSeF Optimy
            if ksef_number_known(read_conn, row["ksef_number"]):
                context.downloads.set_status(invoice_id, "already_in_optima")
                continue
            if vat_register_has(read_conn, parsed.seller_nip, parsed.invoice_number):
                context.downloads.set_status(
                    invoice_id, "already_in_optima",
                    error_detail="Wpis (NIP, numer) juz istnieje w rejestrze VAT",
                )
                continue
            acq = (row["storage_date"] or "")[:10]
            acq_date = date.fromisoformat(acq) if acq else datetime.now(UTC).date()
            try:
                van_id = writer.export_direct(parsed, acq_date)
                context.downloads.set_status(
                    invoice_id, "exported", van_id=van_id,
                    exported_at=datetime.now(UTC).isoformat(),
                )
                context.audit.log(
                    "purchase_exported", c, "purchase_invoice", row["ksef_number"], env,
                    {"van_id": van_id, "invoice_number": parsed.invoice_number,
                     "gross": str(parsed.total_gross)},
                )
            except ContractorMissingError as exc:
                context.downloads.set_status(
                    invoice_id, "needs_contractor", error_detail=str(exc)
                )
            except (WriteBlockedError, VerificationError) as exc:
                context.downloads.set_status(invoice_id, "error", error_detail=str(exc))
            except Exception as exc:  # noqa: BLE001
                context.downloads.set_status(
                    invoice_id, "error", error_detail=f"Blad INSERT: {exc}"
                )
        return RedirectResponse(f"/purchases?c={c}", status_code=303)

    raise HTTPException(400, f"Nieznany tryb eksportu: {mode}")


@router.get("/purchases/{invoice_id}")
def purchase_detail(
    request: Request, invoice_id: int, c: str, context: AppContext = Depends(ctx)
):
    row = context.downloads.get(invoice_id)
    if row is None or row["company_id"] != c:
        raise HTTPException(404, "Nie znaleziono faktury")
    parsed = None
    if row["xml_path"] and Path(row["xml_path"]).exists():
        try:
            parsed = parse_invoice(Path(row["xml_path"]).read_bytes(), row["ksef_number"])
        except Exception:  # noqa: BLE001
            parsed = None
    return request.app.state.templates.TemplateResponse(
        request,
        "purchases/detail.html",
        {
            "context": context, "company_id": c, "company": context.company(c),
            "active": "purchases", "row": row, "parsed": parsed,
        },
    )


@router.get("/purchases/{invoice_id}/xml")
def purchase_xml(invoice_id: int, c: str, context: AppContext = Depends(ctx)):
    row = context.downloads.get(invoice_id)
    if row is None or row["company_id"] != c or not row["xml_path"]:
        raise HTTPException(404, "Brak XML")
    path = Path(row["xml_path"])
    if not path.exists():
        raise HTTPException(404, "Plik XML nie istnieje")
    return Response(
        content=path.read_bytes(),
        media_type="application/xml",
        headers={
            "Content-Disposition": f'attachment; filename="{row["ksef_number"]}.xml"'
        },
    )
