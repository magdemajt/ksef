"""Modele aplikacji dla warstwy KSeF (niezalezne od typow biblioteki ksef2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class SendStatus(StrEnum):
    QUEUED = "queued"
    SENT = "sent"
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass
class InvoiceSendResult:
    trn_id: int
    numer_pelny: str
    status: SendStatus
    invoice_ref: str | None = None
    ksef_number: str | None = None
    error_code: int | None = None
    error_detail: str | None = None


@dataclass
class PurchaseInvoiceMeta:
    ksef_number: str
    invoice_number: str
    seller_nip: str
    seller_name: str
    issue_date: str
    storage_date: str
    net: float | None
    vat: float | None
    gross: float | None
    currency: str | None


@dataclass
class MetadataPage:
    invoices: list[PurchaseInvoiceMeta] = field(default_factory=list)
    has_more: bool = False
    is_truncated: bool = False
    hwm_date: datetime | None = None
