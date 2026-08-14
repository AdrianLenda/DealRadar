# A4 — Historia cen i siła promocji (`deal_score`)

**Wymaga ukończonego A0.** Twoja wyłączna własność: `src/dealradar/scoring/history.py`,
`src/dealradar/scoring/deal.py`, `config/scoring.yaml`.
Nie modyfikujesz `models.py`, `db.py`, `config.py`, `cli.py` ani katalogów A1–A3.

---

## Kontekst

DealRadar wykrywa **prawdziwe** promocje. Przeczytaj `ARCHITEKTURA.md` —
sekcję 8 implementujesz dosłownie, plus zasady **Z1**, **Z3**, **Z6**
i tryby porażki **F1**, **F3**.

To jest miejsce, w którym powstaje główna wartość produktu. Agregatorów okazji
są dziesiątki i wszystkie powtarzają liczbę podaną przez sklep. Ty budujesz
jedyną rzecz, której sklep nie kontroluje: **własny pomiar tego, ile ten produkt
naprawdę kosztował przez ostatnie trzy miesiące**.

> **Z3: cena „przed obniżką" podana przez sklep nie jest danymi o cenie.**
> Nie wolno jej użyć do wyliczenia obniżki. Nigdzie. Pod żadnym warunkiem.

## Kontrakt

```python
def snapshot_prices(session, at: date | None = None) -> SnapshotReport: ...
def score_deals(session, product_ids: list[int] | None = None) -> DealReport: ...
def price_series(session, product_id: int, days: int) -> list[PricePoint]: ...
```

Wejście: `offer` (z `product_id` od A3) + `price_point`.
Wyjście: rekordy `deal` + `shop_credibility`.

## Zadanie 1 — snapshot cen (`history.py`)

**To jest najpilniejsza rzecz w całym projekcie.** Historia cen zaczyna być
wartościowa po ~30 dniach i jest jedynym zasobem, którego nie da się nadrobić
później. Zrób to najpierw i uruchom, zanim reszta systemu będzie gotowa.

- Raz na dobę: dla każdej aktywnej oferty zapisz `price_point(offer_id,
  observed_at, price_total, in_stock)`.
- Append-only, **maksymalnie jeden wpis na ofertę na dobę** (helper
  `append_price_point` z A0 to gwarantuje).
- Oferta, która zniknęła ze źródła, dostaje wpis `in_stock = False` przez
  kolejne 7 dni, potem przestajesz ją odpytywać. Znikanie i powrót oferty to
  sygnał, nie brak danych.
- **Kompaktowanie** (uruchamiane osobną komendą, nie automatycznie): punkty
  starsze niż 180 dni redukuj do jednego wpisu tygodniowo (mediana tygodnia),
  zachowując zawsze minimum i maksimum tygodnia. Baza rośnie liniowo z czasem
  i liczbą ofert — 100 tys. ofert × 365 dni to 36 mln wierszy rocznie.

## Zadanie 2 — `deal_score` (`deal.py`)

Implementacja wzoru z §8 architektury. Przepisz go do kodu **dokładnie**,
z parametrami w `config/scoring.yaml`, nie zaszytymi w źródle.

### Baza historyczna

```
H = price_point produktu P, ostatnie 90 dni
    filtry: condition = new, in_stock = True, match_confidence >= 0.90,
            price_incomplete = False

med   = median(H)
mad   = median(|h - med|)
sigma = max(1.4826 * mad, 0.01 * med)     # podłoga: chroni przed ceną stałą
z     = (med - price_total) / sigma
depth = 1 - price_total / med
```

**Mediana i MAD, nie średnia i odchylenie standardowe.** Jedna wyprzedaż resztek
psuje średnią na kwartał. Napisz test, który to pokazuje: seria 89 dni po 1000 zł
i jeden dzień po 200 zł — średnia mówi, że 850 zł to okazja, mediana i MAD
mówią, że nie.

### Składowe i wynik

```
s_z     = clamp(z / 4, 0, 1)
s_depth = clamp(depth / 0.35, 0, 1)
s_rank  = 1.0 najtańsza dziś | 0.5 w top-3 | 0.0 poza
s_atl   = 1.0 gdy price_total <= min(H)

raw = 0.35*s_z + 0.30*s_depth + 0.20*s_rank + 0.15*s_atl
```

### Bramka pokrycia G1 — sufit, nie kara

| Warunek | Sufit |
|---|---|
| historia < 14 dni | 0,50 |
| historia < 30 dni | 0,70 |
| < 3 konkurencyjne oferty | 0,60 |
| < 2 źródła | 0,80 |

