from __future__ import annotations

import contextlib
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.fa3.parser import parse_invoice
from app.ksef.downloader import blocked_until, queue_paused, set_queue_paused
from app.ksef.ratelimit import RateLimitExceeded
from app.optima.contractors import (
    find_by_nip,
    find_vat_entries,
    ksef_number_known,
    ksef_numbers_known,
    normalize_nip,
    payment_form_guids,
    vat_register_has,
)
from app.optima.writer import (
    ContractorMissingError,
    PurchaseWriter,
    VerificationError,
    WriteBlockedError,
    export_as_xml,
)
from app.web.deps import AppContext, ctx

router = APIRouter()
logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "new": "nowa",
    "needs_contractor": "brak kontrahenta",
    "already_in_optima": "juz w Optimie",
    "exported": "w rejestrze VAT",
    "skipped": "pominieta",
    "error": "blad",
}

# statusy, przy ktorych faktura czeka jeszcze na trafienie do Optimy
OPEN_STATUSES = ("new", "needs_contractor", "error")
QUEUE_VIEW_LIMIT = 50  # tyle pozycji kolejki pokazuje panel; reszta jako licznik


def _check_in_optima(context: AppContext, company_id: str, *, only_exported: bool) -> dict:
    """Uzgadnia statusy z baza Optimy - import pliku XML odbywa sie poza aplikacja.

    Faktura wyslana wczesniej do pliku XML i znaleziona w rejestrze VAT -> "exported"
    (z VaN_VaNID); pozostale znalezione w Optimie -> "already_in_optima".
    only_exported ogranicza sprawdzanie do faktur czekajacych na import pliku.
    """
    env = context.cfg.ksef.environment
    rows = [
        row for row in context.downloads.list_for(company_id, env)
        if row["status"] in OPEN_STATUSES and (row["exported_at"] or not only_exported)
    ]
    result = {"checked": len(rows), "in_register": 0, "already": 0}
    if not rows:
        return result
    conn = context.optima.read_connection(context.company(company_id))
    try:
        # cala lista w 3 zapytaniach - nie po kilka na fakture (strona laduje sie przy kazdym wejsciu)
        in_register = find_vat_entries(conn, [
            (row["ksef_number"], row["seller_nip"] or "", row["invoice_number"] or "")
            for row in rows
        ])
        known = ksef_numbers_known(
            conn, [row["ksef_number"] for row in rows if row["ksef_number"] not in in_register]
        )
        for row in rows:
            van_id = in_register.get(row["ksef_number"])
            if van_id is not None and row["exported_at"]:
                context.downloads.set_status(
                    row["id"], "exported", van_id=van_id, error_detail=None
                )
                result["in_register"] += 1
            elif van_id is not None or row["ksef_number"] in known:
                context.downloads.set_status(row["id"], "already_in_optima", error_detail=None)
                result["already"] += 1
    finally:
        with contextlib.suppress(Exception):
            conn.close()
    if result["in_register"] or result["already"]:
        context.audit.log(
            "purchases_checked_in_optima", company_id, environment=env, details=result
        )
    return result


DATE_FIELDS = {"issue": "issue_date", "storage": "storage_date"}
DEFAULT_SORT = ("storage", "desc")  # najnowsze w KSeF na gorze


def _date_filter(request: Request | None) -> dict:
    """Filtr dat i sortowanie listy z parametrow adresu (POST przenosi je w adresie akcji)."""
    params = request.query_params if request is not None else {}

    def iso_day(name: str) -> str:
        raw = (params.get(name) or "").strip()
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            return ""

    field = params.get("date_field") or "issue"
    sort = params.get("sort") or DEFAULT_SORT[0]
    direction = params.get("dir") or DEFAULT_SORT[1]
    return {
        "date_from": iso_day("date_from"),
        "date_to": iso_day("date_to"),
        "date_field": field if field in DATE_FIELDS else "issue",
        "sort": sort if sort in DATE_FIELDS else DEFAULT_SORT[0],
        "dir": direction if direction in ("asc", "desc") else DEFAULT_SORT[1],
    }


def _has_dates(flt: dict) -> bool:
    return bool(flt["date_from"] or flt["date_to"])


