# DealRadar — architektura

Wersja 0.1 (projekt do oceny) · 2026-08-05

Narzędzie do wykrywania **prawdziwych** promocji w sklepach i serwisach (Allegro,
AliExpress, Amazon, x-kom, MediaExpert, Erli, Pepper, iBood, …), z oceną
stosunku jakość/cena względem konkurencyjnych modeli w tej samej półce cenowej.

Rdzeń jest **w pełni deterministyczny — bez LLM-a**. Sekcja 10 opisuje dokładnie,
co to kosztuje i gdzie ewentualny model dopina się później jako wtyczka.

---

## 1. Cel i zasada naczelna

> **Narzędzie ma prawo powiedzieć: „to nie jest promocja".**
> I ma prawo powiedzieć: „nie wiem, za mało danych".

Agregatorów okazji są dziesiątki i wszystkie mają tę samą wadę: powtarzają
liczbę, którą podał sklep. Sklep mówi „−60%", agregator pokazuje „−60%".
Wartość tego narzędzia zaczyna się w momencie, w którym potrafi powiedzieć:
*„sklep twierdzi −60%, ale ten produkt kosztował tyle samo przez ostatnie
70 dni, realna obniżka to −4%"*.

Z tego wynika wszystko poniżej: własna historia cen jest jedynym źródłem prawdy,
każda ocena ma dowody, a brak danych nigdy nie jest zamieniany na zero.

---

## 2. Pięć trybów porażki, które ta architektura ma blokować

| # | Tryb porażki | Kiedy boli | Obrona w architekturze |
|---|---|---|---|
| **F1** | **Fałszywa obniżka** — sklep podbija cenę na tydzień, potem „przecenia" | Przy zakupie: płacisz cenę normalną w poczuciu okazji | Własny `price_point` (append-only) + zakaz użycia ceny przekreślonej ze sklepu (Z3) |
| **F2** | **Złe dopasowanie produktu** — porównujesz 512 GB z 1 TB, poleasing z nowym, sztukę z dwupakiem | Nigdy nie wychodzi na jaw — dostajesz „okazję życia", która nie istnieje | Bramka atrybutowa **G2** + rozdział klas stanu + próg precyzji 0,97 |
| **F3** | **Cena niepełna** — bez dostawy, bez cła/VAT z Chin, z kuponem którego nie masz | Przy kasie | `price_total` jako jedyna cena w rankingu + flaga `price_incomplete` |
| **F4** | **Ocena z powietrza** — 5,0 z trzech opinii, opinie kupione, ocena tylko z jednego sklepu | Po miesiącu używania | Ściąganie bayesowskie + zgodność międzysklepowa + bramka pokrycia **G3** |
| **F5** | **Cicha śmierć źródła** — feed zmienił format, kolektor zwraca 0 rekordów, raport pokazuje „brak okazji" i wygląda normalnie | Tygodniami — nie zauważasz, że połowa sklepów zniknęła | Metryki pokrycia per przebieg + alarm na spadek > 30% (Z7) |

Jeśli implementacja nie broni przed którymś z tych pięciu, nie ma znaczenia,
jak ładny ma interfejs.

---

## 3. Siedem zasad projektowych

**Z1. Surowe dane są nietykalne, wszystko pochodne jest przeliczalne.**
`raw_offer` to append-only JSON dokładnie taki, jaki przyszedł ze źródła.
Każdy kolejny etap (normalizacja, dopasowanie, scoring) da się odtworzyć
od zera komendą `dealradar rebuild --from raw`. Algorytm scoringu zmienisz
dziesięć razy — musisz móc przeliczyć całą historię pod nowy algorytm.

**Z2. Cena to liczba całkowita w groszach i zawsze cena całkowita.**
Żadnych floatów. `price_total = price_gross + shipping_cost`. Jeśli koszt
dostawy jest nieznany, rekord dostaje `price_incomplete = true` i **nie wchodzi
do rankingu**, tylko do historii.

**Z3. Cena „przed obniżką" podana przez sklep nie jest danymi o cenie.**
Zapisujemy ją wyłącznie po to, żeby liczyć rozjazd między obniżką deklarowaną
a rzeczywistą. Ten rozjazd, uśredniony po sklepie, to **wskaźnik ściemy** —
osobna, użyteczna metryka, której nie daje żadna porównywarka.

**Z4. Brak danych to brak, nie zero.**
Produkt bez formuły użyteczności w swojej kategorii nie dostaje oceny 1–10.
Dostaje `null` i powód. Ocena wymyślona jest gorsza niż jej brak, bo wygląda
tak samo jak prawdziwa.

