# A6 — Raporty, watchlista i orkiestracja przebiegu

**Wymaga ukończonego A0; A1–A5 mogą być w trakcie** (pracujesz na kontraktach,
brakujące etapy zastępujesz zaślepkami i danymi z fixture'ów).
Twoja wyłączna własność: `src/dealradar/reporting/`, `src/dealradar/pipeline.py`.
Nie modyfikujesz `models.py`, `db.py`, `config.py` ani katalogów A1–A5.

---

## Kontekst

DealRadar wykrywa prawdziwe promocje i ocenia stosunek jakość/cena.
Przeczytaj `ARCHITEKTURA.md` — sekcje 4, 11 i zasady **Z1** (odtwarzalność),
**Z6** (dowody), **Z7** (awaria źródła musi być widoczna) oraz tryb porażki **F5**.

Twoja rola: skleić etapy w jeden przebieg i **pokazać człowiekowi wynik
w formie, która daje się ocenić w 30 sekund**. Raport, który wymaga
zaufania na słowo, jest nieodróżnialny od agregatora okazji — czyli bezwartościowy.

## Zadanie 1 — orkiestracja (`pipeline.py`)

```python
def run_full(session, cfg: Config) -> RunReport: ...     # nocny przebieg
def run_fast(session, cfg: Config) -> RunReport: ...     # godzinowy: Pepper + watchlista
def rebuild(session, from_stage: str, since: date | None) -> RunReport: ...
```

- Kolejność pełnego przebiegu: `collect → normalize → match → snapshot → score
  --deals → score --quality → report`.
- **Awaria etapu nie kasuje pracy poprzednich.** Każdy etap zatwierdza swoją
  transakcję; wyjątek loguje się, oznacza etap jako `FAILED` i przebieg idzie
  dalej tam, gdzie ma to sens (bez `match` nie ma `score` — pomiń i powiedz,
  dlaczego, zamiast wywalać cały proces).
- `rebuild --from raw` odtwarza wszystko pochodne od zera na istniejących
  `raw_offer` (Z1). To musi realnie działać — algorytm scoringu zmieni się
  jeszcze wiele razy.
- Blokada współbieżności: dwa równoległe przebiegi na tej samej bazie to
  uszkodzone dane. Plik-lock albo tabela `run_lock` z TTL.

## Zadanie 2 — alarmy zdrowia systemu (F5, Z7)

To jest funkcja, którą wszyscy pomijają i przez którą narzędzia tego typu
umierają po cichu: feed zmienia format, kolektor zwraca zero, raport pokazuje
„brak okazji" i wygląda **całkowicie normalnie**.

Po każdym przebiegu porównaj z medianą z 7 ostatnich przebiegów:
`offers_fetched`, `pct_with_ean`, `pct_matched`, `deals_scored`.

Spadek > 30% → sekcja **„UWAGA"** na samej górze raportu, przed okazjami,
z nazwą źródła i obiema liczbami. Źródło, które zwróciło 0 rekordów przy
niepustym poprzednim przebiegu, zawsze ląduje w tej sekcji.

## Zadanie 3 — raport dzienny (`reporting/digest.py`)

Dwa formaty z jednego modelu danych: **tekst** (terminal, mail) i **HTML**
(samodzielny plik, bez zasobów zewnętrznych, czytelny w trybie jasnym i ciemnym).

Struktura, w tej kolejności:

1. **UWAGI** — problemy ze zdrowiem systemu (jeśli są). Nigdy nie chowaj tego
   na dole.
2. **Top okazji** — posortowane po `deal_score`, domyślnie 20.
   Każda pozycja pokazuje **dlaczego jest okazją, nie tylko że jest**:

```
 87  Samsung 990 PRO 2TB M.2                              649,00 zł
     ├─ mediana 90 dni: 879 zł  ·  minimum historyczne: 699 zł  ← nowe minimum
     ├─ najtańsza z 7 ofert w 4 sklepach  ·  historia: 64 dni
     ├─ jakość/cena: 8,1/10 (12 konkurentów w paśmie 490–810 zł)
     ├─ sklep deklaruje −45%, realnie −26%
     └─ x-kom · gwarancja 60 mc · dostawa 0 zł
```

   Wymogi:
   - **Zawsze `price_total`**, nigdy cena bez dostawy (F3).
   - Rozjazd deklaracja/rzeczywistość pokazuj wtedy i tylko wtedy, gdy istnieje
     realna historia — inaczej pomijasz ten wiersz.
   - Pozycja oparta na `basis = "cross_shop"` (zimny start) musi być **wizualnie
     oznaczona**: „bez historii — porównanie tylko międzysklepowe".
   - Produkt bez oceny jakości pokazuje „brak oceny: nie mam formuły dla tej
     kategorii", a nie pusty wiersz ani zero.
3. **Watchlista** — trafienia na obserwowanych produktach, niezależnie od progu
   `deal_score`.
4. **Wskaźnik ściemy** — 5 sklepów z największym rozjazdem deklaracji, z liczbą
   ofert w próbce.
5. **Stopka** — statystyki przebiegu (Z7) i czas trwania.

Zasada nadrzędna dla całego raportu: **każda liczba ma być sprawdzalna**.
Jeśli piszesz „mediana 90 dni: 879 zł", to `deal.evidence` musi zawierać dane,
z których to wyszło, a raport HTML ma je pokazywać po rozwinięciu.

## Zadanie 4 — watchlista i powiadomienia

- CRUD przez CLI: `dealradar watch add --product-id 123 --max-price 600`
  oraz wariant z frazą, gdy produkt jeszcze nie istnieje w bazie.
- Warunki: `max_price`, `min_deal_score`, `any_alltime_low`.
- Kanały jako wtyczki za jednym interfejsem `Notifier`: `console`, `file`,
  `smtp`, `telegram`. Brak konfiguracji kanału → kanał się grzecznie wyłącza,
  nie wywala przebiegu.
- **Antyspam**: to samo trafienie nie może być wysłane dwa razy. Tabela
  `notification_log` z kluczem `(watch_id, product_id, price_bucket)`;
  ponowne powiadomienie tylko przy cenie niższej o ≥ 3% od poprzedniej wysyłki.
  Bez tego watchlista zaczyna być ignorowana po trzech dniach i cała funkcja
  jest martwa.

## Definicja ukończenia

1. `dealradar run` wykonuje pełny przebieg na fixture'ach i produkuje raport.
2. Celowo zepsuty etap (podmień źródło na wywalające się) → przebieg kończy się,
   raport zawiera sekcję UWAGI, pozostałe etapy działają.
3. `dealradar rebuild --from raw` odtwarza `offer`, `product`, `deal`,
   `quality_score` z samych `raw_offer` — wyniki identyczne z pierwotnymi
   (napisz test porównujący sumy kontrolne obu przebiegów; to jest test
   zasady Z1).
4. `dealradar report --format html --out digest.html` — plik otwiera się
   samodzielnie, bez internetu, czytelny w trybie jasnym i ciemnym.
5. Dwa równoległe `dealradar run` → drugi kończy się komunikatem o blokadzie,
   nie uszkodzeniem danych.
6. Test antyspamu: to samo trafienie dwa przebiegi z rzędu → jedno powiadomienie.
7. `pytest tests/reporting/` zielone — podaj liczbę.

## Czego NIE robisz

- Nie liczysz żadnych ocen ani obniżek — konsumujesz gotowe z `deal`
  i `quality_score`. Jeśli brakuje ci danych do wyświetlenia, zgłoś to
  w raporcie końcowym; **nie licz tego u siebie po swojemu**, bo powstaną dwie
  wersje prawdy.
- Nie budujesz aplikacji webowej z bazą użytkowników, logowaniem i API.
  Samodzielny HTML i CLI. Web dopiero wtedy, gdy dane będą miały sens.
- Nie wysyłasz niczego na zewnątrz bez jawnej konfiguracji kanału przez
  użytkownika. Domyślny kanał to `console`.
- Nie wywołujesz żadnego modelu językowego — raport składa się z liczb, które
  już są w bazie.

## Raport końcowy

Załącz przykładowy raport tekstowy wygenerowany na fixture'ach. Napisz wprost,
które pozycje raportu byłyby **mylące dla użytkownika** przy obecnym stanie
danych (np. „przy 5 dniach historii sekcja top okazji pokazuje głównie zimny
start i nie należy jej ufać") — to jest informacja ważniejsza niż sam fakt,
że kod działa.

---

> **Zasady twarde:** ceny to `int` w groszach; brak danych to `None` + powód,
> nigdy `0`; każda funkcja publiczna ma typy i docstring; żadnych zależności
> spoza `pyproject.toml` bez uzasadnienia; **zero wywołań LLM-a**; testy
> odpalasz sam i podajesz wynik; w raporcie wypisz co pominąłeś i dlaczego.
