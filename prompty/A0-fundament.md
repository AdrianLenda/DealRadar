# A0 — Fundament: modele, baza, CLI, testy

**Uruchom jako pierwszy, samodzielnie. Reszta agentów stoi na tym, co tu powstanie.**

---

## Kontekst

Budujemy DealRadar — narzędzie wykrywające prawdziwe promocje w sklepach
internetowych i oceniające stosunek jakość/cena. Przeczytaj `ARCHITEKTURA.md`
w katalogu nadrzędnym, w całości, zanim napiszesz linijkę kodu. Szczególnie
sekcje 3 (zasady Z1–Z7) i 5 (model danych) — twoje zadanie to dosłowna
implementacja sekcji 5 plus szkielet, w który reszta zespołu się wepnie.

Rdzeń systemu jest **w pełni deterministyczny — żadnych wywołań LLM-a nigdzie
w kodzie.**

## Twoje zadanie

Stwórz szkielet projektu, który da się uruchomić, przetestować i rozbudować,
oraz **kontrakty danych**, których nie będzie już trzeba zmieniać.

### 1. Struktura repozytorium

```
dealradar/
  pyproject.toml
  src/dealradar/
    __init__.py
    models.py          # kanoniczne modele pydantic v2
    db.py              # sesje, silnik, helpery zapisu
    config.py          # ładowanie YAML + zmienne środowiskowe
    cli.py             # typer, komendy-zaślepki dla wszystkich etapów
    errors.py
    migrations/        # numerowane pliki .sql, prosty runner
    collectors/__init__.py     # tylko protokół + klasa bazowa
    normalize/__init__.py      # pusty, właściciel: A2
    matching/__init__.py       # pusty, właściciel: A3
    scoring/__init__.py        # pusty, właściciele: A4, A5
    reporting/__init__.py      # pusty, właściciel: A6
  config/
    sources.yaml
    brands.yaml        # aliasy marek
  tests/
    conftest.py
    test_models.py
    test_db.py
    fixtures/
  README.md
```

### 2. `models.py` — kontrakty (to jest najważniejsza część zadania)

Pydantic v2. Zaimplementuj dokładnie tabele z §5 `ARCHITEKTURA.md` jako modele:
`RawOffer`, `Offer`, `PricePoint`, `Product`, `ProductAttr`, `Category`,
`Deal`, `QualityScore`, `ShopCredibility`, `MatchReview`, `Watch`, `Source`.

Wymagania twarde:

- **Wszystkie kwoty to `int` w groszach.** Zdefiniuj typ `Grosze = Annotated[int, Field(ge=0)]`.
  Waliduj, że nikt nie przekazał `float` — rzucaj błąd, nie konwertuj po cichu.
- `Offer.price_total` jest **wyliczane**, nie przyjmowane: `price_gross + (shipping_cost or 0)`.
  Gdy `shipping_cost is None` → `price_incomplete = True`.
- `Condition = Literal["new", "refurb", "used", "damaged"]`.
- `MatchMethod = Literal["ean", "brand_mpn", "fuzzy", "manual", "none"]`.
- `DealBasis = Literal["history", "cross_shop"]`.
- Pola nieznane to `None`, **nigdy** wartość domyślna udająca dane
  (`rating_avg` bez ocen to `None`, nie `0.0`).
- `Offer.ean` przechodzi walidator: 8 lub 13 cyfr **z poprawną sumą kontrolną
  GTIN**; błędny EAN → `None` + zapis powodu w `Offer.data_issues: list[str]`.
  Zaimplementuj `validate_gtin(value: str) -> bool` w `models.py` i przetestuj
  na znanych poprawnych i niepoprawnych kodach.
- Każdy model wyliczeniowy (`Deal`, `QualityScore`) ma pole
  `evidence: dict[str, Any]` — zasada Z6.

### 3. `db.py` + `migrations/`

- SQLite z `PRAGMA journal_mode=WAL`, `foreign_keys=ON`.
- Warstwa dostępu ma być na tyle cienka, żeby przejście na Postgres
  polegało na zmianie connection stringa — żadnego SQL-a specyficznego dla SQLite
  poza migracjami.
- Prosty runner migracji: pliki `001_init.sql`, `002_....sql`, tabela
  `schema_version`, komenda `dealradar db migrate`. Bez Alembica.
- **Indeksy, które muszą powstać od razu** (bez nich system staje przy ~100k ofert):
  - `price_point (offer_id, observed_at)`
  - `offer (product_id)`, `offer (source_id, external_id)` UNIQUE
  - `offer (ean)` WHERE ean IS NOT NULL
  - `offer (brand_norm, category_id)` — klucz blokujący matchera
  - `raw_offer (source_id, content_hash)`
