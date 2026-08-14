"""End-to-end tests for normalize_batch against synthetic RawOffer fixtures standing in for A1's collectors.

A1 (kolektory) runs in parallel with A2 and has no guaranteed live output yet, so these fixtures
(tests/normalize/fixtures/*.json) are hand-built to look like realistic messy output from a generic
affiliate feed (x-kom), a marketplace API (Allegro), an RSS deal channel (Pepper), and a small daily feed
(iBood) -- per the coordinator's note on Definicja ukończenia item 1. The point is proving normalize_batch
behaves correctly on realistic messy input, not depending on A1's actual run.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.db import get_offer_by_source_external_id
from dealradar.models import RawOffer, Source, compute_content_hash
from dealradar.normalize.brands import load_brand_index
from dealradar.normalize.categories import load_category_map
from dealradar.normalize.mapping import NormalizerConfig
from dealradar.normalize.pipeline import BRANDS_PATH, CATEGORY_MAP_PATH, GenericNormalizer, normalize_batch

# NOTE: these three helpers are intentionally NOT imported from conftest.py -- pytest's default (no
# __init__.py) rootless import mode makes cross-file imports of a module literally named "conftest"
# collide with tests/conftest.py's own module name. Duplicating ~15 lines here is simpler than fighting
# that. `normalize_db` itself is a pytest fixture (auto-discovered, no import needed).

FIXTURES_DIR = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def load_fixture(name: str) -> list[dict[str, Any]]:
    """Loads a JSON array fixture (a list of raw payload_json-shaped dicts) from tests/normalize/fixtures/."""
    data: list[dict[str, Any]] = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
    return data


def seed_source(session: Session, name: str, kind: str) -> int:
    """Registers one source (name/kind matching a plausible config/sources.yaml entry); returns its row id."""
    source_id = dealradar_db.upsert_source(
        session, Source(name=name, kind=kind, base_url=f"https://{name}.example", risk_prior=0.1)
    )
    session.commit()
    return source_id


def insert_raw_offers(
    session: Session, source_id: int, payloads: list[dict[str, Any]], *, fetched_at: datetime = NOW
) -> None:
    """Inserts one raw_offer row per payload dict, keyed by each payload's own `external_id` field."""
    for payload in payloads:
        raw = RawOffer(
            source_id=source_id,
            external_id=str(payload["external_id"]),
            fetched_at=fetched_at,
            payload_json=payload,
            content_hash=compute_content_hash(payload),
        )
        dealradar_db.insert_raw_offer(session, raw)
    session.commit()


def _seed_all_fixtures(session: Session) -> dict[str, int]:
    """Registers the four fixture sources and inserts every fixture payload as a raw_offer row."""
    sources = {
        "x-kom": seed_source(session, "x-kom", "feed"),
        "allegro": seed_source(session, "allegro", "api"),
        "pepper": seed_source(session, "pepper", "rss"),
        "ibood": seed_source(session, "ibood", "feed"),
    }
    insert_raw_offers(session, sources["x-kom"], load_fixture("x_kom_feed.json"))
    insert_raw_offers(session, sources["allegro"], load_fixture("allegro_marketplace.json"))
    insert_raw_offers(session, sources["pepper"], load_fixture("pepper_rss.json"))
    insert_raw_offers(session, sources["ibood"], load_fixture("ibood_feed.json"))
    return sources


def test_normalize_batch_processes_all_fixtures_without_crashing(normalize_db: Session) -> None:
    _seed_all_fixtures(normalize_db)
    total_raw = normalize_db.execute(text("SELECT COUNT(*) FROM raw_offer")).scalar_one()

    report = normalize_batch(normalize_db, since=None)

    assert report.processed == total_raw
    assert report.accepted + report.rejected == report.processed
    assert report.accepted > 0
    assert report.rejected > 0  # the fixtures deliberately include unfixable rows

    offer_count = normalize_db.execute(text("SELECT COUNT(*) FROM offer")).scalar_one()
    assert offer_count == report.accepted


def test_rejected_reasons_cover_the_deliberately_broken_fixture_rows(normalize_db: Session) -> None:
    _seed_all_fixtures(normalize_db)
    report = normalize_batch(normalize_db, since=None)

    assert report.rejected_by_reason.get("missing_title", 0) >= 1
    assert report.rejected_by_reason.get("missing_url", 0) >= 1
    assert report.rejected_by_reason.get("missing_or_unparseable_price", 0) >= 2  # x-kom + pepper each have one


