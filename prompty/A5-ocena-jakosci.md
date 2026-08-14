# A5 — Ocena jakość/cena 1–10 i grupy porównawcze

**Wymaga ukończonego A0 i A4** (potrzebujesz `deal_score`).
Twoja wyłączna własność: `src/dealradar/scoring/quality.py`,
`config/categories/*.yaml`.
Nie modyfikujesz `models.py`, `db.py`, `config.py`, `cli.py` ani katalogów A1–A4.

---

## Kontekst

DealRadar ocenia, czy produkt jest wart swojej ceny **na tle konkurencji
w tej samej półce cenowej**. Przeczytaj `ARCHITEKTURA.md` — sekcję 9
implementujesz dosłownie, plus zasady **Z4** (brak danych to nie zero),
**Z6** (dowody) i tryb porażki **F4**.

Rdzeń jest deterministyczny — **żadnego modelu językowego**. To znaczy, że nie
możesz „ocenić jakości" niczego, czego nie da się policzyć. I to jest zasada,
a nie ograniczenie do obejścia:

> **Z4: ocena wymyślona jest gorsza niż jej brak, bo wygląda tak samo
> jak prawdziwa.**

## Kontrakt

```python
def score_quality(session, product_ids: list[int] | None = None) -> QualityReport: ...
def build_peer_group(session, product_id: int) -> PeerGroup | None: ...
```

Wejście: `product`, `product_attr`, `offer` (oceny, gwarancja, sprzedawca),
`deal` (od A4), `source.risk_prior`.
Wyjście: rekordy `quality_score` z `total_1_10`, czterema składowymi,
`confidence`, `missing_reason` i `evidence`.

## Zadanie 1 — grupa porównawcza

Bez poprawnej grupy porównawczej ocena „7,4/10" nie znaczy nic.

- Ta sama **kategoria liściowa**, cena w przedziale **±25%** wokół mediany ceny
  produktu P, oferta dostępna.
- Minimum **5 członków**. Jeśli mniej — rozszerzaj pasmo co 5 pp, maksymalnie
  do ±60%. Dalej mniej niż 5 → `confidence = "low"`, ale licz.
- Poniżej 3 członków → **`total_1_10 = None`, `missing_reason = "peer_group_too_small"`**.
  Nie wymyślasz oceny (Z4).
- Grupa porównawcza jest zapisywana w `evidence` jako lista `product_id`
  z ich cenami — musi dać się ręcznie sprawdzić, z kim porównywałeś.

Pułapka do obsłużenia: produkt sprzedawany też w zestawach albo w wersji
poleasingowej trafi do grupy jako sztucznie tani konkurent. Filtruj
`condition = new`, `is_bundle = False` **po stronie ofert**, zanim policzysz
medianę ceny produktu.

## Zadanie 2 — cztery składowe

Każda normalizowana do **percentyla w grupie porównawczej**, nie do wartości
bezwzględnej. „Dobry" znaczy „lepszy od konkurencji w tej cenie", nie „ma dużo
gigabajtów".

### 1. Niezawodność (obrona przed F4)

Ocena ściągnięta bayesowsko:
```
r* = (C*m + suma_ocen) / (C + n)      # C = 30, m = mediana ocen w kategorii
```
5,0 z trzech opinii ma wylądować **niżej** niż 4,6 z dwóch tysięcy. Napisz test,
który to sprawdza — to jest cały sens tego wzoru.

**Modyfikator zgodności międzysklepowej:** jeśli produkt ma oceny z ≥ 2 źródeł
i różnią się o ≤ 0,4 → premia (niezależne potwierdzenie).
Jeśli różnią się o > 0,8 → karany jest **wyższy** odczyt, bo kupione opinie są
lokalne dla jednego sklepu. Zapisz obie oceny w `evidence`.

Brak ocen w ogóle → składowa `None`, nie 0 (Z4).

### 2. Wartość — użyteczność na złotówkę

Formuły użyteczności **per kategoria**, w `config/categories/<kategoria>.yaml`:

```yaml
category: dyski_ssd
required_attrs: [capacity_tb]
utility: "capacity_tb * (1 + 0.3 * norm(read_mbs, 500, 7000))"
weights: {value: 0.40, reliability: 0.30, deal: 0.20, risk: 0.10}
```

