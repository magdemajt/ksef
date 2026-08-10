"""Wykrywanie baz Optimy na serwerze MS SQL.

Strategia (wszystko defensywnie - brak uprawnien do bazy nie przerywa skanu):
    1. sys.databases na master -> lista baz uzytkownika,
    2. klasyfikacja kazdej bazy po zawartosci:
       - firmowa: istnieje tabela CDN.TraNag,
       - konfiguracyjna: nazwa CDN_KNF_* lub tabela CDN.Bazy,
       - no_access / other,
    3. z bazy konfiguracyjnej proba odczytu rejestru firm (CDN.Bazy) i heurystyczne
       dopasowanie kolumn (schemat nieudokumentowany publicznie): kolumna, ktorej
       wartosci pokrywaja sie z nazwami wykrytych baz = nazwa bazy; inna kolumna
       tekstowa = nazwa wyswietlana firmy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

SYSTEM_DATABASES = {"master", "tempdb", "model", "msdb"}


@dataclass
class DiscoveredDatabase:
    name: str
    kind: str  # company | config | no_access | other
    company_name: str | None = None  # nazwa firmy z rejestru bazy konfiguracyjnej
    detail: str | None = None


@dataclass
class DiscoveryResult:
    databases: list[DiscoveredDatabase] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def company_databases(self) -> list[DiscoveredDatabase]:
        return [d for d in self.databases if d.kind == "company"]


def _has_table(cursor, database: str, table: str) -> bool:
    cursor.execute(
        f"SELECT COUNT(*) FROM [{database}].INFORMATION_SCHEMA.TABLES "  # noqa: S608
        "WHERE TABLE_SCHEMA = 'CDN' AND TABLE_NAME = ?",
        (table,),
    )
    return cursor.fetchone()[0] > 0


def discover_databases(master_conn) -> DiscoveryResult:
    """Skanuje serwer przez polaczenie do bazy master (login read-only wystarczy)."""
    result = DiscoveryResult()
    cur = master_conn.cursor()
    try:
        cur.execute(
            "SELECT name FROM sys.databases "
            "WHERE state_desc = 'ONLINE' AND database_id > 4 ORDER BY name"
        )
        names = [r[0] for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"Nie mozna odczytac listy baz (sys.databases): {exc}")
        return result

    for name in names:
        if name in SYSTEM_DATABASES:
            continue
        try:
            if _has_table(cur, name, "TraNag"):
                result.databases.append(DiscoveredDatabase(name, "company"))
            elif name.upper().startswith("CDN_KNF") or _has_table(cur, name, "Bazy"):
                result.databases.append(DiscoveredDatabase(name, "config"))
            else:
                result.databases.append(DiscoveredDatabase(name, "other"))
        except Exception as exc:  # noqa: BLE001
            result.databases.append(
                DiscoveredDatabase(name, "no_access", detail=str(exc)[:200])
            )

    _enrich_from_config_db(cur, result)
    return result


def _enrich_from_config_db(cur, result: DiscoveryResult) -> None:
    """Dopisuje nazwy firm z rejestru bazy konfiguracyjnej (CDN.Bazy), jesli czytelny."""
    config_dbs = [d for d in result.databases if d.kind == "config"]
    company_names = {d.name for d in result.company_databases}
    for config_db in config_dbs:
        try:
            cur.execute(f"SELECT * FROM [{config_db.name}].CDN.Bazy")  # noqa: S608
            columns = [d[0] for d in cur.description]
            rows = [tuple(r) for r in cur.fetchall()]
        except Exception as exc:  # noqa: BLE001
            result.errors.append(
                f"Rejestr firm z {config_db.name} nieczytelny: {str(exc)[:200]}"
            )
            continue
        mapping = match_registry_to_dbs(columns, rows, company_names)
        for db_name, display_name in mapping.items():
            for d in result.company_databases:
                if d.name == db_name and not d.company_name:
                    d.company_name = display_name
        if mapping:
            return  # pierwszy czytelny rejestr wystarczy


def match_registry_to_dbs(
    columns: list[str], rows: list[tuple], company_db_names: set[str]
) -> dict[str, str]:
    """Heurystyka: znajdz kolumne z nazwami baz i kolumne z nazwa wyswietlana.

    Zwraca {nazwa_bazy: nazwa_firmy}. Pusty slownik gdy nie da sie dopasowac.
    """
    if not rows or not company_db_names:
        return {}

    def col_values(idx: int) -> list[str]:
        return [str(r[idx]).strip() for r in rows if r[idx] is not None]

    # kolumna bazy: najwiecej wartosci pokrywajacych sie z faktycznymi nazwami baz
    best_db_col, best_hits = None, 0
    lowered_dbs = {n.lower() for n in company_db_names}
    for i in range(len(columns)):
        hits = sum(1 for v in col_values(i) if v.lower() in lowered_dbs)
        if hits > best_hits:
            best_db_col, best_hits = i, hits
    if best_db_col is None:
        return {}

    # kolumna nazwy: tekstowa, rozna od kolumny bazy, z najdluzszymi wartosciami
    best_name_col, best_score = None, -1.0
    for i in range(len(columns)):
        if i == best_db_col:
            continue
        values = col_values(i)
        if not values:
            continue
        text_values = [v for v in values if v and not v.replace(".", "").isdigit()]
        if len(text_values) < len(values) / 2:
            continue
        score = sum(len(v) for v in text_values) / len(values)
        if score > best_score:
            best_name_col, best_score = i, score

    mapping: dict[str, str] = {}
    for row in rows:
        db_val = row[best_db_col]
        if db_val is None:
            continue
        db_name = str(db_val).strip()
        matched = next(
            (n for n in company_db_names if n.lower() == db_name.lower()), None
        )
        if matched is None:
            continue
        display = None
        if best_name_col is not None and row[best_name_col] is not None:
            display = str(row[best_name_col]).strip() or None
        mapping[matched] = display or matched
    return mapping
