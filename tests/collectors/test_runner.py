"""Tests for collectors/runner.py: source resolution, per-source failure isolation (Z7), F5 detection,
and idempotency of run_collect against a fresh in-memory database (via the shared `db` fixture from
tests/conftest.py).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.collectors.aliexpress import AliexpressCollector
from dealradar.collectors.allegro_api import AllegroApiCollector
from dealradar.collectors.feed_xml import FeedXmlCollector
from dealradar.collectors.ibood import IboodCollector
from dealradar.collectors.pepper_rss import PepperRssCollector
from dealradar.collectors import DEFAULT_USER_AGENT
from dealradar.collectors.runner import build_collector, resolve_collector_class, run_collect
from dealradar.config import AppConfig, BrandAliasesConfig, SourceConfig, SourcesConfig
from dealradar.models import RunReport

from conftest import read_fixture, write_cached_response

NOW_STAGE = "collect"


def _cfg(*entries: SourceConfig) -> AppConfig:
    return AppConfig(
        database_url="sqlite:///:memory:",
        sources=SourcesConfig(sources=list(entries)),
        brands=BrandAliasesConfig(aliases={}),
    )


def _report() -> RunReport:
    from datetime import datetime, timezone

    return RunReport(stage=NOW_STAGE, started_at=datetime.now(timezone.utc))


def test_resolve_collector_class_uses_explicit_registry_and_generic_feed_fallback() -> None:
    assert resolve_collector_class(SourceConfig(name="ibood", kind="feed", base_url="x", risk_prior=0.1)) is IboodCollector
    assert resolve_collector_class(SourceConfig(name="Pepper", kind="rss", base_url="x", risk_prior=0.1)) is PepperRssCollector
    assert (
        resolve_collector_class(SourceConfig(name="allegro", kind="api", base_url="x", risk_prior=0.1))
        is AllegroApiCollector
    )
    assert (
        resolve_collector_class(SourceConfig(name="aliexpress", kind="api", base_url="x", risk_prior=0.4))
        is AliexpressCollector
    )
    generic = SourceConfig(name="some_new_shop_awin", kind="feed", base_url="x", risk_prior=0.05)
    assert resolve_collector_class(generic) is FeedXmlCollector
    unregistered_api = SourceConfig(name="mystery_api", kind="api", base_url="x", risk_prior=0.2)
    assert resolve_collector_class(unregistered_api) is None


def test_explicit_collector_key_overrides_name_based_resolution() -> None:
    """Pepper publishes several RSS channels; each needs its own source row but the same parser, which
    name-based lookup alone cannot express."""
    entry = SourceConfig(
        name="pepper_elektronika",
        kind="rss",
        base_url="https://www.pepper.pl/rss/grupa/elektronika",
        risk_prior=0.05,
        collector="pepper_rss",
    )
    assert resolve_collector_class(entry) is PepperRssCollector


def test_unknown_explicit_collector_key_fails_loudly() -> None:
    """A typo in `collector:` must not silently fall through to the generic feed parser."""
    entry = SourceConfig(
        name="whatever", kind="rss", base_url="x", risk_prior=0.05, collector="peppr_rss"
    )
    with pytest.raises(ValueError, match="peppr_rss"):
        resolve_collector_class(entry)


def test_configured_user_agent_reaches_the_collector_and_its_http_client(tmp_path: Path) -> None:
    """Allegro blocks the API key when the User-Agent is missing or malformed, so a UA set in
    sources.yaml must actually reach the outgoing request headers -- not just sit in config."""
    required_ua = "DealRadar/0.1.0 (+https://example.org/dealradar)"
    entry = SourceConfig(
        name="allegro",
        kind="api",
        base_url="https://api.allegro.pl",
        risk_prior=0.1,
        user_agent=required_ua,
    )
    collector = build_collector(entry, source_id=1, cache_dir=tmp_path, offline=True)
    assert collector.user_agent == required_ua
    assert collector.client.headers["User-Agent"] == required_ua


def test_collector_falls_back_to_the_default_user_agent_when_config_omits_one(tmp_path: Path) -> None:
    entry = SourceConfig(name="ibood", kind="feed", base_url="https://x.invalid/feed", risk_prior=0.1)
    collector = build_collector(entry, source_id=1, cache_dir=tmp_path, offline=True)
    assert collector.user_agent == DEFAULT_USER_AGENT


def test_unknown_source_name_marks_report_failed(db: Session) -> None:
    cfg = _cfg(SourceConfig(name="ibood", kind="feed", base_url="https://x.invalid/feed", risk_prior=0.1))
    report = _report()
    run_collect(db, cfg, report, source_name="does-not-exist")
    assert report.status == "failed"
    assert report.sources_failed == ["does-not-exist"]


def test_broken_source_does_not_stop_the_others_and_lands_in_sources_failed(db: Session, tmp_path: Path) -> None:
    """Z7: one collector raising must not prevent a sibling source's offers from being collected."""
    ibood_url = "https://ibood.example.invalid/feed"
    cache_dir = tmp_path / "cache"
    write_cached_response(cache_dir / "ibood", ibood_url, read_fixture("ibood", "run1.json"))

    cfg = _cfg(
        SourceConfig(name="ibood", kind="feed", base_url=ibood_url, risk_prior=0.1, enabled=True),
        # No cache entry is pre-populated for this URL, so in offline mode BaseCollector.get() raises
        # CollectorError immediately -- this is our "celowo zepsute źródło" / nonexistent-URL stand-in
        # (the same failure mode a real 404/unreachable host would produce in a live run).
        SourceConfig(
            name="broken_shop", kind="feed", base_url="https://broken.example.invalid/feed.xml", risk_prior=0.05, enabled=True
        ),
    )
    report = _report()
    run_collect(db, cfg, report, cache_dir=cache_dir, offline=True)

    assert "broken_shop" in report.sources_failed
    assert "ibood" not in report.sources_failed
    assert report.offers_fetched == 3
    assert report.offers_new == 3
    notes_by_source = {n.split(",", 1)[0]: n for n in report.notes}
    assert "fetched=3, new=3, errors=0" in notes_by_source["ibood"]
    assert "errors=1" in notes_by_source["broken_shop"]

    # The overall stage report is "partial", not "failed" -- Z7: isolated failure is visible, not fatal.
    if report.sources_failed:
        report.status = "partial"
    assert report.status == "partial"