- Zaimplementuj **bezpieczny ewaluator wyrażeń** — biała lista funkcji
  (`norm`, `min`, `max`, `log`, operatory arytmetyczne) na AST.
  **Nie `eval()`.** Plik konfiguracyjny nie jest zaufanym kodem.
- `norm(x, lo, hi)` → skalowanie liniowe do 0–1 z obcięciem.
- Brak któregokolwiek `required_attrs` → składowa `value = None`.

Napisz formuły na start dla: **dyski_ssd, monitory, powerbanki, laptopy,
telefony, słuchawki**. Po ~20 linii YAML-a na kategorię. To jest uczciwa praca
domenowa i jedyna część systemu, której nie da się obejść — jeśli nie wiesz,
co czyni monitor dobrym, system też nie będzie wiedział.

### 3. Siła promocji

`deal_score / 100` od agenta A4. Nie przeliczasz, nie modyfikujesz.

### 4. Ryzyko (odwrócone — mniejsze ryzyko, wyższy wkład)

Gwarancja w miesiącach, polityka zwrotów, `condition`, ocena sprzedawcy,
`source.risk_prior` z konfiguracji (x-kom ≈ 0,0; AliExpress ≈ 0,4).

## Zadanie 3 — wynik i bramka G3

```
total_1_10 = 1 + 9 * Σ(waga_i * percentyl_i)
```
Wagi per kategoria z YAML-a, renormalizowane, gdy któraś składowa jest `None`.

**Bramka G3 — zwracasz `None` + `missing_reason`, gdy:**
- brak formuły użyteczności dla kategorii → `"no_utility_formula"`
- < 3 członków grupy porównawczej → `"peer_group_too_small"`
- brak ≥ 2 z 4 składowych → `"insufficient_signals"`

Produkt bez oceny **musi** przejść przez system i pojawić się w raporcie
z adnotacją „brak oceny jakości — nie mam formuły dla tej kategorii".
Zniknięcie go z wyników byłoby cichym kłamstwem przez pominięcie.

**Zaokrąglanie:** wynik do jednego miejsca po przecinku. Nie pokazuj `7,43` —
sugeruje precyzję, której nie ma.

## Definicja ukończenia

1. `dealradar score --quality` wypełnia `quality_score` dla produktów
   z zamapowaną kategorią.
2. Raport podaje: ile produktów ocenionych, ile z `missing_reason` (z rozbiciem
   na powody), rozkład ocen. **Jeśli 80% produktów nie ma oceny — napisz to
   wprost jako główny wniosek**, to jest prawdziwy stan systemu.
3. Testy:
   - ściąganie bayesowskie: 5,0 z n=3 wypada niżej niż 4,6 z n=2000
   - rozbieżne oceny z dwóch źródeł → wyższa jest karana
   - brak formuły kategorii → `None` + `"no_utility_formula"`, nie liczba
   - grupa porównawcza rozszerza pasmo do ±60% i zatrzymuje się
   - ewaluator wyrażeń odrzuca `__import__` i wywołania spoza białej listy
   - dwa produkty o identycznych atrybutach i różnej cenie → tańszy ma wyższą
     składową `value`
4. `pytest tests/scoring/test_quality.py` zielone — podaj liczbę.

## Czego NIE robisz

- Nie liczysz `deal_score` od nowa — bierzesz gotowy od A4.
- Nie dopasowujesz produktów, nie normalizujesz, nie pobierasz danych.
- **Nie wywołujesz modelu językowego, żeby „ocenić jakość" produktu bez danych.**
  Jeśli nie ma formuły — nie ma oceny. To jest zamierzone (Z4).
- Nie używasz `eval()` do formuł z YAML-a.
- Nie dodajesz składowych „subiektywnych" typu popularność marki bez twardego
  sygnału za nimi.

## Raport końcowy

Podaj: dla ilu kategorii istnieje formuła i jaki procent ofert w bazie one
pokrywają; listę 10 najczęstszych kategorii **bez** formuły (to gotowa lista
roboty); przykład jednego produktu z pełnym rozbiciem oceny na składowe
i percentyle — żeby dało się ręcznie sprawdzić, czy liczba ma sens.

---

> **Zasady twarde:** ceny to `int` w groszach; brak danych to `None` + powód,
> nigdy `0`; każda funkcja publiczna ma typy i docstring; żadnych zależności
> spoza `pyproject.toml` bez uzasadnienia; **zero wywołań LLM-a**; testy
> odpalasz sam i podajesz wynik; w raporcie wypisz co pominąłeś i dlaczego.