```
deal_score = 100 * min(raw, min(obowiązujące sufity))
```

Sufit, nie mnożnik — produkt z rewelacyjną ceną i cienką historią ma dostać
„może być, ale nie wiem", a nie sztucznie zaniżony wynik proporcjonalny.

### Zimny start (`basis = "cross_shop"`)

Brak historii (< 3 punkty) → `med` zastąpione **medianą dzisiejszych cen
wszystkich ofert produktu P**, `s_z = 0` (wagę rozdziel proporcjonalnie na
pozostałe składowe), sufit 0,70. Działa od pierwszego dnia, o ile produkt
ma pokrycie w kilku sklepach. Produkt z jedną ofertą i bez historii → **brak
`deal`**, nie `deal_score = 0` (Z4).

### Cena całkowita (F3)

Do rankingu wchodzi wyłącznie `price_total`. Oferta z `price_incomplete = True`
może trafić do historii, ale **nie do porównania rankingowego** —
inaczej „najtańsza" zawsze będzie ta z ukrytym kosztem dostawy 39 zł.

### Dowody (Z6)

`deal.evidence` zapisuje: `med`, `mad`, `sigma`, liczbę punktów historii,
zakres dat, listę `offer_id` konkurentów użytych do rankingu, zastosowany sufit
i jego powód. Bez tego nie odpowiesz na pytanie „czemu to dostało 84".

## Zadanie 3 — wskaźnik ściemy (`shop_credibility`)

Osobna, nietypowa metryka, której nie daje żadna porównywarka — i bezpośrednia
odpowiedź na tryb porażki **F1**.

Dla każdej oferty, która ma `list_price_claimed`:
```
claimed_discount = 1 - price_total / list_price_claimed
real_discount    = depth   (obniżka wobec Twojej mediany 90-dniowej)
```
Agreguj per sklep za okres: `bullshit_index = mean(claimed_discount - real_discount)`
z liczbą ofert w próbce. Sklep deklarujący −60% przy realnych −5% ma dostać
etykietę widoczną w raporcie.

Wymóg: liczysz to **wyłącznie** z ofert mających realną historię (≥ 30 dni),
inaczej porównujesz deklarację z niczym. Sklep z próbką < 20 ofert →
`bullshit_index = None`.

## Definicja ukończenia

1. `dealradar snapshot` zapisuje punkty cenowe; uruchomiony dwa razy tego samego
   dnia nie tworzy duplikatów.
2. `dealradar score --deals` wypełnia tabelę `deal` z `evidence`.
3. Testy na **syntetycznych seriach czasowych** (to jest sedno tego zadania):
   - cena stała 90 dni, potem −40% → wysoki `deal_score`
   - cena podbita o 30% na 10 dni, potem „przecena" do poziomu wyjściowego →
     `deal_score` bliski zeru **i** wysoki `claimed - real` (obrona przed F1)
   - jeden odstający punkt w serii nie zmienia mediany (odporność MAD)
   - 5 punktów historii → sufit 0,50 zastosowany i zapisany w `evidence`
   - produkt z jedną ofertą bez historii → brak rekordu `deal`, nie zero
4. `pytest tests/scoring/` zielone — podaj liczbę testów.
5. Wydajność: scoring 10 000 produktów < 60 s na SQLite. Jeśli nie —
   napisz, gdzie jest wąskie gardło, zamiast to ukrywać.

## Czego NIE robisz

- Nie liczysz oceny jakości 1–10 ani grup porównawczych — to A5.
  Wystawiasz mu tylko `deal_score`.
- Nie dopasowujesz produktów, nie normalizujesz, nie pobierasz danych.
- **Nie używasz `list_price_claimed` do wyliczenia obniżki** (Z3). Jedyne
  dozwolone użycie tego pola to wskaźnik ściemy.
- Nie wywołujesz żadnego modelu językowego. To czysta statystyka.

## Raport końcowy

Podaj rozkład `deal_score` na danych testowych (histogram w tekście), procent
produktów z podstawą `history` vs `cross_shop`, i ocenę: **po ilu dniach
zbierania danych system zacznie dawać sensowne wyniki** przy obecnym pokryciu
źródeł. To jest liczba, którą użytkownik musi znać, zanim zacznie ufać
raportom.

---

> **Zasady twarde:** ceny to `int` w groszach; brak danych to `None` + powód,
> nigdy `0`; każda funkcja publiczna ma typy i docstring; żadnych zależności
> spoza `pyproject.toml` bez uzasadnienia; **zero wywołań LLM-a**; testy
> odpalasz sam i podajesz wynik; w raporcie wypisz co pominąłeś i dlaczego.