**Z5. Fałszywe scalenie jest gorsze niż brak scalenia.**
Jedno błędne połączenie dwóch różnych produktów zatruwa medianę, a mediana jest
podstawą wszystkich obniżek. Dopasowanie optymalizujemy pod **precyzję**
(cel: ≥ 0,97), nie pod kompletność. Niedopasowana oferta to strata; źle
dopasowana to kłamstwo.

**Z6. Każda liczba niesie dowody.**
`deal_score` i `quality_score` zapisują `evidence_json`: z jakich ofert,
z ilu dni, wobec jakich konkurentów. Liczba bez dowodów jest niefalsyfikowalna,
czyli bezużyteczna przy debugowaniu.

**Z7. Awaria jednego źródła nie zatrzymuje przebiegu, ale musi być widoczna.**
Kolektory są izolowane. Każdy przebieg kończy się raportem: ile ofert, jaki
procent z EAN-em, jaki procent dopasowany, ile nowych produktów. Spadek
któregokolwiek wskaźnika o > 30% dzień do dnia to alarm, nie ciekawostka.

---

## 4. Pipeline

Sześć etapów połączonych bazą danych, każdy uruchamialny osobno i idempotentny.

```
 ŹRÓDŁA          ETAPY                        TABELE
 ───────         ─────                        ──────
 Allegro API ─┐
 feed XML    ─┤
 Pepper RSS  ─┼─► 1. collect   ──────────────► raw_offer      (append-only)
 iBood       ─┤      surowy JSON, zero logiki
 AliExpress  ─┘
                  2. normalize ──────────────► offer          (kanoniczny rekord)
                       ceny w groszach, EAN z sumą kontrolną,
                       marka z aliasów, stan, zestawy

                  3. match     ──────────────► product        (klaster ofert)
                       EAN → MPN → blocking + fuzzy + bramka G2

                  4. snapshot  ──────────────► price_point    (append-only, 1/dzień)

                  5. score     ──────────────► deal, quality_score
                       statystyka odporna (mediana + MAD),
                       grupa porównawcza, rubryka 1–10

                  6. report    ──────────────► digest / alerty / UI
```

Etap 1 jest jedynym, który dotyka sieci. Etapy 2–5 są czystymi funkcjami bazy —
da się je uruchomić offline, w testach, na danych sprzed miesiąca.

---

## 5. Model danych

```sql
source(id, name, kind, base_url, enabled, risk_prior)
    -- kind: api | feed | rss
    -- risk_prior: 0.0 (x-kom) … 0.4 (AliExpress) — wchodzi do oceny ryzyka

raw_offer(id, source_id, external_id, fetched_at, payload_json, content_hash)
    -- content_hash pozwala pominąć identyczny rekord przy ponownym pobraniu

offer(id, source_id, external_id, first_seen, last_seen,
      title, title_norm, brand, brand_norm, mpn, mpn_norm, ean,
      category_raw, category_id,
      url, seller, seller_rating, condition, is_bundle, bundle_size,
      price_gross, shipping_cost, price_total, currency, price_incomplete,
      list_price_claimed,          -- Z3: tylko do wskaźnika ściemy
      omnibus_min_30d,             -- jeśli sklep publikuje
      rating_avg, rating_count, warranty_months, in_stock,
      product_id, match_confidence, match_method)

price_point(offer_id, observed_at, price_total, in_stock)
    -- append-only, indeks (offer_id, observed_at); serce systemu

product(id, canonical_title, brand, mpn, ean, category_id, created_at)
product_attr(product_id, key, value_num, value_text, unit, extracted_from)
    -- capacity_gb=512, refresh_hz=165, capacity_wh=74 …

category(id, parent_id, name, path)     -- własne drzewo + mapowanie ze źródeł

deal(id, offer_id, computed_at, basis, deal_score,
     depth_vs_median_90d, z_robust, price_rank_today, is_alltime_low,
     n_competing_offers, n_days_history, coverage_cap, evidence_json)

quality_score(product_id, computed_at, peer_group_id,
     value_score, reliability_score, deal_strength, risk_score,
     total_1_10, confidence, missing_reason, evidence_json)

shop_credibility(source_id, period, claimed_discount_avg, real_discount_avg,
                 bullshit_index, n_offers)

match_review(offer_a, offer_b, combined_score, method, label, labeled_at)
    -- ręcznie oznaczone pary: zbiór ewaluacyjny dla matchera

watch(id, product_id_or_query, max_price, min_deal_score, channel, active)
```