def test_price_parsing_and_price_incomplete_through_the_pipeline(normalize_db: Session) -> None:
    sources = _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)

    ssd = get_offer_by_source_external_id(normalize_db, sources["x-kom"], "xk-ssd-990pro-2tb")
    assert ssd is not None
    assert ssd.price_gross == 129900  # "1 299,00 zł"
    assert ssd.shipping_cost == 0
    assert ssd.price_incomplete is False
    assert ssd.price_total == 129900

    monitor = get_offer_by_source_external_id(normalize_db, sources["x-kom"], "xk-lg-27gp850")
    assert monitor is not None
    assert monitor.price_gross == 129900  # "1.299,00" PL-format trap
    assert monitor.shipping_cost is None
    assert monitor.price_incomplete is True  # F3: no shipping cost known


def test_invalid_ean_is_nulled_with_data_issue_never_passed_through(normalize_db: Session) -> None:
    sources = _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)

    bundle_offer = get_offer_by_source_external_id(normalize_db, sources["x-kom"], "xk-duracell-aa-2pak")
    assert bundle_offer is not None
    assert bundle_offer.ean is None  # checksum-invalid EAN in the fixture must never survive
    assert any("invalid EAN dropped" in issue for issue in bundle_offer.data_issues)


def test_bundle_detection_through_the_pipeline(normalize_db: Session) -> None:
    sources = _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)

    bundle_offer = get_offer_by_source_external_id(normalize_db, sources["x-kom"], "xk-duracell-aa-2pak")
    assert bundle_offer is not None
    assert bundle_offer.is_bundle is True
    assert bundle_offer.bundle_size == 2


def test_condition_detection_through_the_pipeline(normalize_db: Session) -> None:
    sources = _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)

    poleasing = get_offer_by_source_external_id(normalize_db, sources["allegro"], "al-dell-5420-poleasing")
    assert poleasing is not None
    assert poleasing.condition == "refurb"

    undeclared_marketplace = get_offer_by_source_external_id(normalize_db, sources["allegro"], "al-anker-20000")
    assert undeclared_marketplace is not None
    assert undeclared_marketplace.condition is None  # Allegro: undeclared must stay None, never "new"

    damaged = get_offer_by_source_external_id(normalize_db, sources["allegro"], "al-asus-vivobook-damaged")
    assert damaged is not None
    assert damaged.condition == "damaged"


def test_allegro_parameters_hook_extracts_ean(normalize_db: Session) -> None:
    sources = _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)

    kingston = get_offer_by_source_external_id(normalize_db, sources["allegro"], "al-kingston-nv2-1tb")
    assert kingston is not None
    assert kingston.ean == "5901234123457"


def test_pepper_title_price_hook_extracts_price_from_title(normalize_db: Session) -> None:
    sources = _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)

    lg_deal = get_offer_by_source_external_id(normalize_db, sources["pepper"], "pp-lg-27gp850")
    assert lg_deal is not None
    assert lg_deal.price_gross == 179900  # "@ 1799 zł" inside the title, no dedicated price field


def test_brand_and_category_resolved_through_the_pipeline(normalize_db: Session) -> None:
    sources = _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)

    ssd = get_offer_by_source_external_id(normalize_db, sources["x-kom"], "xk-ssd-990pro-2tb")
    assert ssd is not None
    assert ssd.brand_norm == "samsung"
    assert ssd.category_id is not None  # "Dyski SSD" is mapped in config/category_map.yaml


def test_unmapped_category_is_reported(normalize_db: Session) -> None:
    _seed_all_fixtures(normalize_db)
    report = normalize_batch(normalize_db, since=None)

    unmapped_sources = {row[0] for row in report.unmapped_categories}
    unmapped_cats = {row[1] for row in report.unmapped_categories}
    assert "x-kom" in unmapped_sources
    assert "Akcesoria RTV" in unmapped_cats  # deliberately not in config/category_map.yaml


def test_coverage_percentages_are_populated_and_bounded(normalize_db: Session) -> None:
    _seed_all_fixtures(normalize_db)
    report = normalize_batch(normalize_db, since=None)

    for pct in (report.pct_with_ean, report.pct_with_brand, report.pct_with_category, report.pct_with_any_attr):
        assert pct is not None
        assert 0.0 <= pct <= 100.0


