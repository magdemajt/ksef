"""Walidacja XML FA(3) przed wysylka: XSD + reguly techniczne KSeF.

Bramka obowiazkowa: uruchamiana przy preview ORAZ ponownie tuz przed wysylka.
"""

from __future__ import annotations

import threading
from importlib import resources

from lxml import etree

FA3_NAMESPACE = "http://crd.gov.pl/wzor/2025/06/25/13775/"
MAX_INVOICE_BYTES = 1_000_000  # limit KSeF dla faktury bez zalacznika

_schema_lock = threading.Lock()
_schema: etree.XMLSchema | None = None


class _LocalResolver(etree.Resolver):
    """Przekierowuje importy XSD (URL crd.gov.pl) na przypiete pliki lokalne."""

    def resolve(self, system_url, public_id, context):
        if not system_url:
            return None
        basename = system_url.rsplit("/", 1)[-1]
        target = resources.files("app.fa3.schemas").joinpath(basename)
        if target.is_file():
            return self.resolve_string(target.read_bytes(), context, base_url=system_url)
        return None


def _load_schema() -> etree.XMLSchema:
    global _schema
    with _schema_lock:
        if _schema is None:
            parser = etree.XMLParser()
            parser.resolvers.add(_LocalResolver())
            xsd_path = resources.files("app.fa3.schemas").joinpath("schemat_FA(3)_v1-0E.xsd")
            doc = etree.parse(str(xsd_path), parser)
            _schema = etree.XMLSchema(doc)
        return _schema


def validate(xml: bytes) -> list[str]:
    """Zwraca liste bledow; pusta lista = XML gotowy do wysylki."""
    errors: list[str] = []

    if xml.startswith(b"\xef\xbb\xbf"):
        errors.append("Plik zaczyna sie od BOM UTF-8 - KSeF odrzuca BOM")
        xml = xml[3:]

    if len(xml) > MAX_INVOICE_BYTES:
        errors.append(
            f"Faktura ma {len(xml)} B, limit KSeF bez zalacznika to {MAX_INVOICE_BYTES} B"
        )

    try:
        doc = etree.fromstring(xml)
    except etree.XMLSyntaxError as exc:
        return errors + [f"Niepoprawny XML: {exc}"]

    # KSeF odrzuca processing instructions - takze przed/po elemencie glownym
    pi_nodes = [n for n in doc.iter() if isinstance(n, etree._ProcessingInstruction)]
    sibling = doc.getprevious()
    while sibling is not None:
        if isinstance(sibling, etree._ProcessingInstruction):
            pi_nodes.append(sibling)
        sibling = sibling.getprevious()
    sibling = doc.getnext()
    while sibling is not None:
        if isinstance(sibling, etree._ProcessingInstruction):
            pi_nodes.append(sibling)
        sibling = sibling.getnext()
    for node in pi_nodes:
        errors.append(f"Dokument zawiera processing instruction: <?{node.target}?>")

    if not doc.tag.startswith(f"{{{FA3_NAMESPACE}}}"):
        errors.append(
            f"Element glowny w przestrzeni {etree.QName(doc).namespace!r}, "
            f"oczekiwano {FA3_NAMESPACE!r}"
        )
        return errors

    schema = _load_schema()
    if not schema.validate(doc):
        for err in schema.error_log:
            errors.append(f"XSD linia {err.line}: {err.message}")
    return errors