---

## 6. Warstwa źródeł

Jeden interfejs, wiele adapterów. Adapter **nie parsuje** — zwraca surowy ładunek.

```python
class Collector(Protocol):
    source_name: str
    kind: Literal["api", "feed", "rss"]
    def fetch(self, since: datetime | None) -> Iterator[RawOffer]: ...
```

Limitowanie zapytań, ponawianie, cache na dysku i obsługa stronicowania siedzą
w klasie bazowej — nie w każdym adapterze osobno.

| Źródło | Droga | Uwagi |
|---|---|---|
| Allegro | oficjalne REST API, OAuth2 (client credentials) | limity zapytań; oferty często mają EAN w parametrach |
| Sklepy PL (x-kom, MediaExpert, Morele, Erli…) | **feedy produktowe z sieci afiliacyjnych** (XML/CSV) | jeden parser obsługuje kilkadziesiąt sklepów; feedy zwykle mają EAN — to jest główny mnożnik projektu |
| Pepper | RSS gorących okazji | ludzie już przefiltrowali; świetny „szybki tor" |
| iBood | feed dzienny | mały wolumen, trywialny |
| AliExpress | API partnerskie (wymaga akceptacji konta) | brak EAN-ów, produkty no-name — patrz ograniczenie w §7 |
| Amazon | PA-API 5.0 (wymaga konta Associates) | dobre dane: ASIN, ocena, liczba opinii |
| Ceneo, Temu | brak legalnej ścieżki publicznej | **pomijamy** — obchodzenia zabezpieczeń anty-botowych nie budujemy |

Praktycznie: rejestracja w 2–3 sieciach afiliacyjnych daje kilkadziesiąt sklepów
naraz, legalnie, w formacie strukturalnym z EAN-em. To zamienia problem
scrapingu w problem parsowania XML-a.

---

## 7. Dopasowanie produktów (matcher) — rdzeń całości

Kaskada. Każdy krok nadaje `match_method` i `match_confidence`.

**Krok 1 — EAN dokładny (0,99).** Walidacja: 8/13 cyfr + suma kontrolna.
Zabezpieczenie: jeśli dwie oferty mają ten sam EAN, a podobieństwo tytułów
< 0,30 → konflikt do `match_review`, nie scalamy. Błędne EAN-y w feedach
zdarzają się notorycznie.

**Krok 2 — marka + znormalizowany MPN (0,95).** Normalizacja: wielkie litery,
usunięcie `-`, `/`, spacji, obcięcie sufiksów regionalnych (`/EU`, `-EU`, `PL`).

**Krok 3 — blocking + podobieństwo rozmyte (0,60–0,85).**
Kandydaci ograniczeni przez klucz blokujący `(brand_norm, category_id, przedział
ceny ±35%)` — bez tego masz porównanie N².
Miara łączona:
- TF-IDF na n-gramach znakowych 3–5, cosinus (scikit-learn)
- `token_set_ratio` (RapidFuzz)
- **bramka G2 (atrybutowa)** — atrybuty liczbowe wyciągnięte regexem nie mogą
  się sprzeczać. `512GB` vs `1TB` → odrzut niezależnie od podobieństwa tekstu.
  To jest jedyny powód, dla którego fuzzy matching w ogóle nadaje się do użytku.

**Bramki twarde (nigdy nie scalamy ponad nimi):**
- różna klasa stanu (`new` / `refurb` / `used`)
- różny `bundle_size`
- sprzeczny atrybut liczbowy (G2)

**Klastrowanie:** union-find po zaakceptowanych parach, w obrębie klasy stanu.

**Ewaluacja — obowiązkowa.** 200 ręcznie oznaczonych par w `match_review`,
raport precyzja/kompletność przy każdej zmianie progów. Bez zbioru
ewaluacyjnego strojenie progów to zgadywanie (Z5).

> **Ograniczenie, które trzeba powiedzieć wprost:** dla AliExpress i podobnych
> „to samo gdzie indziej" zwykle nie istnieje — to towar bez marki, bez EAN-u,
> pod dziesięcioma nazwami. Te oferty zostają jako samotne produkty i dostają
> ocenę tylko na podstawie własnej historii ceny.

---

## 8. Siła promocji (`deal_score`) — czysta statystyka

Dla oferty O produktu P w chwili t:

```
H = price_point produktu P, ostatnie 90 dni
    filtry: condition=new, in_stock, match_confidence ≥ 0.90

med   = median(H)
mad   = median(|h − med|)
sigma = max(1.4826 · mad, 0.01 · med)      # podłoga chroni przed ceną stałą

z     = (med − price_total) / sigma        # ile odchyleń poniżej normy
depth = 1 − price_total / med              # głębokość obniżki
```

Mediana i MAD, **nie** średnia i odchylenie standardowe — jedna wyprzedaż
resztek psuje średnią na kwartał.

```
s_z     = clamp(z / 4, 0, 1)
s_depth = clamp(depth / 0.35, 0, 1)
s_rank  = 1.0 jeśli najtańsza dziś | 0.5 jeśli w top-3 | 0.0
s_atl   = 1.0 jeśli cena ≤ min(H)

raw = 0.35·s_z + 0.30·s_depth + 0.20·s_rank + 0.15·s_atl
```

**Bramka pokrycia G1** — sufit, nie kara:

| Warunek | Sufit |
|---|---|
| historia < 14 dni | 0,50 |
| historia < 30 dni | 0,70 |
| < 3 konkurencyjne oferty | 0,60 |
| < 2 źródła | 0,80 |

`deal_score = 100 · min(raw, min(sufity))`

**Zimny start** (`basis = "cross_shop"`): brak historii → `med` zastąpione
medianą dzisiejszych cen wszystkich ofert P, `s_z` wyzerowane, sufit 0,70.
Działa od pierwszego dnia, o ile masz pokrycie w wielu sklepach.

**Wskaźnik ściemy** (osobno, na sklep): `bullshit_index =
mean(claimed_discount − real_discount)`. Sklep, który regularnie deklaruje −60%
przy realnych −5%, dostaje etykietę widoczną w raporcie.

---

## 9. Ocena jakość/cena 1–10 — rubryka deterministyczna

**Grupa porównawcza.** Ta sama kategoria liściowa, cena w przedziale ±25% wokół
mediany ceny P, dostępne. Minimum 5 członków — jeśli mniej, rozszerzaj pasmo
co 5 pp do ±60%. Dalej mniej niż 5 → `confidence = "low"`.

**Cztery składowe, każda jako percentyl w grupie porównawczej:**

1. **Niezawodność** — ocena ściągnięta bayesowsko:
   `r* = (C·m + Σr) / (C + n)`, gdzie `m` = mediana ocen w kategorii, `C = 30`.
   5,0 z trzech opinii ląduje niżej niż 4,6 z dwóch tysięcy — i o to chodzi.
   Modyfikator zgodności: oceny z ≥ 2 źródeł zgodne w granicach 0,4 → premia;
   rozbieżne → karany jest wyższy odczyt (kupione opinie są lokalne dla sklepu).

2. **Wartość** — użyteczność na złotówkę, z **formuły kategorii** w YAML-u:
   ```yaml
   dyski_ssd:      utility: capacity_tb * (1 + 0.3 * (read_mbs / 3500))
   monitory:       utility: 0.4*norm(diagonal_in) + 0.4*norm(refresh_hz) + 0.2*norm(pixels)
   powerbanki:     utility: capacity_wh
   ```
   ~20 linii na kategorię. To jest uczciwa praca domenowa i **jedyna część,
   której nie da się obejść** (patrz Z4 i §10).

3. **Siła promocji** — `deal_score / 100` z §8.

4. **Ryzyko** (odwrócone) — gwarancja w miesiącach, polityka zwrotów, stan,
   ocena sprzedawcy, `risk_prior` źródła.

```
total_1_10 = 1 + 9 · Σ(waga_i · percentyl_i)
```
Wagi per kategoria w konfiguracji. `evidence_json` zapisuje listę konkurentów
i percentyle — inaczej nie da się sprawdzić, czemu coś dostało 7,4.

**Bramka G3.** Brak formuły użyteczności dla kategorii **albo** < 5 konkurentów
→ zwracamy `null` + `missing_reason`, nie liczbę (Z4).

---

## 10. Bez LLM-a — co działa, a co tracisz

Rdzeń opisany wyżej nie zawiera ani jednego wywołania modelu. Wszystko to
statystyka odporna, dopasowywanie ciągów i tabele konfiguracyjne.

**Co dostajesz w zamian:** zerowy koszt operacyjny, pełną odtwarzalność
(ten sam wsad zawsze daje tę samą liczbę), testowalność, brak limitów zapytań,
brak halucynacji i możliwość przeliczenia trzech lat historii w minutę.
Dla zadania, które nocą przemiela dziesiątki tysięcy ofert, determinizm jest
domyślnie właściwym wyborem — nie kompromisem.