def test_rerunning_normalize_batch_does_not_duplicate_offers(normalize_db: Session) -> None:
    _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)
    offer_count_1 = normalize_db.execute(text("SELECT COUNT(*) FROM offer")).scalar_one()

    normalize_batch(normalize_db, since=None)
    offer_count_2 = normalize_db.execute(text("SELECT COUNT(*) FROM offer")).scalar_one()

    assert offer_count_1 == offer_count_2  # upsert_offer is keyed by (source_id, external_id) -- idempotent


def test_rejected_offer_table_is_queryable(normalize_db: Session) -> None:
    _seed_all_fixtures(normalize_db)
    normalize_batch(normalize_db, since=None)

    rows = normalize_db.execute(text("SELECT reason, detail FROM rejected_offer")).all()
    assert len(rows) > 0
    assert all(row.reason and row.detail for row in rows)


# --------------------------------------------------------------------------------------------------
# Targeted GenericNormalizer tests not covered by the shared fixture files (FX conversion, and a
# realistic bad-data ValidationError path).
# --------------------------------------------------------------------------------------------------


def _normalizer_for(source_name: str, config: NormalizerConfig) -> GenericNormalizer:
    brand_index = load_brand_index(BRANDS_PATH)
    category_map = load_category_map(CATEGORY_MAP_PATH)
    return GenericNormalizer(source_name, config=config, brand_index=brand_index, category_map=category_map)


def _raw(payload: dict[str, object], *, external_id: str = "ext-1", source_id: int = 1) -> RawOffer:
    return RawOffer(
        id=1,
        source_id=source_id,
        external_id=external_id,
        fetched_at=datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
        payload_json=payload,
        content_hash=compute_content_hash(payload),
    )


def test_fx_conversion_applies_rate_and_records_evidence(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from dealradar.normalize import fx as fx_module

    def _fake_rate(currency: str, as_of: object, *, cache_dir: object = None, http_get: object = None):  # type: ignore[no-untyped-def]
        assert currency == "EUR"
        return 4.30, "155/A/NBP/2026"

    monkeypatch.setattr(fx_module, "get_fx_rate_to_pln", _fake_rate)

    config = NormalizerConfig(
        fields={
            "title": ["title"], "url": ["url"], "price_gross": ["price"], "currency": ["currency"],
        },
        assume_new_if_undeclared=True,
    )
    normalizer = _normalizer_for("eur-shop", config)
    raw = _raw({"title": "Test EUR product", "url": "https://eur-shop.example/1", "price": "10.00", "currency": "EUR"})

    result = normalizer.to_offer(raw)
    from dealradar.models import Offer

    assert isinstance(result, Offer)
    assert result.currency == "PLN"
    assert result.price_gross == 4300  # 10.00 EUR * 4.30
    assert result.evidence["fx_rate"] == 4.30
    assert result.evidence["fx_currency"] == "EUR"


def test_fx_unavailable_rejects_with_clear_reason(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from dealradar.normalize import fx as fx_module
    from dealradar.normalize.report import RejectedOffer

    monkeypatch.setattr(fx_module, "get_fx_rate_to_pln", lambda *a, **k: None)

    config = NormalizerConfig(
        fields={"title": ["title"], "url": ["url"], "price_gross": ["price"], "currency": ["currency"]},
        assume_new_if_undeclared=True,
    )
    normalizer = _normalizer_for("eur-shop", config)
    raw = _raw({"title": "Test EUR product", "url": "https://eur-shop.example/1", "price": "10.00", "currency": "EUR"})

    result = normalizer.to_offer(raw)
    assert isinstance(result, RejectedOffer)
    assert result.reason == "fx_rate_unavailable"


def test_out_of_range_seller_rating_is_rejected_not_crashed() -> None:
    """A garbage seller_rating (e.g. 9.9 on a 0-5 scale) must become a RejectedOffer, not an unhandled crash."""
    from dealradar.normalize.report import RejectedOffer

    config = NormalizerConfig(
        fields={
            "title": ["title"], "url": ["url"], "price_gross": ["price"], "seller_rating": ["seller_rating"],
        },
        assume_new_if_undeclared=False,
    )
    normalizer = _normalizer_for("bad-data-shop", config)
    raw = _raw(
        {"title": "Podejrzana oferta", "url": "https://bad-data-shop.example/1", "price": "99,00", "seller_rating": "9.9"}
    )
    result = normalizer.to_offer(raw)
    assert isinstance(result, RejectedOffer)
    assert result.reason == "offer_validation_failed"
