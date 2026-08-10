from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request

from app.web.deps import AppContext, ctx

router = APIRouter()


def _render(request: Request, context: AppContext, company_id: str,
            test_result: str | None = None, test_ok: bool = True):
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "context": context, "company_id": company_id,
            "company": context.company(company_id), "active": "settings",
            "test_result": test_result, "test_ok": test_ok,
        },
    )


@router.get("/settings")
def settings(request: Request, c: str | None = None, context: AppContext = Depends(ctx)):
    return _render(request, context, c or context.default_company_id())


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
