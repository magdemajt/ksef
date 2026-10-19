"""Diagnostyka zywej bazy Optimy + silnik snapshot/diff dla specyfikacji INSERT-ow.

Diagnose (read-only) rozstrzyga wszystkie [U] z planu:
    - kolumny KSeF (TraNag, DokumentyKSeF),
    - identity/triggery na tabelach zapisu,
    - inwentarz kolumn VatNag/VatTab/BnkZdarzenia,
    - schemat numeracji VaN_Lp,
    - kombinacje (TrE_Stawka, TrE_Flaga) i kolumny walutowe.

Snapshot/diff: przed/po recznym wystawieniu faktury zakupu w UI Optimy -> zrzut
nowych wierszy = autorytatywna specyfikacja INSERT-ow (insert_spec.json).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

WRITE_TABLES = ("VatNag", "VatTab", "BnkZdarzenia")
INTEREST_TABLES = (
    "TraNag", "TraElem", "VatNag", "VatTab", "BnkZdarzenia", "DokumentyKSeF", "Kontrahenci",
    "Firma", "Pieczatki",
)


def _rows(cur) -> list[tuple]:
    return [tuple(r) for r in cur.fetchall()]


def run_diagnose(conn) -> dict:
    cur = conn.cursor()
    report: dict = {"generated_at": datetime.now(UTC).isoformat()}

    cur.execute("SELECT @@VERSION, DB_NAME()")
    ver, dbname = cur.fetchone()
    report["server_version"] = str(ver).splitlines()[0]
    report["database"] = str(dbname)

    # kolumny KSeF w calej bazie
    cur.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA='CDN' AND (COLUMN_NAME LIKE '%KSeF%' OR COLUMN_NAME LIKE '%KSEF%') "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION"
    )
    report["ksef_columns"] = [
        {"table": t, "column": c, "type": d} for t, c, d in _rows(cur)
    ]

    # pelny inwentarz kolumn tabel zapisu i odczytu
    inventory: dict[str, list[dict]] = {}
    for table in INTEREST_TABLES:
        cur.execute(
            "SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT "
            "FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='CDN' AND TABLE_NAME=? ORDER BY ORDINAL_POSITION",
            (table,),
        )
        inventory[table] = [
            {"name": n, "type": t, "nullable": nl == "YES", "default": df}
            for n, t, nl, df in _rows(cur)
        ]
    report["columns"] = inventory

    # identity + triggery
    cur.execute(
        "SELECT OBJECT_NAME(object_id), name FROM sys.identity_columns "
        "WHERE OBJECT_SCHEMA_NAME(object_id)='CDN'"
    )
    report["identity_columns"] = [
        {"table": t, "column": c} for t, c in _rows(cur)
    ]
    cur.execute(
        "SELECT OBJECT_NAME(parent_id), name, is_disabled FROM sys.triggers "
        "WHERE OBJECT_SCHEMA_NAME(parent_id)='CDN'"
    )
    report["triggers"] = [
        {"table": t, "name": n, "disabled": bool(d)} for t, n, d in _rows(cur)
    ]

    # inferencja numeracji VaN_Lp: czy Lp startuje od 1 w kazdym miesiacu?
    try:
        cur.execute(
            "SELECT VaN_Rejestr, VaN_RokMies, MIN(VaN_Lp), MAX(VaN_Lp), COUNT(*) "
            "FROM CDN.VatNag WHERE VaN_Typ=1 "
            "GROUP BY VaN_Rejestr, VaN_RokMies ORDER BY VaN_Rejestr, VaN_RokMies"
        )
        lp_data = [
            {"rejestr": r, "rokmies": rm, "min_lp": mn, "max_lp": mx, "count": c}
            for r, rm, mn, mx, c in _rows(cur)
        ]
        report["van_lp_by_period"] = lp_data
        months_starting_at_1 = sum(1 for d in lp_data if d["min_lp"] == 1)
        report["lp_numbering_guess"] = (
            "month" if lp_data and months_starting_at_1 >= max(1, len(lp_data) - 1) else "year"
        )
    except Exception as exc:
        report["van_lp_by_period"] = f"blad: {exc}"

    # kombinacje stawka/flaga z 12 miesiecy - zasilaja mape stawek mappera
    try:
        cur.execute(
            "SELECT DISTINCT TrE_Stawka, TrE_Flaga FROM CDN.TraElem "
            "WHERE TrE_DataDok >= DATEADD(month, -12, GETDATE()) "
            "ORDER BY TrE_Stawka, TrE_Flaga"
        )
        report["rate_flag_combinations"] = [
            {"stawka": float(s), "flaga": f} for s, f in _rows(cur)
        ]
    except Exception as exc:
        report["rate_flag_combinations"] = f"blad: {exc}"

    # kolumny walutowe na TraNag
    tranag_cols = {c["name"] for c in inventory.get("TraNag", [])}
    report["currency_columns"] = {
        "waluta": "TrN_Waluta" in tranag_cols,
        "kurs_l": "TrN_KursL" in tranag_cols,
        "kurs_m": "TrN_KursM" in tranag_cols,
        "candidates": sorted(
            c for c in tranag_cols if "walut" in c.lower() or "kurs" in c.lower()
        ),
    }

    # kandydaci na znacznik mechanizmu podzielonej platnosci (Adnotacje/P_18A)
    report["mpp_columns"] = sorted(
        c for c in tranag_cols
        if any(h in c.lower() for h in ("mpp", "podziel", "split"))
    )

    # slownik statusow KSeF Optimy z istniejacych dokumentow - jakie wartosci ustawia
    # modul KSeF (wysylka z Optimy / reczne wpisanie numeru); baza pod zapis numeru KSeF
    try:
        cur.execute(
            "SELECT TrN_TypDokumentu, TrN_KSeFStatusId, CAST(TrN_KSeFStatus AS NVARCHAR(200)), "
            "TrN_KSeFTypSrodowiska, COUNT(*), "
            "SUM(CASE WHEN ISNULL(TrN_NrKSeF, '') <> '' THEN 1 ELSE 0 END), MAX(TrN_TrNID) "
            "FROM CDN.TraNag GROUP BY TrN_TypDokumentu, TrN_KSeFStatusId, "
            "CAST(TrN_KSeFStatus AS NVARCHAR(200)), TrN_KSeFTypSrodowiska "
            "ORDER BY TrN_TypDokumentu, TrN_KSeFStatusId"
        )
        report["ksef_status_values"] = [
            {
                "typ_dokumentu": typ, "status_id": sid, "status": st, "srodowisko": env,
                "dokumentow": cnt, "z_numerem_ksef": with_nr, "przyklad_trn_id": example,
            }
            for typ, sid, st, env, cnt, with_nr, example in _rows(cur)
        ]
    except Exception as exc:
        report["ksef_status_values"] = f"blad: {exc}"

    # czy Optima uzupelnia numer KSeF w rejestrze VAT (JPK_V7 bierze go z VatNag)
    try:
        cur.execute(
            "SELECT VaN_Typ, COUNT(*), "
            "SUM(CASE WHEN ISNULL(VaN_NrKSeF, '') <> '' THEN 1 ELSE 0 END) "
            "FROM CDN.VatNag GROUP BY VaN_Typ ORDER BY VaN_Typ"
        )
        report["vat_register_ksef"] = [
            {"van_typ": typ, "wpisow": cnt, "z_numerem_ksef": with_nr}
            for typ, cnt, with_nr in _rows(cur)
        ]
    except Exception as exc:
        report["vat_register_ksef"] = f"blad: {exc}"

    # tresc triggerow na tabelach, ktore zmieni zapis numeru KSeF (NULL = brak
    # uprawnienia VIEW DEFINITION albo trigger zaszyfrowany)
    try:
        cur.execute(
            "SELECT OBJECT_NAME(parent_id), name, OBJECT_DEFINITION(object_id) "
            "FROM sys.triggers WHERE parent_id IN "
            "(OBJECT_ID('CDN.TraNag'), OBJECT_ID('CDN.VatNag'), OBJECT_ID('CDN.DokumentyKSeF'))"
        )
        report["trigger_definitions"] = [
            {"table": t, "name": n, "definition": d} for t, n, d in _rows(cur)
        ]
    except Exception as exc:
        report["trigger_definitions"] = f"blad: {exc}"

    # dane wlasne firmy (sprzedawcy) - zrodlo adresu Podmiot1
    try:
        report["company_address_candidates"] = find_company_address(conn)
    except Exception as exc:
        report["company_address_candidates"] = f"blad: {exc}"

    # probka 5 najnowszych faktur (tylko naglowki, do porownania z UI)
    try:
        cur.execute(
            "SELECT TOP 5 TrN_TrNID, TrN_NumerPelny, TrN_DataDok, TrN_RazemNetto, "
            "TrN_RazemVAT, TrN_RazemBrutto FROM CDN.TraNag "
            "WHERE TrN_TypDokumentu=302 AND TrN_Bufor=0 ORDER BY TrN_TrNID DESC"
        )
        report["sample_invoices"] = [
            {
                "trn_id": i, "numer": n, "data": str(d),
                "netto": float(nt), "vat": float(v), "brutto": float(b),
            }
            for i, n, d, nt, v, b in _rows(cur)
        ]
    except Exception as exc:
        report["sample_invoices"] = f"blad: {exc}"

    return report


# -- dane firmy (sprzedawcy) z Optimy --

# Pole adresu -> sufiksy nazw kolumn wg konwencji Optimy (<Prefiks>_<Pole>), w kolejnosci
# preferencji. Dokladne dopasowanie sufiksu, zeby nie lapac np. Knt_KorUlica / Knt_OsUlica.
_ADDRESS_FIELDS = {
    "nazwa": ("nazwa1", "nazwa", "nazwapelna"),
    "nazwa2": ("nazwa2",),
    "nip": ("nipe", "nip"),
    "ulica": ("ulica",),
    "nr_domu": ("nrdomu",),
    "nr_lokalu": ("nrlokalu",),
    "miasto": ("miasto", "miejscowosc"),
    "kod_pocztowy": ("kodpocztowy",),
}
COMPANY_TABLES = ("Pieczatki", "Firma")


def _match_address_columns(columns: list[str]) -> dict[str, str]:
    by_suffix = {c.lower().split("_", 1)[-1]: c for c in columns}
    found = {}
    for field, suffixes in _ADDRESS_FIELDS.items():
        for suffix in suffixes:
            if suffix in by_suffix:
                found[field] = by_suffix[suffix]
                break
    return found


def find_company_address(conn) -> list[dict]:
    """Kandydaci na adres firmy z tabel z kolumnami adresowymi (pieczatka firmy).

    Tylko podpowiedz dla formularza firmy - uzytkownik zatwierdza adres recznie,
    bo nazwy kolumn i znaczenie wierszy nie sa udokumentowane.
    """
    cur = conn.cursor()
    candidates = []
    for table in COMPANY_TABLES:
        cur.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA='CDN' AND TABLE_NAME=? ORDER BY ORDINAL_POSITION",
            (table,),
        )
        found = _match_address_columns([r[0] for r in cur.fetchall()])
        if "miasto" not in found:
            continue
        cur.execute(f"SELECT TOP 10 * FROM CDN.[{table}]")  # noqa: S608
        for row in _dicts(cur):
            def get(field: str) -> str:
                return (row.get(found[field]) or "").strip() if field in found else ""

            street = " ".join(p for p in (get("ulica"), get("nr_domu")) if p)
            if get("nr_lokalu"):
                street += f" lok. {get('nr_lokalu')}"
            line2 = " ".join(p for p in (get("kod_pocztowy"), get("miasto")) if p)
            if not (street or line2):
                continue
            candidates.append({
                "source": f"CDN.{table}",
                "nazwa": " ".join(p for p in (get("nazwa"), get("nazwa2")) if p),
                "nip": get("nip"),
                "address_line1": street or line2,
                "address_line2": line2 if street else "",
            })
    return candidates


# -- sonda pojedynczej faktury (ustalenie, gdzie Optima zapisuje numer KSeF) --


def _plain(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes | bytearray):
        return "0x" + bytes(value).hex()
    return str(value)


# Tabele powiazane z faktura, w ktorych Optima trzyma kopie numeru KSeF:
# tabela -> (kolumna FK na TraNag, klucz glowny tabeli)
LINKED_TABLES = {
    "VatNag": ("TrN_VaNId", "VaN_VaNID"),
    "DekretyNag": ("TrN_DekId", "DeN_DeNId"),
    "ZapisyKPR": ("TrN_KPRID", "KPR_KPRID"),
    "Ryczalt": ("TrN_RycID", "RYC_RycID"),
}


def _dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [{c: _plain(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


def probe_invoice(conn, numer: str) -> dict:
    """Pelny wiersz TraNag faktury sprzedazy + wiersze powiazane (rejestr VAT, dekret,
    KPiR/ryczalt, DokumentyKSeF).

    Porownanie dwoch sond (przed/po recznym wpisaniu numeru KSeF w Optimie) pokazuje,
    ktore kolumny Optima ustawia - snapshot/diff widzi tylko nowe wiersze, nie UPDATE.
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM CDN.TraNag WHERE TrN_NumerPelny = ? AND TrN_TypDokumentu = 302",
        (numer,),
    )
    rows = _dicts(cur)
    result: dict = {"numer": numer, "tranag": None, "linked": {}, "dokumenty_ksef": []}
    if not rows:
        return result
    tranag = result["tranag"] = rows[0]
    for table, (fk, pk) in LINKED_TABLES.items():
        if not tranag.get(fk):
            continue
        try:
            cur.execute(f"SELECT * FROM CDN.{table} WHERE {pk} = ?", (int(tranag[fk]),))
            result["linked"][table] = _dicts(cur)
        except Exception as exc:
            result["linked"][table] = [{"_blad": str(exc)}]
    try:
        # wiersze przypiete do faktury + najnowsze (gdyby Optima nie ustawiala DKF_TrNId)
        cur.execute(
            "SELECT * FROM CDN.DokumentyKSeF WHERE DKF_TrNId = ? OR DKF_DKFID IN "
            "(SELECT TOP 5 DKF_DKFID FROM CDN.DokumentyKSeF ORDER BY DKF_DKFID DESC) "
            "ORDER BY DKF_DKFID DESC",
            (int(tranag["TrN_TrNID"]),),
        )
        result["dokumenty_ksef"] = _dicts(cur)
    except Exception as exc:
        result["dokumenty_ksef_error"] = str(exc)
    return result


