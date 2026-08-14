# A3 — Matcher: rozpoznanie, że dwie oferty to ten sam produkt

**Wymaga ukończonego A0.** Twoja wyłączna własność: katalog `src/dealradar/matching/`.
Nie modyfikujesz `models.py`, `db.py`, `config.py`, `cli.py`.

---

## Kontekst

DealRadar wykrywa prawdziwe promocje. Przeczytaj `ARCHITEKTURA.md` w całości,
ze szczególną uwagą na sekcję 7 (matcher), tryb porażki **F2** i zasadę **Z5**.

**To jest najtrudniejsza i najważniejsza część systemu.** Wszystkie liczby,
które produkuje DealRadar — obniżki, mediany, rankingi, oceny — stoją na
założeniu, że klaster ofert to naprawdę jeden produkt. Jedno błędne scalenie
(1 TB z 512 GB, poleasing z nowym) zatruwa medianę i produkuje fałszywą
„okazję życia", której nikt nigdy nie wykryje.

> **Z5: fałszywe scalenie jest gorsze niż brak scalenia.**
> Optymalizujesz **precyzję**, cel ≥ 0,97. Kompletność jest drugorzędna.
> Niedopasowana oferta to strata. Źle dopasowana to kłamstwo w raporcie.

## Kontrakt

```python
def match_offers(session, full: bool = False) -> MatchReport: ...
def evaluate(session) -> EvalReport: ...   # precyzja/kompletność na zbiorze oznaczonym
```

Wejście: tabela `offer` (wypełniona przez A2 — masz tam `ean`, `brand_norm`,
`mpn_norm`, `title_norm`, `category_id`, `condition`, `is_bundle`, `price_total`
oraz atrybuty liczbowe w `product_attr`).
Wyjście: `offer.product_id`, `offer.match_confidence`, `offer.match_method`,
rekordy w `product`, pary graniczne w `match_review`.

## Kaskada — implementuj dokładnie w tej kolejności

### Krok 1 — EAN dokładny (`confidence = 0.99`)

Grupowanie po zwalidowanym EAN-ie (A2 gwarantuje poprawną sumę kontrolną).

**Zabezpieczenie przed śmieciowym EAN-em w feedach:** jeśli dwie oferty mają
ten sam EAN, ale podobieństwo `title_norm` < 0,30 → **nie scalaj**, zapisz parę
do `match_review` jako `conflict`. Sklepy notorycznie wklejają EAN producenta
do złego wariantu. Zliczaj takie konflikty — to metryka jakości źródła.

### Krok 2 — marka + MPN (`confidence = 0.95`)

Grupowanie po `(brand_norm, mpn_norm)`, oba niepuste. MPN krótszy niż 4 znaki
odrzucasz — `"A1"` nie jest identyfikatorem.

### Krok 3 — blocking + podobieństwo rozmyte (`confidence 0.60–0.85`)

**Blocking (obowiązkowy, inaczej masz porównanie N²):**
klucz `(brand_norm, category_id, kubełek cenowy)`, gdzie kubełek to przedział
±35% wokół ceny oferty. Oferta bez marki lub bez kategorii **nie wchodzi**
do kroku 3 — zostaje samotna. To celowe.

**Miara podobieństwa** — kombinacja trzech sygnałów:
1. TF-IDF na n-gramach znakowych 3–5 z `title_norm`, cosinus
   (`sklearn.feature_extraction.text.TfidfVectorizer`, `analyzer="char_wb"`).
   Jeden wektoryzator na blok, nie na całą bazę.
2. `rapidfuzz.fuzz.token_set_ratio` na `title_norm`.
3. Zgodność atrybutów liczbowych.

**Bramka G2 — atrybutowa. To jest jedyny powód, dla którego fuzzy matching
nadaje się do użytku.** Jeśli obie oferty mają wyekstrahowany ten sam atrybut
liczbowy i wartości się różnią (`capacity_gb`: 512 vs 1024) → **odrzut,
niezależnie od podobieństwa tekstu**. Tekstowo „SSD 512GB" i „SSD 1TB" są
niemal identyczne; to właśnie tak powstaje tryb porażki F2.
Tolerancja: 0% dla pojemności/rozdzielczości/liczby sztuk, ±2% dla wymiarów
i masy.

**Bramki twarde — nigdy nie scalamy ponad nimi, na żadnym kroku kaskady:**
- różna klasa `condition` (`new` / `refurb` / `used` / `damaged`)
- różny `bundle_size` lub `is_bundle` po jednej stronie
- sprzeczny atrybut liczbowy (G2)

Bramki obowiązują **także krok 1 i 2** — ten sam EAN dla nowego i poleasingowego
egzemplarza to dwa różne produkty z punktu widzenia ceny.