def _filter_query(flt: dict, prefix: str, *, dates: bool = True) -> str:
    """Parametry widoku do doklejenia do adresu; pusty napis dla widoku domyslnego."""
    params = {}
    if dates and _has_dates(flt):
        params = {k: flt[k] for k in ("date_from", "date_to", "date_field") if flt[k]}
    if (flt["sort"], flt["dir"]) != DEFAULT_SORT:
        params |= {"sort": flt["sort"], "dir": flt["dir"]}
    return prefix + urlencode(params) if params else ""


def _list_url(request: Request | None, company_id: str) -> str:
    return f"/purchases?c={company_id}" + _filter_query(_date_filter(request), "&")


def _in_range(row, flt: dict) -> bool:
    day = (row[DATE_FIELDS[flt["date_field"]]] or "")[:10]
    if not day:
        return True  # bez daty nie da sie ocenic - nie ukrywamy faktury
    return (not flt["date_from"] or day >= flt["date_from"]) and (
        not flt["date_to"] or day <= flt["date_to"]
    )


def _sorted(rows: list, flt: dict) -> list:
    """Sortuje po wybranej dacie; faktury bez tej daty zawsze na koncu."""
    column = DATE_FIELDS[flt["sort"]]
    dated = sorted(
        (row for row in rows if row[column]),
        key=lambda row: (row[column], row["id"]),
        reverse=flt["dir"] == "desc",
    )
    return dated + [row for row in rows if not row[column]]


def _sort_headers(flt: dict, company_id: str) -> dict:
    """Dla kazdej kolumny daty: adres po kliknieciu naglowka i strzalka biezacego kierunku."""
    headers = {}
    for key in DATE_FIELDS:
        active = flt["sort"] == key
        # klikniecie aktywnej kolumny odwraca kierunek; nowa kolumna zaczyna od najnowszych
        direction = "asc" if active and flt["dir"] == "desc" else "desc"
        target = {**flt, "sort": key, "dir": direction}
        headers[key] = {
            "url": f"/purchases?c={company_id}" + _filter_query(target, "&"),
            "arrow": ("▼" if flt["dir"] == "desc" else "▲") if active else "",
        }
    return headers


def _needs_xml(row) -> bool:
    return not row["xml_path"] and row["status"] in ("new", "error")


def _queue_view(context: AppContext, company_id: str, *, opened: bool) -> dict:
    """Dane panelu kolejki pobierania XML (pelna strona i odswiezany fragment htmx)."""
    env = context.cfg.ksef.environment
    total = context.xml_queue.count(company_id, env)
    # zwiniety panel pokazuje tylko naglowek - wystarczy pierwsza pozycja (ta pobierana)
    items = context.xml_queue.items(company_id, env, QUEUE_VIEW_LIMIT if opened else 1)
    waiting = [item for item in items if item["state"] == "queued"]
    blocked = blocked_until(context.sync_state, company_id, env)
    return {
        "context": context, "company_id": company_id, "queue_open": opened,
        "queue_total": total, "queue_items": items if opened else [],
        "queue_current": next((i for i in items if i["state"] == "downloading"), None),
        "queue_first_id": waiting[0]["invoice_id"] if waiting else None,
        "queue_last_id": waiting[-1]["invoice_id"] if waiting else None,
        "queue_hidden": max(0, total - len(items)) if opened else 0,
        "queue_paused": queue_paused(context.sync_state, company_id, env),
        "queue_blocked": blocked.astimezone().strftime("%H:%M:%S") if blocked else None,
    }


def _render_queue(request: Request, context: AppContext, company_id: str, *, opened: bool = True):
    return request.app.state.templates.TemplateResponse(
        request, "partials/xml_queue.html",
        {**_queue_view(context, company_id, opened=opened), "queue_oob": True},
    )


