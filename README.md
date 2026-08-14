# DealRadar

Deterministic (no-LLM) detector of **real** price discounts, with a value-for-money score, for Polish
online stores. See `ARCHITEKTURA.md` for the full design rationale.

The point: dozens of deal aggregators exist and all of them repeat the number the shop gave them. The shop
says "−60%", the aggregator shows "−60%". DealRadar keeps its own price history, so it can say instead:
*"the shop claims −60%, but this product cost the same for the last 70 days — the real cut is −4%."*

Everything in the core is deterministic: robust statistics, string matching and config tables. There is not
a single language-model call anywhere in the pipeline, by design — same input always produces the same
number, and three years of history can be re-scored in a minute.

## Pipeline

Six stages, each its own CLI command, each idempotent and re-runnable:

```
collect    → raw_offer      untouched JSON exactly as the source returned it (append-only)
normalize  → offer          prices in grosze, checksum-validated EANs, brands, condition, bundles, attrs
match      → product        EAN → brand+MPN → fuzzy cascade, guarded by a numeric-attribute gate
snapshot   → price_point    one price observation per offer per day (append-only) — the actual moat
score      → deal, quality_score
report     → text / HTML digest, watchlist alerts
```

`dealradar run` chains all of it; `dealradar rebuild --from raw` re-derives everything downstream from the
untouched raw data, which is what makes changing the scoring algorithm safe.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
dealradar db migrate
```

Copy `.env.example` to `.env` and fill in whatever credentials you have. Secrets live **only** in the
environment, never in `config/*.yaml`. Every source without credentials disables itself gracefully and says
so in the log — it never fails the run.

## Usage

```bash
dealradar run                                   # full nightly pipeline
dealradar collect --source allegro              # one stage, one source
dealradar snapshot                              # daily price observation — run this from day one
dealradar score --deals --quality
dealradar report --format html --out digest.html
dealradar match --eval                          # matcher precision/recall on labeled pairs
dealradar watch add --query "SSD 2TB" --max-price 60000   # max_price is in grosze
```

Every command prints a one-line run report and writes it to `run_log`, so a source that silently starts
returning zero records shows up as an alarm instead of an empty report that looks normal.

## Sources

| Source | Access | Status |
|---|---|---|
| Allegro | official REST API, OAuth2 client credentials | needs `ALLEGRO_CLIENT_ID`/`SECRET` + a User-Agent in Allegro's required format (`config/sources.yaml`) |
| Pepper.pl | public RSS | works, no credentials needed |
| iBood | daily feed | works, no credentials needed |
| Affiliate feeds | generic XML/CSV parser (`feed_xml.py`) | works, but needs a publisher account at an affiliate network |
| Amazon | PA-API 5.0 | implemented, but requires an approved Associates account (qualifying sales) |
| AliExpress | affiliate API | implemented, requires affiliate account approval |
| Ceneo, Temu | — | deliberately not implemented: no legal public path |

Only official APIs, affiliate feeds and public RSS. No scraping, no anti-bot circumvention, `robots.txt`
and `Retry-After` respected.

## Development

```bash
pytest                              # 451 tests
mypy src/dealradar --strict         # 52 files, clean
```

## Layout

```
src/dealradar/
  models.py        canonical pydantic v2 models — money is always int grosze, missing data is None + a reason
  db.py            engine/session, migration runner, upsert_offer / append_price_point / run_report
  config.py        YAML + env loading, validated at startup
  cli.py           typer commands
  collectors/      Collector protocol, BaseCollector (rate limit, retry, disk cache) + one file per source
  normalize/       raw_offer → offer: prices, identifiers, brands, condition, categories, attribute regexes
  matching/        offer → product clusters: the cascade, hard gates, union-find, evaluation harness
  scoring/         history.py + deal.py (deal_score), quality.py (1–10 rubric, safe AST formula evaluator)
  reporting/       digest, health alarms, watchlist, notifiers
  pipeline.py      stage orchestration, failure isolation, run lock
config/
  sources.yaml     source registry
  brands.yaml      brand aliases
  category_map.yaml, normalizers/, categories/, scoring.yaml, matching.yaml
```

## Design rules that are not negotiable

- **Money is `int` grosze.** No floats on prices, anywhere.
- **Missing data is `None` plus a reason** — never `0`, never a default that looks like real data. A made-up
  score is worse than no score, because it looks exactly like a real one.
- **The shop's own "before" price is never used to compute a discount.** It is recorded only to measure the
  gap between claimed and real discounts, per shop — the "bullshit index".
- **A false merge is worse than no merge.** The matcher optimizes precision (target ≥ 0.97), not recall.
- **Every number carries evidence.** `deal.evidence` / `quality_score.evidence` record what the number was
  computed from, so any figure in a report can be checked by hand.
