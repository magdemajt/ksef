"""Fabryka aplikacji FastAPI + scheduler synchronizacji zakupow i kolejka pobierania XML."""

from __future__ import annotations

import asyncio
import logging
import time
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


QUEUE_POLL_SECONDS = 3
SLOW_REQUEST_SECONDS = 1.0


async def _queue_worker(app: FastAPI) -> None:
    """Pobiera XML faktur, ktore uzytkownik wstawil do kolejki (zakladka Zakupy).

    Kolejka kazdej firmy jest sprawdzana co kilka sekund; process_queue konczy sie sam,
    gdy kolejka jest pusta, zatrzymana albo wyczerpal sie limit KSeF (16/min, 64/h).
    """
    context: AppContext = app.state.ctx
    # Sprzatanie po poprzednim uruchomieniu dopiero po chwili: druga, omylkowo uruchomiona
    # kopia programu konczy sie od razu (zajety port) i nie moze ruszyc stanu dzialajacej.
    await asyncio.sleep(QUEUE_POLL_SECONDS)
    context.xml_queue.release_all()  # zadna pozycja nie jest w trakcie pobierania
    interrupted = context.sent.reset_interrupted()
    if interrupted:
        logger.warning("Przerwane wysylki sprzedazy oznaczone jako blad: %d", interrupted)
    while True:
        await asyncio.sleep(QUEUE_POLL_SECONDS)
        env = context.cfg.ksef.environment
        for company_id in list(context.cfg.companies):
            try:
                if not context.xml_queue.count(company_id, env):
                    continue
                service = context.download_service(company_id)
                if not service.queue_ready():
                    continue
                result = await asyncio.to_thread(service.process_queue)
                if result["fetched"] or result["failed"]:
                    logger.info("Kolejka XML %s: %s", company_id, result)
            except Exception:
                logger.exception("Kolejka XML: pobieranie dla %s nie powiodlo sie", company_id)


async def _sync_scheduler(app: FastAPI) -> None:
    """Cyklicznie pobiera metadane nowych faktur zakupu dla kazdej firmy (bez XML)."""
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
        tasks = [
            asyncio.create_task(_sync_scheduler(app)),
            asyncio.create_task(_queue_worker(app)),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            app.state.ctx.conn.close()

    app = FastAPI(title="ksef-bridge", lifespan=lifespan)

    @app.middleware("http")
    async def log_slow_requests(request, call_next):
        # diagnostyka "aplikacja dziala wolno": w konsoli widac, ktora strona i ile trwala
        start = time.perf_counter()
        response = await call_next(request)
        elapsed = time.perf_counter() - start
        if elapsed >= SLOW_REQUEST_SECONDS:
            logger.warning(
                "Wolne zadanie: %s %s - %.1f s", request.method, request.url.path, elapsed
            )
        return response

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