def _render_list(request: Request, context: AppContext, company_id: str, sync_result=None):
    company = context.company(company_id)
    env = context.cfg.ksef.environment
    all_rows = context.downloads.list_for(company_id, env)
    flt = _date_filter(request)
    rows = _sorted([row for row in all_rows if _in_range(row, flt)], flt)
    queued_ids = context.xml_queue.ids(company_id, env)
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
            "pending_xml": sum(1 for row in all_rows if _needs_xml(row)),
            # faktury bez XML w biezacym widoku - tyle obejmie przycisk "Pobierz XML"
            "loadable": sum(1 for row in rows if _needs_xml(row)),
            "queued_ids": queued_ids,
            **_queue_view(context, company_id, opened=False),
            "total_rows": len(all_rows), "filter": flt, "dates_active": _has_dates(flt),
            "filter_qs": _filter_query(flt, "?"),
            "clear_url": f"/purchases?c={company_id}" + _filter_query(flt, "&", dates=False),
            "sort_headers": _sort_headers(flt, company_id),
        },
    )


@router.get("/purchases")
def purchases_list(request: Request, c: str | None = None, context: AppContext = Depends(ctx)):
    company_id = c or context.default_company_id()
    message = None
    try:
        # faktury z pliku XML: sprawdz, czy import w Optimie juz je wprowadzil
        found = _check_in_optima(context, company_id, only_exported=True)
        if found["in_register"]:
            message = f"Optima: {found['in_register']} faktur z pliku XML jest juz w rejestrze VAT"
    except Exception as exc:  # noqa: BLE001
        logger.warning("Nie udalo sie sprawdzic statusow w Optimie: %s", exc)
    return _render_list(request, context, company_id, sync_result=message)


