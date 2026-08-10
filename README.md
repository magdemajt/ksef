# ksef-bridge

Integrator **Comarch ERP Optima ↔ KSeF 2.0** (Krajowy System e-Faktur):

1. **Wysyłka**: czyta faktury sprzedaży z bazy MS SQL Optimy, pokazuje w lokalnym
   web GUI i po jawnym potwierdzeniu wysyła do KSeF (schemat FA(3), sesja interaktywna,
   UPO per faktura).
2. **Pobieranie**: pobiera faktury zakupu z KSeF (kontekst nabywcy) i eksportuje je do
   rejestru VAT zakupu Optimy — przez XML „pracy rozproszonej” (bezpieczny domyślny tryb)
   albo bezpośredni INSERT SQL (po świadomym odblokowaniu, patrz *Etap M5*).

Obsługuje wiele firm (baz firmowych), środowiska TEST/DEMO/PROD i uwierzytelnienie
tokenem KSeF lub certyfikatem KSeF.

## Wymagania

- Python 3.12+ (testowane na 3.14), Linux/Windows
- dostęp sieciowy do serwera MS SQL z bazami Optimy
- konto SQL **tylko do odczytu** (`ksef_reader`) i opcjonalnie konto do zapisu
  (`ksef_writer`, INSERT wyłącznie na `CDN.VatNag`, `CDN.VatTab`, `CDN.BnkZdarzenia`)
- token KSeF albo certyfikat KSeF (.p12) dla każdej firmy

## Wersja spakowana (Windows / Linux, bez instalacji Pythona)

Aplikację można zbudować jako samodzielny program (PyInstaller):

```bash
pip install -e .[dev]
cd packaging && pyinstaller ksef-bridge.spec --noconfirm
./dist/ksef-bridge/ksef-bridge --selftest   # weryfikacja bundla
```

Wynik: katalog `packaging/dist/ksef-bridge/` z programem `ksef-bridge`
(`ksef-bridge.exe` na Windows) — do skopiowania/rozpakowania u użytkownika
i uruchomienia podwójnym kliknięciem. Uwaga: build trzeba wykonać **na docelowym
systemie** (PyInstaller nie cross-kompiluje). Workflow GitHub Actions
(`.github/workflows/build.yml`) buduje obie wersje automatycznie — ręcznie
(workflow_dispatch) albo przy tagu `v*` z gotowym release (zip + tar.gz).

