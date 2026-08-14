"""Wires concrete collectors into the collect pipeline stage (etap 1) with per-source failure isolation (Z7).

Resolves each configured source in `config/sources.yaml` to a concrete collector class, runs it to
completion, persists every yielded `RawOffer` via `db.insert_raw_offer`, and folds per-source counters into
the stage's `RunReport`. Called by the CLI's `collect` command (see the minimal wiring change in `cli.py`)
and directly by tests.

Failure isolation (Z7): one source's exception is caught here and never propagates to the caller -- the
overall `collect` run continues with the next source. A source is also marked failed, not merely empty, when
it returns zero records in a run but has non-empty history from earlier runs (F5: a feed silently changed
format).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db
from dealradar.config import AppConfig, SourceConfig
from dealradar.models import RawOffer, RunReport, Source

from . import BaseCollector
from .aliexpress import AliexpressCollector
from .allegro_api import AllegroApiCollector
from .amazon_pa_api import AmazonPaApiCollector
from .feed_xml import FeedXmlCollector
from .ibood import IboodCollector
from .pepper_rss import PepperRssCollector

logger = logging.getLogger("dealradar.collectors.runner")

# Sources with source-specific collector logic. Any other source configured with kind="feed" falls back to
# FeedXmlCollector (the generic affiliate-feed parser) -- that fallback is what lets one collector serve
# dozens of shops through config alone (ARCHITEKTURA.md section 6).
_EXPLICIT_REGISTRY: dict[str, type[BaseCollector]] = {
    "ibood": IboodCollector,
    "pepper": PepperRssCollector,
    "allegro": AllegroApiCollector,
    "aliexpress": AliexpressCollector,
    "amazon": AmazonPaApiCollector,
}


def resolve_collector_class(entry: SourceConfig) -> type[BaseCollector] | None:
    """Returns the collector class registered for entry's name, or FeedXmlCollector for any other feed-kind source."""
    explicit = _EXPLICIT_REGISTRY.get(entry.name.strip().lower())
    if explicit is not None:
        return explicit
    if entry.kind == "feed":
        return FeedXmlCollector
    return None


def build_collector(
    entry: SourceConfig, source_id: int, *, cache_dir: Path | None, offline: bool
) -> BaseCollector:
    """Instantiates the collector class for entry, wiring its config extras through as constructor kwargs."""
    cls = resolve_collector_class(entry)
    if cls is None:
        raise ValueError(f"no collector registered for source {entry.name!r} (kind={entry.kind!r})")
    rps = float(getattr(entry, "rps", None) or 1.0)
    common: dict[str, Any] = {"source_id": source_id, "rps": rps, "cache_dir": cache_dir, "offline": offline}
    # Per-source User-Agent override. Allegro *requires* a UA in their own generated format and blocks the
    # API key outright when it is missing or malformed (see config/sources.yaml), so this cannot stay on
    # BaseCollector's generic default for every source.
    user_agent = getattr(entry, "user_agent", None)
    if user_agent:
        common["user_agent"] = str(user_agent)

    if cls is FeedXmlCollector:
        return FeedXmlCollector(
            url=entry.base_url,
            source_name=entry.name,
            feed_format=getattr(entry, "feed_format", None),
            record_tag=getattr(entry, "record_tag", None),
            id_field=getattr(entry, "id_field", None),
            encoding=getattr(entry, "encoding", None),
            delimiter=getattr(entry, "delimiter", None),
            **common,
        )
    if cls is PepperRssCollector:
        return PepperRssCollector(url=entry.base_url, source_name=entry.name, **common)
    if cls is IboodCollector:
        return IboodCollector(url=entry.base_url, source_name=entry.name, **common)
    if cls is AllegroApiCollector:
        return AllegroApiCollector(
            source_name=entry.name,
            queries=getattr(entry, "queries", None),
            category_ids=getattr(entry, "category_ids", None),
            **common,
        )
    if cls is AliexpressCollector:
        return AliexpressCollector(
            source_name=entry.name,
            keywords=getattr(entry, "keywords", None),
            category_ids=getattr(entry, "category_ids", None),
            **common,
        )
    if cls is AmazonPaApiCollector:
        kwargs: dict[str, Any] = {
            "source_name": entry.name,
            "keywords": getattr(entry, "keywords", None),
            **common,
        }
        for field in ("search_index", "marketplace", "host", "region", "resources", "page_size", "max_pages"):
            value = getattr(entry, field, None)
            if value is not None:
                kwargs[field] = value
        return AmazonPaApiCollector(**kwargs)
    raise AssertionError(f"unhandled collector class {cls!r}")  # pragma: no cover -- exhaustive above


