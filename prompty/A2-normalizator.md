# A2 — Normalizator: surowy JSON → kanoniczna oferta

**Wymaga ukończonego A0.** Twoja wyłączna własność: katalog `src/dealradar/normalize/`
oraz `config/brands.yaml`, `config/category_map.yaml`.
Nie modyfikujesz `models.py`, `db.py`, `config.py`, `cli.py`.

---

## Kontekst

DealRadar wykrywa prawdziwe promocje w sklepach internetowych. Przeczytaj
`ARCHITEKTURA.md` — sekcje 4, 5 i zasady **Z2** (grosze, cena całkowita),
**Z3** (nie ufamy cenie przekreślonej), **Z4** (brak danych to nie zero).

Twój etap to **etap 2: normalize**. Czytasz `raw_offer`, produkujesz `offer`.
Jesteś jedynym miejscem w systemie, które wie, jak wygląda bałagan w danych
źródłowych — dalsze etapy widzą już tylko czyste rekordy.

## Kontrakt

```python
def normalize_batch(session, since: datetime | None) -> NormalizeReport: ...

class SourceNormalizer(Protocol):
    source_name: str
    def to_offer(self, raw: RawOffer) -> Offer | RejectedOffer: ...
```

Architektura: **deklaratywne mapowanie pól w YAML** (`config/normalizers/<source>.yaml`)
+ opcjonalny hak w Pythonie dla przypadków, których nie da się opisać mapowaniem.
Dodanie nowego sklepu z feedu afiliacyjnego ma być zmianą w YAML-u, nie w kodzie.

`RejectedOffer` niesie powód odrzucenia i **ląduje w tabeli, nie w koszu** —
musisz umieć odpowiedzieć na pytanie „dlaczego z 40 tysięcy rekordów zostało 31 tysięcy".

## Co konkretnie robisz

### 1. Ceny (Z2)

- Parsowanie kwot z wszystkiego, co się trafia: `"1 299,00 zł"`, `"1299.00 PLN"`,
  `"1.299,00"`, `1299.0`, `"od 1299 zł"`. Wynik: `int` groszy.
- `price_total = price_gross + shipping_cost`. Gdy koszt dostawy nieznany →
  `shipping_cost = None`, `price_incomplete = True`.
- Waluty inne niż PLN: przelicz po kursie z dnia (tabela `fx_rate`, źródło NBP)
  i **zapisz kurs w `Offer.evidence`** — inaczej za pół roku nie odtworzysz, skąd
  wzięła się liczba.
- **Cenę przekreśloną zapisujesz do `list_price_claimed` i nigdzie indziej (Z3).**
  Nie wolno jej użyć do wyliczenia jakiejkolwiek obniżki. Jeśli źródło podaje
  cenę minimalną z 30 dni (Omnibus) — to osobne pole `omnibus_min_30d`.

### 2. Identyfikatory

- EAN: użyj `validate_gtin` z `models.py`. Kod z błędną sumą kontrolną →
  `ean = None` + wpis w `data_issues`. **Nigdy nie przepuszczaj niezwalidowanego
  EAN-u** — cały matcher stoi na założeniu, że EAN jest wiarygodny.
- MPN: wyciągnij z pola źródłowego albo z tytułu wzorcami per marka.
  `mpn_norm`: wielkie litery, usunięte `-`, `/`, spacje, obcięte sufiksy
  regionalne (`/EU`, `-EU`, `-PL`, `/A`).

### 3. Marki (`config/brands.yaml`)

