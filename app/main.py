"""Fabryka aplikacji FastAPI + scheduler pobierania zakupow."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import AppConfig, load_config
from app.web.deps import AppContext
from app.web.routes import audit, dashboard, purchases, sales, settings

logger = logging.getLogger(__name__)


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

    templates_dir = Path(str(resources.files("app.web").joinpath("templates")))
    static_dir = Path(str(resources.files("app.web").joinpath("static")))
    app.state.templates = Jinja2Templates(directory=str(templates_dir))
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    app.include_router(dashboard.router)
    app.include_router(sales.router)
    app.include_router(purchases.router)
    app.include_router(settings.router)
    app.include_router(audit.router)
    return app