def _raw_offer_is_new(session: Session, raw: RawOffer) -> bool:
    """Returns True iff no raw_offer row already shares raw's (source_id, content_hash) -- i.e. this is not a dupe."""
    existing = session.execute(
        text("SELECT id FROM raw_offer WHERE source_id = :source_id AND content_hash = :content_hash"),
        {"source_id": raw.source_id, "content_hash": raw.content_hash},
    ).scalar_one_or_none()
    return existing is None


def _prior_raw_offer_count(session: Session, source_id: int) -> int:
    """Returns how many raw_offer rows already exist for source_id -- the baseline used to detect F5."""
    return int(
        session.execute(
            text("SELECT COUNT(*) FROM raw_offer WHERE source_id = :source_id"), {"source_id": source_id}
        ).scalar_one()
    )


def run_collect(
    session: Session,
    cfg: AppConfig,
    report: RunReport,
    *,
    source_name: str | None = None,
    since: datetime | None = None,
    cache_dir: Path | None = None,
    offline: bool = False,
) -> RunReport:
    """Runs collect for `source_name` (or every enabled+registered source), isolating failures per source (Z7).

    Mutates and returns `report`: `offers_fetched`/`offers_new` accumulate across sources, `sources_failed`
    lists every source whose collector raised or that hit the F5 silent-failure pattern (0 records with
    non-empty history), and one summary note per source is appended in "source, fetched=N, new=N, errors=N,
    duration_s=S" form, per the task spec.
    """
    if source_name is not None:
        entry = cfg.sources.get(source_name)
        if entry is None:
            report.status = "failed"
            report.sources_failed = [source_name]
            report.notes.append(f"unknown source {source_name!r}: not present in config/sources.yaml")
            return report
        entries = [entry]
    else:
        entries = [e for e in cfg.sources.sources if e.enabled]
        if not entries:
            report.notes.append("no enabled sources in config/sources.yaml")

    for entry in entries:
        _run_one_source(session, entry, report, since=since, cache_dir=cache_dir, offline=offline)

    return report


def _run_one_source(
    session: Session,
    entry: SourceConfig,
    report: RunReport,
    *,
    since: datetime | None,
    cache_dir: Path | None,
    offline: bool,
) -> None:
    """Runs one source's collector to completion, isolating any exception so it cannot abort the overall run."""
    started = time.monotonic()
    fetched = 0
    new = 0
    errors = 0
    status = "ok"
    error_note: str | None = None

    source_id = db.upsert_source(
        session,
        Source(
            name=entry.name, kind=entry.kind, base_url=entry.base_url, enabled=entry.enabled, risk_prior=entry.risk_prior
        ),
    )
    session.commit()
    prior_count = _prior_raw_offer_count(session, source_id)
    source_cache_dir = (cache_dir / entry.name) if cache_dir is not None else None

    collector: BaseCollector | None = None
    try:
        collector = build_collector(entry, source_id, cache_dir=source_cache_dir, offline=offline)
        for raw in collector.fetch(since):
            fetched += 1
            is_new = _raw_offer_is_new(session, raw)
            db.insert_raw_offer(session, raw)
            if is_new:
                new += 1
    except Exception as exc:  # noqa: BLE001 -- Z7: one source's failure must never abort the others
        errors += 1
        status = "failed"
        error_note = f"{type(exc).__name__}: {exc}"
        logger.exception("source=%s collector failed", entry.name)
        session.rollback()
    else:
        if fetched == 0 and prior_count > 0:
            status = "failed"
            error_note = (
                f"F5: returned 0 records this run but {prior_count} exist from earlier runs "
                "(possible feed format change)"
            )
        session.commit()
    finally:
        if collector is not None:
            collector.close()

    duration_s = time.monotonic() - started
    report.offers_fetched += fetched
    report.offers_new += new
    if status == "failed":
        report.sources_failed = [*report.sources_failed, entry.name]
    note = f"{entry.name}, fetched={fetched}, new={new}, errors={errors}, duration_s={duration_s:.2f}"
    if error_note:
        note += f" -- {error_note}"
    report.notes.append(note)
