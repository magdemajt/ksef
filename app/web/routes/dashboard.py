from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.web.deps import AppContext, ctx

router = APIRouter()


@router.get("/")
def dashboard(request: Request, c: str | None = None, context: AppContext = Depends(ctx)):
    company_id = c or context.default_company_id()
    company = context.company(company_id)
    env = context.cfg.ksef.environment

    sent = context.sent.statuses_for(company_id, env)
    downloads = context.downloads.list_for(company_id, env)
    stats = {
        "accepted": sum(1 for r in sent.values() if r["status"] == "accepted"),
        "rejected": sum(1 for r in sent.values() if r["status"] in ("rejected", "error")),
        "duplicate": sum(1 for r in sent.values() if r["status"] == "duplicate"),
        "downloaded": len(downloads),
        "new": sum(1 for r in downloads if r["status"] in ("new", "needs_contractor")),
        "exported": sum(1 for r in downloads if r["status"] == "exported"),
    }
    budget = context.limiter.remaining(company_id, "invoice_metadata_or_export")
    resets = context.limiter.resets_at(company_id, "invoice_metadata_or_export")
    return request.app.state.templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "context": context,
            "company_id": company_id,
            "company": company,
            "active": "dashboard",
            "stats": stats,
            "budget": budget,
            "resets_at": resets.astimezone().strftime("%H:%M") if resets else None,
        },
    )
