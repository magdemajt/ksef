"""Fabryka aplikacji FastAPI + scheduler pobierania zakupow."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from importlib import resources
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, load_config
from app.web.deps import AppContext
from app.web.routes import audit, dashboard, purchases, sales, settings, setup

logger = logging.getLogger(__name__)


def _fmt_qty(value: Decimal) -> str:
    """Ilosc bez zbednych zer: 1.000 -> 1, 2.50 -> 2.5."""
    text = f"{value:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _fmt_price(value: Decimal) -> str:
    """Cena z co najmniej 2 miejscami; dalsze miejsca tylko gdy znaczace (0.0125)."""
    digits = max(2, -int(value.normalize().as_tuple().exponent))
    return f"{value:.{digits}f}"


async def _sync_scheduler(app: FastAPI) -> None:
    """Cyklicznie pobiera nowe faktury zakupu dla kazdej firmy (rozlozone w czasie)."""
    context: AppContext = app.state.ctx
    interval = context.cfg.sync.poll_interval_minutes * 60
    await asyncio.sleep(30)  # daj aplikacji wystartowac
    while True:
        for company_id in context.cfg.companies:
            try:
                stats = await asyncio.to_thread(
                    context.download_service(company_id).sync
                )
                logger.info("Scheduler sync %s: %s", company_id, stats)
            except Exception:
                logger.exception("Scheduler: synchronizacja %s nie powiodla sie", company_id)
            await asyncio.sleep(10)  # odstep miedzy firmami
        await asyncio.sleep(interval)


def build_templates() -> Jinja2Templates:
    templates_dir = Path(str(resources.files("app.web").joinpath("templates")))
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.filters["qty"] = _fmt_qty
    templates.env.filters["price"] = _fmt_price
    return templates


def create_app(cfg: AppConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ctx = AppContext.create(cfg)
        task = asyncio.create_task(_sync_scheduler(app))
        try:
            yield
        finally:
            task.cancel()
            app.state.ctx.conn.close()

    app = FastAPI(title="ksef-bridge", lifespan=lifespan)

    static_dir = Path(str(resources.files("app.web").joinpath("static")))
    app.state.templates = build_templates()
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(setup.router)
    app.include_router(dashboard.router)
    app.include_router(sales.router)
    app.include_router(purchases.router)
    app.include_router(settings.router)
    app.include_router(audit.router)
    return app