**Co realnie tracisz — pięć rzeczy, wprost:**

| # | Strata | Zastępnik deterministyczny | Ile kosztuje |
|---|---|---|---|
| 1 | Wyciąganie specyfikacji z zaśmieconych tytułów (Allegro, AliExpress: „🔥 NOWY Dysk SSD 1TB M.2 GW24 FV23") | regex + parser jednostek + wzorce per kategoria | pokrycie ~85% w feedach afiliacyjnych, ~55% na Allegro |
| 2 | Ujednolicanie kategorii między źródłami | ręczna tabela mapowań, kilkaset linii | nudne, ale jednorazowe i deterministyczne |
| 3 | Rozstrzyganie granicznych dopasowań | próg + kolejka `match_review` do ręcznego oznaczenia | ~15 min tygodniowo na przeglądanie |
| 4 | Streszczenie opinii i typowych awarii modelu | **brak zastępnika** | dostajesz ocenę bez uzasadnienia jakościowego |
| 5 | Nowa kategoria działa od razu | musisz napisać formułę użyteczności | ~30 min na kategorię |

Straty 1–3 są do nadrobienia pracą. Strata 4 jest realna i nie ma obejścia.
Strata 5 jest cechą, nie błędem — wymusza jawne powiedzenie, co system potrafi
ocenić, zamiast udawania kompetencji wszędzie (Z4).

**Miejsce na model, jeśli kiedyś zechcesz.** Jeden interfejs, zero zmian
w rdzeniu:

```python
class Enricher(Protocol):
    def extract_attrs(self, offer: Offer) -> dict[str, Attr]: ...
    def adjudicate(self, a: Offer, b: Offer) -> MatchVerdict: ...
    def summarize(self, product: Product, reviews: list[str]) -> str: ...
```

Domyślna implementacja to `NullEnricher` (zwraca puste). Model wchodzi wyłącznie
tam, gdzie regex zawiódł, i **nigdy nie liczy oceny** — może co najwyżej
wypełnić brakujący atrybut, który potem trafia do tej samej rubryki.
Liczby zostają po stronie kodu.

---

## 11. Eksploatacja

**Harmonogram.**
- Nocny pełny przebieg: collect → normalize → match → snapshot → score → digest.
- Godzinowy szybki tor: tylko Pepper RSS + produkty z `watch`.

**Idempotencja.** Każdy etap kluczowany `(source, external_id, run_date)`.
Ponowne uruchomienie jest bezpieczne. `dealradar rebuild --from raw --since 2026-01-01`
przelicza wszystko pochodne pod nowy algorytm.

**Raport przebiegu** (Z7) — do logu i do digestu:
```
offers_fetched, offers_new, pct_with_ean, pct_matched,
products_new, deals_scored, quality_scored, sources_failed
```
Spadek `pct_matched` lub `offers_fetched` o > 30% d/d → alarm.

**Stos.** Python 3.12 · `httpx` (async) · `pydantic` v2 · SQLModel/SQLAlchemy ·
SQLite (→ Postgres, gdy urośnie) · `rapidfuzz` · `scikit-learn` (TF-IDF) ·
`lxml` · `typer` (CLI) · `pytest` + nagrane odpowiedzi HTTP jako fixture'y.
Harmonogram: Harmonogram zadań Windows albo `apscheduler`.
Interfejs: najpierw CLI i digest HTML/mail. Web dopiero, gdy dane mają sens.

---

## 12. Kolejność budowy

| Etap | Zawartość | Odpowiada na pytanie |
|---|---|---|
| **MVP** | Pepper + iBood + Allegro + jeden feed afiliacyjny, dopasowanie tylko po EAN, ranking „vs najtańsza dziś" | „która promocja jest prawdziwa **dziś**" |
| **v2** | `price_point` + `deal_score` na historii, matcher rozmyty, wskaźnik ściemy | „czy ta cena była kiedykolwiek niższa" |
| **v3** | Formuły użyteczności, grupy porównawcze, ocena 1–10, digest i watchlista | „czy to dobry produkt za te pieniądze" |

Historia cen zaczyna być wartościowa po ~30 dniach i to jest jedyny zasób
w tym projekcie, którego nie da się kupić ani nadrobić później. **Snapshot cen
uruchom pierwszego dnia, nawet zanim reszta systemu będzie działać.**