@router.post("/purchases/check")
def purchases_check(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    try:
        found = _check_in_optima(context, c, only_exported=False)
        result = (
            f"Sprawdzono {found['checked']}: w rejestrze VAT po imporcie pliku "
            f"{found['in_register']}, juz w Optimie {found['already']}"
        )
    except Exception as exc:  # noqa: BLE001
        result = f"Nie udalo sie sprawdzic w Optimie: {exc}"
    return _render_list(request, context, c, sync_result=result)


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
        if stats.get("pending"):
            result += f", bez pobranego XML: {stats['pending']} (przycisk \"Pobierz XML\")"
    except RateLimitExceeded as exc:
        result = str(exc)
    except Exception as exc:  # noqa: BLE001
        result = f"Blad synchronizacji: {exc}"
    return _render_list(request, context, c, sync_result=result)


@router.post("/purchases/queue/add")
def purchases_queue_add(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    front: bool = Form(False),
    context: AppContext = Depends(ctx),
):
    """Wstawia do kolejki XML faktury z biezacego widoku (filtr dat), w kolejnosci z listy."""
    context.check_csrf(csrf)
    env = context.cfg.ksef.environment
    flt = _date_filter(request)
    rows = _sorted(
        [row for row in context.downloads.list_for(c, env) if _in_range(row, flt)], flt
    )
    ids = [row["id"] for row in rows if _needs_xml(row)]
    added = context.xml_queue.add(c, env, ids, front=front)
    if added["added"] or added["moved"]:
        context.audit.log(
            "xml_queue_add", c, environment=env,
            details={**added, "front": front,
                     **{k: flt[k] for k in ("date_from", "date_to", "date_field")}},
        )
    result = f"Kolejka XML: dodano {added['added']}"
    if added["moved"]:
        result += f", przesunieto na poczatek {added['moved']}"
    if not ids:
        result = "Wszystkie faktury z tego widoku maja juz XML"
    elif queue_paused(context.sync_state, c, env):
        result += ' - kolejka jest zatrzymana, kliknij "Wznow" w panelu kolejki'
    return _render_list(request, context, c, sync_result=result)


@router.get("/purchases/queue")
def purchases_queue(
    request: Request, c: str, open: int = 0, context: AppContext = Depends(ctx)
):
    """Fragment htmx: panel kolejki (odswiezany co kilka sekund, takze po zwinieciu)."""
    return _render_queue(request, context, c, opened=bool(open))


@router.post("/purchases/queue/move")
def purchases_queue_move(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    id: int = Form(...),
    where: str = Form(...),
    context: AppContext = Depends(ctx),
):
    context.check_csrf(csrf)
    env = context.cfg.ksef.environment
    if where == "remove":
        context.xml_queue.remove_queued(c, env, id)
    else:
        context.xml_queue.move(c, env, id, where)
    return _render_queue(request, context, c)


@router.post("/purchases/queue/control")
def purchases_queue_control(
    request: Request,
    c: str = Form(...),
    csrf: str = Form(...),
    action: str = Form(...),
    context: AppContext = Depends(ctx),
):
    """Zatrzymanie, wznowienie i wyczyszczenie kolejki pobierania XML."""
    context.check_csrf(csrf)
    env = context.cfg.ksef.environment
    if action in ("stop", "resume"):
        set_queue_paused(context.sync_state, c, env, action == "stop")
        context.audit.log(f"xml_queue_{action}", c, environment=env)
    elif action == "purge":
        removed = context.xml_queue.purge(c, env)
        context.audit.log("xml_queue_purge", c, environment=env, details={"removed": removed})
    else:
        raise HTTPException(400, f"Nieznana akcja kolejki: {action}")
    return _render_queue(request, context, c)


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
        return RedirectResponse(_list_url(request, c), status_code=303)
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
        if not company.offline_target_id:
            # Optima odrzuca plik z pustym BAZA_DOC_ID ("Niezgodnosc identyfikatora bazy docelowej")
            return _render_list(
                request, context, c,
                sync_result="Eksport XML przerwany - ustaw identyfikator bazy w pracy rozproszonej "
                "(Ustawienia -> Firmy -> edycja firmy), taki sam jak w Optimie: Konfiguracja -> "
                "Firma -> Ogolne -> Praca rozproszona",
            )
        # Bez bazy Optimy nie da sie sprawdzic duplikatow ani dopasowac kontrahentow -
        # natywny modul KSeF Optimy mogl juz pobrac te same faktury.
        try:
            read_conn = context.optima.read_connection(company)
        except Exception as exc:  # noqa: BLE001
            return _render_list(
                request, context, c,
                sync_result=f"Eksport XML przerwany - brak polaczenia z baza Optimy: {exc}",
            )
        to_export, contractors, skipped = [], {}, 0
        for row, parsed in invoices:
            invoice_id = row["id"]
            if ksef_number_known(read_conn, row["ksef_number"]) or vat_register_has(
                read_conn, parsed.seller_nip, parsed.invoice_number
            ):
                context.downloads.set_status(invoice_id, "already_in_optima")
                skipped += 1
                continue
            if (parsed.currency or "PLN").upper() != "PLN":
                context.downloads.set_status(
                    invoice_id, "error",
                    error_detail=f"Waluta {parsed.currency} - eksport XML obsluguje tylko PLN",
                )
                skipped += 1
                continue
            nip = normalize_nip(parsed.seller_nip)
            if nip not in contractors:
                contractor = find_by_nip(read_conn, nip)
                if contractor is not None:
                    contractors[nip] = contractor
            to_export.append((row, parsed))
        if not to_export:
            return _render_list(
                request, context, c,
                sync_result=f"Nic do eksportu - pominieto {skipped} (juz w Optimie lub blad)",
            )
        xml = export_as_xml(
            [p for _, p in to_export], company.purchase_register, company.offline_target_id,
            contractors=contractors, payment_form_ids=payment_form_guids(read_conn),
        )
        # Status zostaje "new" (import w Optimie jest reczny i moze sie nie udac);
        # exported_at oznacza tylko, ze faktura trafila do pliku - lista pokazuje "w pliku XML".
        for row, _ in to_export:
            context.downloads.set_status(
                row["id"], "new", exported_at=datetime.now(UTC).isoformat(), error_detail=None
            )
        context.audit.log(
            "purchases_export_xml", c, environment=env,
            details={"ids": [row["id"] for row, _ in to_export], "count": len(to_export),
                     "skipped": skipped},
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
        return RedirectResponse(_list_url(request, c), status_code=303)

    raise HTTPException(400, f"Nieznany tryb eksportu: {mode}")


@router.get("/purchases/{invoice_id}")
def purchase_detail(
    request: Request, invoice_id: int, c: str, context: AppContext = Depends(ctx)
):
    row = context.downloads.get(invoice_id)
    if row is None:
        raise HTTPException(404, "Nie znaleziono faktury")
    if row["company_id"] != c:
        # przelaczenie firmy na podgladzie faktury - wracamy do listy wybranej firmy
        return RedirectResponse(f"/purchases?c={c}", status_code=303)
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