def _row_changes(table: str, old_row: dict, new_row: dict) -> list[dict]:
    return [
        {"table": table, "column": c, "before": old_row.get(c), "after": new_row.get(c)}
        for c in new_row
        if old_row.get(c) != new_row.get(c)
    ]


def diff_probes(before: dict, after: dict) -> dict:
    """Zmienione kolumny TraNag i tabel powiazanych + nowe wiersze DokumentyKSeF."""
    changed = _row_changes("TraNag", before.get("tranag") or {}, after.get("tranag") or {})
    for table, rows in (after.get("linked") or {}).items():
        old_rows = (before.get("linked") or {}).get(table) or [{}]
        changed += _row_changes(table, old_rows[0], rows[0] if rows else {})
    old_ksef = {json.dumps(r, sort_keys=True) for r in before.get("dokumenty_ksef", [])}
    new_ksef = [
        r for r in after.get("dokumenty_ksef", [])
        if json.dumps(r, sort_keys=True) not in old_ksef
    ]
    return {"changed": changed, "dokumenty_ksef_new": new_ksef}


# -- snapshot / diff --


def _table_watermarks(conn) -> dict[str, dict]:
    """Dla kazdej tabeli CDN.*: liczba wierszy + max identity (jesli jest)."""
    cur = conn.cursor()
    cur.execute(
        "SELECT t.TABLE_NAME, ic.name FROM INFORMATION_SCHEMA.TABLES t "
        "LEFT JOIN sys.identity_columns ic "
        "  ON ic.object_id = OBJECT_ID('CDN.' + t.TABLE_NAME) "
        "WHERE t.TABLE_SCHEMA='CDN' AND t.TABLE_TYPE='BASE TABLE'"
    )
    tables = _rows(cur)
    marks: dict[str, dict] = {}
    for table, id_col in tables:
        try:
            if id_col:
                cur.execute(
                    f"SELECT COUNT(*), MAX([{id_col}]) FROM CDN.[{table}]"
                )
                count, max_id = cur.fetchone()
                marks[table] = {"count": count, "id_col": id_col, "max_id": max_id}
            else:
                cur.execute(f"SELECT COUNT(*) FROM CDN.[{table}]")
                marks[table] = {"count": cur.fetchone()[0], "id_col": None, "max_id": None}
        except Exception:
            continue
    return marks


