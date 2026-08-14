# Prompty dla agentów kodujących — DealRadar

Siedem promptów. Każdy jest samodzielny: agent, który go dostaje, nie widział
tej rozmowy. Każdy zaczyna od przeczytania `ARCHITEKTURA.md`.

## Kolejność i równoległość

```
A0 (fundament) ──────────► MUSI być pierwszy, sam, do końca
                            definiuje kontrakty, na których stoi reszta
        │
        ├──► A1 kolektory        ─┐
        ├──► A2 normalizator      │  te cztery mogą iść RÓWNOLEGLE
        ├──► A3 matcher           │  (rozłączne pliki, wspólne tylko modele z A0)
        └──► A4 historia + deal  ─┘
                    │
                    ├──► A5 ocena jakości   (potrzebuje deal_score z A4)
                    └──► A6 raporty + orkiestracja (potrzebuje wszystkiego)
```

**Nie uruchamiaj A1–A4 przed zakończeniem A0.** Konflikt w `models.py`
i `schema.sql` kosztuje więcej niż sekwencyjne czekanie.

## Podział plików (żeby agenci sobie nie nadpisywali)

| Agent | Wyłączna własność |
|---|---|
| A0 | `models.py`, `db.py`, `config.py`, `cli.py`, `migrations/`, `conftest.py` |
| A1 | `collectors/` |
| A2 | `normalize/` |
| A3 | `matching/` |
| A4 | `scoring/deal.py`, `scoring/history.py` |
| A5 | `scoring/quality.py`, `config/categories/` |
| A6 | `reporting/`, `pipeline.py` |

Agenci A1–A6 **nie modyfikują plików A0**. Jeśli któryś potrzebuje zmiany
w modelu — zgłasza to w raporcie końcowym zamiast edytować.

## Wspólna stopka do doklejenia do każdego promptu

> **Zasady twarde (obowiązują niezależnie od treści zadania):**
> 1. Ceny wyłącznie jako `int` w groszach. Żadnych `float` na pieniądzach.
> 2. Brak danych = `None` + powód, nigdy `0` ani wartość domyślna.
> 3. Każda funkcja publiczna ma typy i docstring z jednym zdaniem „co zwraca".
> 4. Nie dodawaj zależności spoza listy w `pyproject.toml` bez uzasadnienia
>    w raporcie końcowym.
> 5. Nie wywołuj żadnego modelu językowego. Rdzeń jest deterministyczny.
> 6. Testy odpalasz sam i podajesz wynik. „Powinno działać" nie jest wynikiem.
> 7. W raporcie końcowym wypisz: co zaimplementowałeś, co pominąłeś i dlaczego,
>    jakie założenia przyjąłeś, gdzie kontrakt z `ARCHITEKTURA.md` okazał się
>    niewystarczający.