**Klastrowanie:** union-find po zaakceptowanych parach. Po zbudowaniu klastra
walidacja spójności: jeśli klaster zawiera oferty łamiące bramkę twardą
(bo scalenie przeszło przechodnio A–B, B–C, ale A i C się wykluczają) → rozbij
klaster i zgłoś do `match_review`. Przechodniość jest głównym źródłem cichych
błędów w union-find przy dopasowywaniu rekordów.

### Kanoniczny produkt

Dla klastra wybierz `canonical_title` jako **medoid** (tytuł o największej
sumie podobieństw do pozostałych), nie najdłuższy i nie pierwszy.
`ean`/`mpn` produktu: wartość większościowa, przy remisie — z oferty
o najwyższym `match_confidence`.

**Stabilność identyfikatorów.** `product.id` musi przeżyć ponowne uruchomienie
matchera. Przy `--full` nie wolno przenumerować produktów, bo `price_point`
i historia stracą sens. Przypisuj nowe oferty do istniejących klastrów;
nowy `product` twórz tylko wtedy, gdy nic nie pasuje.

## Ewaluacja — obowiązkowa część zadania, nie dodatek

Bez zbioru testowego strojenie progów to zgadywanie.

1. Zbuduj `dealradar match --eval` liczące **precyzję i kompletność**
   na tabeli `match_review` z ręcznie oznaczonymi parami (`label: same|different`).
2. Wygeneruj zbiór startowy: ≥ 200 par kandydackich z fixture'ów, wybranych
   **stratyfikowanie po podobieństwie** (nie tylko łatwe przypadki — najwięcej
   par z zakresu 0,5–0,8, bo tam leży granica decyzyjna). Oznacz je regułami
   bezspornymi tam, gdzie się da (identyczny EAN + identyczne atrybuty = `same`),
   resztę zostaw jako `unlabeled` do ręcznego przejrzenia i **napisz w raporcie,
   ile par czeka na człowieka**.
3. Progi akceptacji trzymaj w `config/matching.yaml`, nie w kodzie.
   Raport z ewaluacji ma pokazywać precyzję/kompletność dla kilku progów,
   żeby dało się wybrać świadomie.
4. Testy regresji: zestaw ~30 par, które **muszą** zostać rozdzielone
   (512 GB vs 1 TB, nowy vs poleasing, sztuka vs dwupak, wersja EU vs US,
   ładowarka do modelu X vs sam model X — ta ostatnia to klasyczna pułapka).

## Definicja ukończenia

1. `dealradar match` przypisuje `product_id` wszystkim ofertom z fixture'ów.
2. `dealradar match --eval` wypisuje precyzję ≥ 0,97 na oznaczonym zbiorze.
   Jeśli nie osiągasz tego progu — **podnieś progi akceptacji i oddaj niższą
   kompletność**, nie odwrotnie (Z5).
3. Raport podaje: `pct_matched`, rozbicie na `match_method`, liczbę klastrów,
   rozmiar największego klastra (klaster > 50 ofert to niemal na pewno błąd —
   sprawdź go i opisz), liczbę konfliktów EAN.
4. Testy regresji z 30 parami, które muszą pozostać rozdzielone — wszystkie zielone.
5. Ponowne `dealradar match` nie zmienia `product.id` istniejących produktów.

## Czego NIE robisz

- Nie liczysz obniżek, historii ani ocen — to A4 i A5.
- Nie pobierasz danych, nie normalizujesz — to A1 i A2.
- Nie używasz embeddingów neuronowych ani żadnego modelu językowego.
  TF-IDF na n-gramach znakowych + RapidFuzz + bramka atrybutowa to komplet.
  Ma być deterministyczne i odtwarzalne.
- Nie dotykasz plików A0.

## Raport końcowy

Podaj precyzję i kompletność przy wybranym progu, listę przypadków, w których
matcher się myli (konkretne pary z fixture'ów), i ocenę: **dla których źródeł
dopasowanie w ogóle ma sens**. Jeśli AliExpress ma 3% ofert z EAN-em i tytuły
bez marki — napisz wprost, że dla tego źródła krok 3 nie działa i oferty
zostają samotne. To poprawny wynik, nie porażka.

---

> **Zasady twarde:** ceny to `int` w groszach; brak danych to `None` + powód,
> nigdy `0`; każda funkcja publiczna ma typy i docstring; żadnych zależności
> spoza `pyproject.toml` bez uzasadnienia; **zero wywołań LLM-a**; testy
> odpalasz sam i podajesz wynik; w raporcie wypisz co pominąłeś i dlaczego.
