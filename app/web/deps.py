"""Kontekst aplikacji: konfiguracja, stan SQLite, fabryki serwisow per firma."""

from __future__ import annotations

import secrets as pysecrets
import sqlite3
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from app.config import AppConfig, CompanyConfig
from app.ksef.downloader import DownloadService
from app.ksef.gateway import KsefGateway
from app.ksef.ratelimit import RateLimiter
from app.ksef.sender import SendService
from app.optima.db import OptimaDb
from app.optima.reader import SalesReader
from app.secrets import SecretStore
from app.state import db as state_db
from app.state.repo import (
    AuditRepo,
    BatchRepo,
    DownloadRepo,
    SentRepo,
    SessionRepo,
    SyncStateRepo,
)


@dataclass
class AppContext:
    cfg: AppConfig
    conn: sqlite3.Connection
    secrets: SecretStore
    csrf_token: str
    _gateways: dict[str, KsefGateway] = field(default_factory=dict)

    @classmethod
    def create(cls, cfg: AppConfig) -> AppContext:
        conn = state_db.connect(cfg.state.sqlite_path)
        return cls(
            cfg=cfg,
            conn=conn,
            secrets=SecretStore(),
            csrf_token=pysecrets.token_urlsafe(24),
        )

    # -- repozytoria --

    @property
    def sent(self) -> SentRepo:
        return SentRepo(self.conn)

    @property
    def batches(self) -> BatchRepo:
        return BatchRepo(self.conn)

    @property
    def sessions(self) -> SessionRepo:
        return SessionRepo(self.conn)

    @property
    def downloads(self) -> DownloadRepo:
        return DownloadRepo(self.conn)

    @property
    def sync_state(self) -> SyncStateRepo:
        return SyncStateRepo(self.conn)

    @property
    def audit(self) -> AuditRepo:
        return AuditRepo(self.conn)

    @property
    def limiter(self) -> RateLimiter:
        return RateLimiter(self.conn)

    @property
    def optima(self) -> OptimaDb:
        return OptimaDb(self.cfg, self.secrets)

    # -- firmy --

    def company(self, company_id: str) -> CompanyConfig:
        try:
            return self.cfg.company(company_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc)) from exc

    def default_company_id(self) -> str:
        return next(iter(self.cfg.companies))

    def gateway(self, company_id: str) -> KsefGateway:
        if company_id not in self._gateways:
            self._gateways[company_id] = KsefGateway(
                company_id,
                self.company(company_id),
                self.cfg.ksef.environment,
                self.secrets,
                self.limiter,
            )
        return self._gateways[company_id]

    def sales_reader(self, company_id: str) -> SalesReader:
        conn = self.optima.read_connection(self.company(company_id))
        return SalesReader(conn)

    def send_service(self, company_id: str) -> SendService:
        return SendService(
            company_id=company_id,
            company=self.company(company_id),
            env=self.cfg.ksef.environment,
            gateway=self.gateway(company_id),
            reader=self.sales_reader(company_id),
            sent_repo=self.sent,
            session_repo=self.sessions,
            audit=self.audit,
            invoice_store=self.cfg.state.invoice_store,
        )

    def download_service(self, company_id: str) -> DownloadService:
        return DownloadService(
            company_id=company_id,
            company=self.company(company_id),
            env=self.cfg.ksef.environment,
            gateway=self.gateway(company_id),
            limiter=self.limiter,
            downloads=self.downloads,
            sync_state=self.sync_state,
            audit=self.audit,
            invoice_store=self.cfg.state.invoice_store,
            backfill_start=self.cfg.sync.backfill_start_date,
        )

    def check_csrf(self, token: str | None) -> None:
        if token != self.csrf_token:
            raise HTTPException(403, "Niepoprawny token CSRF - odswiez strone")


def ctx(request: Request) -> AppContext:
    return request.app.state.ctx
