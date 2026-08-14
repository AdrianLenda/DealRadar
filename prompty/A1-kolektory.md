# A1 — Kolektory: pobieranie surowych ofert ze źródeł

**Wymaga ukończonego A0.** Twoja wyłączna własność: katalog `src/dealradar/collectors/`.
Nie modyfikujesz `models.py`, `db.py`, `config.py`, `cli.py`.

---

## Kontekst

DealRadar wykrywa prawdziwe promocje w sklepach internetowych. Przeczytaj
`ARCHITEKTURA.md`, sekcje 4 (pipeline), 6 (warstwa źródeł) i zasadę **Z1**
(surowe dane nietykalne) oraz **Z7** (awaria źródła musi być widoczna).

Twój etap to **etap 1: collect**. Pobierasz dane i zapisujesz je jako
`RawOffer` — surowy JSON dokładnie taki, jaki przyszedł ze źródła.
**Nie parsujesz, nie normalizujesz, nie liczysz cen.** To robi agent A2.

## Kontrakt

Implementujesz protokół zdefiniowany przez A0 w `collectors/__init__.py`:

```python
class Collector(Protocol):
    source_name: str
    kind: Literal["api", "feed", "rss"]
    def fetch(self, since: datetime | None) -> Iterator[RawOffer]: ...
```

Klasa bazowa `BaseCollector` już daje ci: limitowanie zapytań, ponawianie
z backoffem, cache odpowiedzi na dysku, liczenie `content_hash`.
**Użyj jej — nie pisz własnego HTTP.**

## Kolektory do zaimplementowania (w tej kolejności)

### 1. `feed_xml.py` — generyczny parser feedów afiliacyjnych *(najważniejszy)*

To jest mnożnik całego projektu: jeden kolektor obsługuje kilkadziesiąt polskich
sklepów, bo sieci afiliacyjne (Awin, TradeDoubler, Convertiser, Adtraction)
publikują feedy produktowe w XML/CSV z EAN-em, ceną i dostępnością.

- Konfiguracja per feed w `config/sources.yaml`: URL, format, mapowanie nazw
  pól (bo każda sieć nazywa je inaczej: `ean`/`gtin`/`barcode`,
  `price`/`price_gross`/`cena`).
- Strumieniowanie przez `lxml.etree.iterparse` — feedy potrafią mieć 500 MB,
  nie wolno ładować całości do pamięci.
- Obsługa gzip i plain, `Last-Modified`/`ETag` (nie pobieraj tego samego dwa razy).
- Wsparcie CSV/TSV z automatycznym wykrywaniem separatora i kodowania
  (windows-1250 zdarza się w polskich feedach — obsłuż to jawnie).
- **Nie odrzucaj rekordów, których nie rozumiesz.** Zapisujesz surowo;
  filtrowaniem zajmie się A2.

### 2. `pepper_rss.py` — okazje z Pepper.pl

- Kanał RSS gorących okazji. Parsowanie `feedparser` lub `lxml`.
- To jest „szybki tor" systemu — uruchamiany co godzinę, nie raz na dobę.
- Rekord RSS jest ubogi (tytuł, link, cena w tytule, opis). Zapisz całość
  surowo — A2 wyciągnie z tego, co się da.
- Zachowaj `pepper_temperature` (liczba głosów) w ładunku, jeśli jest — to
  niezależny sygnał jakości okazji.

### 3. `ibood.py` — feed dzienny

- Mały wolumen, prosty format. Zrób go szybko, posłuży jako test pipeline'u
  end-to-end.

### 4. `allegro_api.py` — oficjalne API Allegro

- OAuth2 client credentials; `client_id`/`client_secret` **wyłącznie ze zmiennych
  środowiskowych** (`ALLEGRO_CLIENT_ID`, `ALLEGRO_CLIENT_SECRET`).
- Odświeżanie tokenu z zapasem czasowym, cache tokenu między przebiegami.
- Wyszukiwanie ofert z stronicowaniem; parametry zapytań (kategorie, frazy)
  z konfiguracji, nie zaszyte w kodzie.
