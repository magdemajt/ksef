# -*- mode: python ; coding: utf-8 -*-
# Spec PyInstaller dla ksef-bridge (tryb onedir - szybszy start, mniej problemow
# z antywirusami niz onefile; katalog dist/ksef-bridge pakujemy do zip).
#
# Budowac NA docelowym systemie:  pyinstaller packaging/ksef-bridge.spec

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = []
binaries = []
hiddenimports = []

# ksef2 - modele pydantic, zasoby FA(3); zbieramy wszystko
for pkg in ("ksef2", "mssql_python", "signxml"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# mssql-python laduje ddbc_bindings.cp{ver}-{arch}.pyd wlasnym loaderem po nazwie
# pliku - collect_all/collect_dynamic_libs nie zbiera .pyd, wiec dokladamy recznie
# wszystkie natywne pliki pakietu z zachowaniem ukladu katalogow.
import pathlib
import mssql_python as _mssql

_pkg_dir = pathlib.Path(_mssql.__file__).parent
for _f in _pkg_dir.rglob("*"):
    if _f.suffix.lower() in (".pyd", ".dll", ".so", ".dylib") or ".so." in _f.name:
        _dest = pathlib.Path("mssql_python") / _f.parent.relative_to(_pkg_dir)
        datas.append((str(_f), str(_dest)))

# nasze zasoby: szablony, statyki, XSD, schema.sql
datas += [
    ("../app/web/templates", "app/web/templates"),
    ("../app/web/static", "app/web/static"),
    ("../app/fa3/schemas", "app/fa3/schemas"),
    ("../app/state/schema.sql", "app/state"),
]

# backendy keyring laduja sie dynamicznie przez entry-pointy
hiddenimports += collect_submodules("keyring.backends")
hiddenimports += [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

a = Analysis(
    ["launcher.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "PyInstaller"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ksef-bridge",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # okno konsoli = widoczne logi; GUI i tak dziala w przegladarce
    icon=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ksef-bridge",
)
