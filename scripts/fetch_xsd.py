"""Pobiera i przypina w repo oficjalne schematy XSD FA(3) z CIRFMF/ksef-api.

Uruchamiane jednorazowo (i po kazdej zmianie schematu przez MF):
    python scripts/fetch_xsd.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

import httpx

RAW_BASE = "https://raw.githubusercontent.com/CIRFMF/ksef-api/main"
TARGET = Path(__file__).resolve().parent.parent / "app" / "fa3" / "schemas"

FILES = {
    "faktury/schemy/FA/schemat_FA(3)_v1-0E.xsd": "schemat_FA(3)_v1-0E.xsd",
    "faktury/schemy/FA/bazowe/ElementarneTypyDanych_v10-0E.xsd": "ElementarneTypyDanych_v10-0E.xsd",
    "faktury/schemy/FA/bazowe/StrukturyDanych_v10-0E.xsd": "StrukturyDanych_v10-0E.xsd",
    "faktury/schemy/FA/bazowe/KodyKrajow_v10-0E.xsd": "KodyKrajow_v10-0E.xsd",
}


def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30, follow_redirects=True) as client:
        for remote, local in FILES.items():
            url = f"{RAW_BASE}/{quote(remote)}"
            print(f"GET {url}")
            resp = client.get(url)
            resp.raise_for_status()
            (TARGET / local).write_bytes(resp.content)
            print(f"  -> {TARGET / local} ({len(resp.content)} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