Pierwsze uruchomienie: program tworzy `config.toml` z przykładową firmą
(Linux: `~/.config/ksef-bridge/`, Windows: `%APPDATA%\ksef-bridge\`), startuje
serwer i otwiera przeglądarkę na stronie Ustawień — dane firmy, serwer SQL
i sekrety uzupełnia się w GUI. Sekrety lądują w keyringu systemowym
(Windows: Menedżer poświadczeń; Linux: GNOME Keyring/Secret Service — na
serwerze bez środowiska graficznego użyj zmiennych `KSEF_BRIDGE_SECRET_*`).

## Instalacja

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp config.toml.example config.toml   # uzupelnij dane firm i serwera SQL
```

Sekrety trzymane są w keyringu systemowym (nigdy w plikach):

```bash
ksef-bridge secrets set sql-read-password
ksef-bridge secrets set sql-write-password
ksef-bridge secrets set ksef-token --company firma_a     # token KSeF dla firmy
ksef-bridge secrets set p12-pass  --company firma_a      # haslo .p12 (gdy auth_method=ksef_cert)
```

Uruchomienie GUI:

```bash
ksef-bridge serve      # domyslnie http://127.0.0.1:8300
```

## Szybki start na środowisku TEST

Środowisko testowe KSeF jest samoobsługowe — nie wymaga rejestracji:

```bash
# tworzy losowy NIP testowy, uwierzytelnia sie certyfikatem self-signed,
# mintuje token KSeF i zapisuje go w keyringu
ksef-bridge testdata --company firma_a
# nastepnie wpisz wypisany NIP do [companies.firma_a] w config.toml
ksef-bridge auth-test --company firma_a
```

Uwaga: środowisko TEST ma okno serwisowe codziennie 16:00–18:00.

## Etapy wdrożenia produkcyjnego

### M2 — diagnostyka bazy Optimy (read-only, bez ryzyka)

```bash
ksef-bridge diagnose --company firma_a
```

Raport rozstrzyga elementy zależne od wersji Optimy:

- nazwy kolumn KSeF na `CDN.TraNag` (deduplikacja z wbudowanym modułem KSeF Optimy),
- kolumny walutowe (`TrN_Waluta`, `TrN_KursL/M`),
- kombinacje `(TrE_Stawka, TrE_Flaga)` z ostatnich 12 miesięcy — **porównaj z mapą
  stawek w `app/fa3/mapper.py`** (FLAGA_OPODATKOWANA=2, ZWOLNIONA=1, NIE_PODLEGA=0);
  jeśli w bazie występują inne flagi, uzupełnij mapę przed pierwszą wysyłką,
- identity/triggery na tabelach zapisu,
- schemat numeracji `VaN_Lp` (miesięczny/roczny) — musi zgadzać się z `lp_numbering`
  w config.toml.

Po diagnozie porównaj 5 przykładowych faktur (sekcja `sample_invoices`) z oknem
faktury w Optimie, a wygenerowany XML (przycisk „podgląd” w GUI) z wartościami
dokumentu — zwłaszcza `TrE_Cena` (netto vs brutto) i sumy per stawka.

### M3/M4 — wysyłka i pobieranie

Cała praca odbywa się w GUI: `Sprzedaż` → zaznacz → „Przygotuj wysyłkę” →
przegląd walidacji → jawne potwierdzenie → statusy na żywo → UPO.
`Zakupy` → „Synchronizuj z KSeF” → zaznacz → eksport.

Limity API KSeF egzekwowane lokalnie (licznik przeżywa restart):
zapytania o metadane/eksporty **20/h na firmę** (stąd synchronizacja co ≥5 min),
wysyłka 180/h.

### M5 — bezpośredni zapis do rejestru VAT (opcjonalny)

Domyślnie zapis jest **wyłączony** (`writes_enabled=false`) i eksport generuje XML
pracy rozproszonej (Optima: *Narzędzia → Praca rozproszona → Import*).

Aby odblokować INSERT-y SQL:

1. **Wykonaj pełny backup bazy firmowej** (`BACKUP DATABASE` lub kopia z Optimy)
   i przywróć go jako bazę testową (np. `CDN_Firma_TEST`); wskaż ją w config.toml.
2. `ksef-bridge snapshot --label before --company firma_a`
3. W Optimie (na bazie testowej!) wystaw ręcznie **jedną fakturę zakupu** w rejestrze
   VAT z płatnością.
4. `ksef-bridge snapshot --label after --company firma_a`
5. `ksef-bridge diff --company firma_a` — zrzuca nowe wiersze kolumna po kolumnie;
   na tej podstawie utwórz `insert_spec-firma_a.json` (ścieżka wypisana przez diff)
   z kolumnami-literałami, których program nie wylicza z faktury
   (`VaT_RodzajZakupu`, `VaT_Odliczenia`, `VaT_KolumnaKPR`, kolumny operatora itd.).
6. Ustaw `writes_enabled = true` i wykonaj eksport **jednej** faktury na bazie
   testowej. Otwórz firmę testową w Optimie i sprawdź: wpis w rejestrze VAT zakupu,
   płatność w preliminarzu Kasa/Bank, obecność w szkicu JPK_V7 właściwego miesiąca,
   test spójności danych Optimy.
7. Dopiero po pozytywnej weryfikacji przełącz na bazę produkcyjną (świeży backup,
   pierwsza faktura, ponowna weryfikacja).

Program dodatkowo sam się chroni: bez pliku `insert_spec` zapis jest odmawiany,
po INSERT-cie wykonuje re-SELECT i porównuje sumy co do grosza, a przed eksportem
sprawdza `CDN.DokumentyKSeF` i rejestr VAT, by nie dublować faktur pobranych
natywnym modułem KSeF Optimy.

### Checklist przejścia na PROD

- [ ] pełny przebieg wysyłki + pobierania na środowisku DEMO
- [ ] `environment = "prod"` w config.toml
- [ ] token/certyfikat KSeF wygenerowany na produkcji i zapisany w keyringu
- [ ] `allow_prod_send = true` (świadoma decyzja)
- [ ] każda partia na PROD wymaga dodatkowo przepisania NIP firmy w formularzu

## Bezpieczeństwo

- GUI słucha tylko na 127.0.0.1; wszystkie POST-y chronione tokenem CSRF.
- Nic nie wysyła się bez podglądu i jawnego potwierdzenia (token jednorazowy, TTL 10 min).
- Faktury spoza zakresu (korekty, zaliczki, sprzedaż bez PL NIP, nieznane stawki)
  są blokowane z podaniem powodu — nigdy nie są „naprawiane” po cichu.
- Duplikaty: lokalny stan + kolumny KSeF Optimy + odpowiedź 440 z KSeF.
- Pełny log audytowy (partie, odpowiedzi KSeF, INSERT-y) w lokalnej bazie SQLite.

## Zakres v1

Krajowe faktury VAT sprzedaży w PLN i walutach obcych (kurs z dokumentu, VAT w PLN
w polach P_14_xW), nabywca z polskim NIP. Poza zakresem: korekty, zaliczki,
sprzedaż konsumencka/zagraniczna, załączniki, sesje wsadowe, tryby offline —
dokumenty te są widoczne na liście jako zablokowane z powodem.

## Struktura

```
app/ksef/      gateway (adapter nad biblioteka ksef2), sender, downloader, ratelimit
app/fa3/       mapper/builder/validator/parser FA(3) + przypiete XSD
app/optima/    reader (TraNag/TraElem), writer (VatNag/VatTab/BnkZdarzenia),
               introspect (diagnose + snapshot/diff), contractors
app/state/     lokalny stan SQLite (wysylki, pobrania, kursory, audyt, limity)
app/web/       FastAPI + Jinja2 + htmx (lokalny GUI)
```

Testy: `.venv/bin/python -m pytest tests/`