- **Poszanowanie limitów zapytań**: obsłuż nagłówki limitów i `429` przez
  wstrzymanie, nie przez ponawianie w pętli.
- Zachowaj pełny obiekt oferty łącznie z tablicą parametrów — tam siedzą EAN-y
  i atrybuty, których A2 i A3 będą potrzebować.

### 5. `aliexpress.py` — API partnerskie *(opcjonalny, jeśli są klucze)*

- Jeśli brak kluczy w środowisku, kolektor ma się **grzecznie wyłączyć**
  (zalogować „disabled: no credentials" i zwrócić pusty iterator), a nie wywalić
  przebieg.

## Wymagania przekrojowe

**Izolacja awarii (Z7).** Wyjątek w jednym kolektorze nie może zatrzymać
pozostałych. Każdy kończy się wpisem: `source, fetched, new, errors, duration_s`.
Źródło, które zwróciło **0 rekordów przy niepustym poprzednim przebiegu**,
oznacz jako `FAILED`, nie `OK` — to jest tryb porażki **F5** z architektury
i jedyny sposób, żeby zauważyć, że feed zmienił format.

**Idempotencja.** Ponowne uruchomienie tego samego dnia nie tworzy duplikatów.
Rekord o niezmienionym `content_hash` pomijasz (ale aktualizujesz `last_seen`).

**Tryb offline do testów.** Każdy kolektor musi dać się uruchomić z nagranych
odpowiedzi w `tests/fixtures/<source>/`. Nagraj po 2–3 realistyczne odpowiedzi
na źródło (okrojone do ~50 rekordów) i napisz testy działające **bez sieci**.
Testy sięgające do internetu są zabronione — będą się losowo wywracać.

**Ograniczenia prawne — nie do negocjacji.**
- Wyłącznie oficjalne API, feedy afiliacyjne i publiczne kanały RSS.
- **Nie implementujesz kolektorów dla Ceneo i Temu** — nie mają legalnej ścieżki
  publicznej.
- Nie obchodzisz zabezpieczeń anty-botowych: żadnego rozwiązywania CAPTCHA,
  rotacji proxy, podszywania się pod przeglądarkę w celu ominięcia blokad.
  Uczciwy `User-Agent` identyfikujący narzędzie i respektowanie `robots.txt`
  oraz `Retry-After`.

## Definicja ukończenia

1. `dealradar collect --source ibood` zapisuje rekordy do `raw_offer` i wypisuje raport.
2. `dealradar collect` uruchamia wszystkie włączone źródła; celowo zepsute
   źródło (podmień URL na nieistniejący) **nie zatrzymuje** reszty i pojawia się
   w `sources_failed`.
3. `pytest tests/collectors/` zielone, bez dostępu do sieci — podaj liczbę testów.
4. Powtórne `collect` na tych samych fixture'ach daje `offers_new = 0`.

## Czego NIE robisz

- Nie parsujesz cen na liczby, nie normalizujesz marek, nie wykrywasz stanu
  produktu — to A2.
- Nie dopasowujesz ofert do produktów — to A3.
- Nie dotykasz plików A0.
- Nie wywołujesz żadnego modelu językowego.

## Raport końcowy

Wypisz: które kolektory działają w pełni, które są zaślepkami z powodu braku
kluczy, jakie pola każde źródło realnie dostarcza (szczególnie: **czy jest EAN**
i jaki procent rekordów go ma) — A3 zbuduje na tym strategię dopasowania.

---

> **Zasady twarde:** ceny to `int` w groszach; brak danych to `None` + powód,
> nigdy `0`; każda funkcja publiczna ma typy i docstring; żadnych zależności
> spoza `pyproject.toml` bez uzasadnienia; **zero wywołań LLM-a**; testy
> odpalasz sam i podajesz wynik; w raporcie wypisz co pominąłeś i dlaczego.