- Funkcja `upsert_offer(session, offer)` — idempotentna po
  `(source_id, external_id)`, aktualizuje `last_seen`, nie nadpisuje `first_seen`.
- Funkcja `append_price_point(session, offer_id, price_total, in_stock, observed_at)`
  — **append-only, maksymalnie jeden wpis na ofertę na dzień** (przy powtórce
  tego samego dnia aktualizuje istniejący wpis, nie dokłada drugiego).

### 4. `config.py`

- Ładowanie `config/*.yaml` do modeli pydantic (walidacja przy starcie, nie przy użyciu).
- Sekrety (klucze API) **wyłącznie ze zmiennych środowiskowych**, nigdy z YAML-a.
  Plik `.env.example` z listą wymaganych zmiennych, `.env` w `.gitignore`.
- `sources.yaml` opisuje źródła zgodnie z tabelą `source` z §5, z polem
  `risk_prior: float` (0.0–1.0).

### 5. `cli.py` (typer)

Komendy — na razie każda woła zaślepkę logującą „nie zaimplementowano":

```
dealradar db migrate
dealradar collect  [--source NAME] [--since DATE]
dealradar normalize [--since DATE]
dealradar match     [--full] [--eval]
dealradar snapshot
dealradar score     [--deals] [--quality]
dealradar report    [--format html|text] [--out PATH]
dealradar run                       # pełny nocny przebieg
dealradar rebuild --from raw [--since DATE]
```

Każda komenda kończy się wypisaniem **raportu przebiegu** (Z7) i zapisem go do
tabeli `run_log`: `offers_fetched, offers_new, pct_with_ean, pct_matched,
products_new, deals_scored, quality_scored, sources_failed, duration_s`.
Zaimplementuj `RunReport` jako model w `models.py` i kontekstmenedżer
`run_report("collect")` w `db.py`, którego reszta zespołu będzie używać.

### 6. `collectors/__init__.py` — tylko kontrakt

Zdefiniuj protokół `Collector` i klasę bazową `BaseCollector` z:
limitowaniem zapytań (token bucket, konfigurowalny `rps` per źródło),
ponawianiem z wykładniczym backoffem (`httpx`), cache'em odpowiedzi na dysku
(klucz: hash URL-a + parametry; do testów offline), obsługą `content_hash`
przy zapisie `RawOffer`. **Nie implementuj żadnego konkretnego źródła** —
to zadanie A1.

### 7. Testy

- `conftest.py`: fixture `db` tworzący bazę w pamięci z pełnym schematem,
  fixture `sample_offers` z 20 realistycznymi ofertami (różne źródła, część
  bez EAN-u, część bez kosztu dostawy, jeden zestaw dwupak, jeden poleasing).
  Reszta zespołu będzie z tego korzystać — zrób to porządnie.
- Testy: walidacja GTIN, wyliczanie `price_total` i `price_incomplete`,
  idempotencja `upsert_offer`, jeden wpis `price_point` na dzień,
  odrzucenie `float` w polu ceny, migracje wykonują się na czystej bazie.

## Definicja ukończenia

1. `pip install -e .` przechodzi.
2. `dealradar db migrate` tworzy bazę od zera bez błędów.
3. `pytest` — wszystkie testy zielone, wypisz liczbę.
4. `dealradar collect --source dummy` wypisuje raport przebiegu i nie wywraca się.
5. `mypy src/dealradar` bez błędów (konfiguracja: `strict = true`).

## Czego NIE robisz

- Nie implementujesz żadnego kolektora, normalizatora, matchera ani scoringu.
- Nie budujesz UI ani API webowego.
- Nie dodajesz Alembica, Django, ORM-owych cudów ani warstw abstrakcji „na przyszłość".
- Nie wywołujesz żadnego modelu językowego.

## Raport końcowy

Wypisz sygnatury wszystkich modeli i funkcji publicznych z `models.py` i `db.py`
w formie zwięzłej listy — to jest kontrakt, który dostaną agenci A1–A6.
Zaznacz miejsca, w których `ARCHITEKTURA.md` był niejednoznaczny i jaką
decyzję podjąłeś.

---

> **Zasady twarde:** ceny to `int` w groszach; brak danych to `None` + powód,
> nigdy `0`; każda funkcja publiczna ma typy i docstring; żadnych zależności
> spoza `pyproject.toml` bez uzasadnienia; **zero wywołań LLM-a**; testy
> odpalasz sam i podajesz wynik; w raporcie wypisz co pominąłeś i dlaczego.