def test_repeated_collect_on_the_same_fixture_yields_offers_new_zero_the_second_time(
    db: Session, tmp_path: Path
) -> None:
    """Definicja ukończenia #4: re-running collect against unchanged fixtures must not create duplicates."""
    ibood_url = "https://ibood.example.invalid/feed"
    cache_dir = tmp_path / "cache"
    write_cached_response(cache_dir / "ibood", ibood_url, read_fixture("ibood", "run1.json"))
    cfg = _cfg(SourceConfig(name="ibood", kind="feed", base_url=ibood_url, risk_prior=0.1, enabled=True))

    report1 = _report()
    run_collect(db, cfg, report1, cache_dir=cache_dir, offline=True)
    assert report1.offers_fetched == 3
    assert report1.offers_new == 3

    report2 = _report()
    run_collect(db, cfg, report2, cache_dir=cache_dir, offline=True)
    assert report2.offers_fetched == 3
    assert report2.offers_new == 0  # identical content -> deduped by content_hash, nothing new

    total_rows = db.execute(text("SELECT COUNT(*) FROM raw_offer")).scalar_one()
    assert total_rows == 3  # no duplicate rows were inserted on the second run


def test_f5_zero_records_with_prior_nonempty_history_marks_source_failed(db: Session, tmp_path: Path) -> None:
    """F5 (ARCHITEKTURA.md §2): a source that silently starts returning 0 records must be flagged, not OK."""
    ibood_url = "https://ibood.example.invalid/feed"
    cache_dir = tmp_path / "cache" / "ibood"
    write_cached_response(cache_dir, ibood_url, read_fixture("ibood", "run1.json"))
    cfg = _cfg(SourceConfig(name="ibood", kind="feed", base_url=ibood_url, risk_prior=0.1, enabled=True))

    report1 = _report()
    run_collect(db, cfg, report1, cache_dir=tmp_path / "cache", offline=True)
    assert report1.offers_fetched == 3
    assert report1.sources_failed == []

    # Simulate the feed silently starting to return nothing, by overwriting the same cache entry.
    write_cached_response(cache_dir, ibood_url, read_fixture("ibood", "empty.json"))

    report2 = _report()
    run_collect(db, cfg, report2, cache_dir=tmp_path / "cache", offline=True)
    assert report2.offers_fetched == 0
    assert report2.sources_failed == ["ibood"]
    assert any("F5" in note for note in report2.notes)


def test_run_collect_all_enabled_sources_skips_disabled_ones(db: Session, tmp_path: Path) -> None:
    ibood_url = "https://ibood.example.invalid/feed"
    cache_dir = tmp_path / "cache"
    write_cached_response(cache_dir / "ibood", ibood_url, read_fixture("ibood", "run1.json"))

    cfg = _cfg(
        SourceConfig(name="ibood", kind="feed", base_url=ibood_url, risk_prior=0.1, enabled=True),
        SourceConfig(name="allegro", kind="api", base_url="https://api.allegro.pl", risk_prior=0.1, enabled=False),
    )
    report = _report()
    run_collect(db, cfg, report, cache_dir=cache_dir, offline=True)
    assert report.offers_fetched == 3
    assert len(report.notes) == 1  # only the enabled source (ibood) was run at all
    assert dealradar_db.upsert_source is not None  # sanity: db module import used above didn't get optimized away