- Słownik aliasów: `SAMSUNG`, `Samsung Electronics`, `samsung` → `samsung`.
- Wyciąganie marki z tytułu, gdy pole źródłowe puste — dopasowanie do słownika
  znanych marek, **tylko na granicy słowa** (żeby „Xiaomi" nie wpadło z frazy
  „do Xiaomi").
- Nieznana marka → `brand_norm = None`, nie zgadujesz.

### 4. Stan produktu i zestawy

To bezpośrednia obrona przed trybem porażki **F2** (porównanie poleasingu
z nowym albo dwupaku ze sztuką):

- `condition` z fraz w tytule/opisie: `poleasingowy`, `powystawowy`, `refurbished`,
  `odnowiony`, `outlet`, `uszkodzony`, `używany`, `open box`. Domyślnie `new`,
  ale **tylko gdy źródło deklaruje stan** — brak deklaracji przy źródle
  z drugiej ręki (Allegro) to `None`, nie `new`.
- `is_bundle` + `bundle_size` z fraz: `zestaw`, `2-pak`, `dwupak`, `x2`, `2 szt`,
  `komplet`. Zestaw z różnych produktów oznacz `bundle_size = None`, ale
  `is_bundle = True` — matcher go wtedy nie scali z niczym.

### 5. Kategorie (`config/category_map.yaml`)

- Własne drzewo kategorii (tabela `category`) + ręczna tabela mapowań
  `(source, kategoria źródłowa) → category_id`.
- To jest nudna, ręczna robota i tak ma być — deterministyczna i sprawdzalna.
  Zamapuj **na start tylko elektronikę** (dyski, monitory, laptopy, telefony,
  powerbanki, słuchawki, RTV) — reszta ląduje w `category_id = None`
  i przechodzi dalej bez oceny jakości. To jest poprawne zachowanie (Z4),
  nie brak.
- Raportuj listę niezamapowanych kategorii źródłowych posortowaną po liczbie
  ofert — to gotowa lista roboty na następny tydzień.

### 6. Atrybuty liczbowe (`normalize/attrs.py`)

Wyciąganie regexem + parser jednostek. To jest **paliwo dla bramki G2** matchera
(agent A3), więc precyzja jest ważniejsza od pokrycia:

- pojemność: `512GB`, `1TB`, `1 TB`, `2000GB` → znormalizowane do GB
- energia: `74Wh`, `20000mAh` (+ napięcie, jeśli podane) → Wh
- ekran: `27"`, `27 cali`, `165Hz`, `2560x1440`
- masa, długość, moc: `65W`, `100 W`, `1,5 m`
- **Wieloznaczność rozstrzygaj na niekorzyść ekstrakcji** — lepiej nie wyciągnąć
  atrybutu niż wyciągnąć zły. Zły atrybut zablokuje poprawne dopasowanie albo,
  gorzej, przepuści błędne.

Zapisuj do `product_attr` z polem `extracted_from` (`title` / `feed_field` /
`description`), żeby dało się mierzyć jakość ekstrakcji per źródło.

### 7. Normalizacja tytułu

`title_norm` do dopasowywania rozmytego (używa go A3): małe litery, usunięte
emoji i znaki ozdobne, usunięte frazy śmieciowe (`NOWY`, `GWARANCJA`, `FV 23%`,
`PROMOCJA`, `WYSYŁKA 24H`, `!!!`), zwinięte białe znaki, polskie znaki
**zachowane** (nie transliteruj — „słuchawki" i „sluchawki" rozjeżdżają TF-IDF).

## Definicja ukończenia

1. `dealradar normalize` przetwarza fixture'y z A1 i zapisuje `offer` bez błędów.
2. Raport przebiegu podaje: przetworzone, odrzucone (z rozbiciem na powody),
   `pct_with_ean`, `pct_with_brand`, `pct_with_category`, `pct_with_any_attr`.
3. Testy jednostkowe parsera cen na ≥ 20 realnych wariantach zapisu kwoty,
   w tym pułapkach: `"1.299,00"` vs `"1,299.00"`.
4. Testy wykrywania stanu i zestawów na ≥ 15 realnych tytułach z Allegro.
5. `pytest tests/normalize/` zielone — podaj liczbę.

## Czego NIE robisz

- Nie dopasowujesz ofert do produktów, nie tworzysz klastrów — to A3.
- Nie liczysz obniżek ani ocen — to A4/A5.
- Nie pobierasz niczego z sieci (poza kursami NBP, w osobnym module z cache'em).
- Nie dotykasz plików A0 ani katalogu `collectors/`.
- Nie wywołujesz żadnego modelu językowego. Ekstrakcja atrybutów jest regexowa
  i ma prawo się nie udać — od tego jest `None`.

## Raport końcowy

Podaj realne pokrycie per źródło: procent ofert z EAN-em, z marką, z kategorią,
z co najmniej jednym atrybutem liczbowym. Te cztery liczby decydują o tym,
co matcher (A3) w ogóle jest w stanie zrobić — jeśli któraś jest niska,
napisz to wprost zamiast maskować.

---

> **Zasady twarde:** ceny to `int` w groszach; brak danych to `None` + powód,
> nigdy `0`; każda funkcja publiczna ma typy i docstring; żadnych zależności
> spoza `pyproject.toml` bez uzasadnienia; **zero wywołań LLM-a**; testy
> odpalasz sam i podajesz wynik; w raporcie wypisz co pominąłeś i dlaczego.