def snapshot(conn, out_path: Path, label: str) -> Path:
    data = {
        "label": label,
        "taken_at": datetime.now(UTC).isoformat(),
        "tables": _table_watermarks(conn),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return out_path


def diff(conn, before_path: Path, after_path: Path) -> dict:
    """Zrzuca nowe wiersze z tabel, ktore urosly miedzy snapshotami."""
    before = json.loads(before_path.read_text(encoding="utf-8"))["tables"]
    after = json.loads(after_path.read_text(encoding="utf-8"))["tables"]
    cur = conn.cursor()
    changes: dict[str, list[dict]] = {}
    for table, aft in after.items():
        bef = before.get(table)
        if bef is None or aft["count"] == bef["count"]:
            continue
        id_col, old_max = aft.get("id_col"), (bef.get("max_id") or 0)
        if not id_col:
            changes[table] = [{"_info": f"wierszy przybylo: {aft['count'] - bef['count']}, brak identity - zrzut pominiety"}]
            continue
        cur.execute(
            f"SELECT * FROM CDN.[{table}] WHERE [{id_col}] > ? ORDER BY [{id_col}]",
            (old_max,),
        )
        cols = [d[0] for d in cur.description]
        changes[table] = [
            {c: (str(v) if v is not None else None) for c, v in zip(cols, row)}
            for row in cur.fetchall()
        ]
    return changes
