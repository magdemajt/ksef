from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.web.deps import AppContext, ctx

router = APIRouter()


@router.get("/audit")
def audit_view(request: Request, c: str | None = None, context: AppContext = Depends(ctx)):
    company_id = c or context.default_company_id()
    return request.app.state.templates.TemplateResponse(
        request,
        "audit.html",
        {
            "context": context, "company_id": company_id,
            "company": context.company(company_id), "active": "audit",
            "rows": context.audit.recent(200),
        },
    )
