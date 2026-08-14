"""Tests for dealradar.db — migrations, upsert_offer idempotency, append_price_point, run_report."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.models import Offer, Source

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------------------------------


def test_migrations_run_on_clean_db() -> None:
    engine = dealradar_db.build_engine("sqlite:///:memory:")
    applied = dealradar_db.run_migrations(engine)
    assert applied == ["001_init.sql"]

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()
        }
    expected = {
        "source", "raw_offer", "category", "product", "product_attr", "offer", "price_point",
        "deal", "quality_score", "shop_credibility", "match_review", "watch", "run_log", "schema_version",
    }
    assert expected.issubset(tables)


def test_migrations_are_idempotent() -> None:
    engine = dealradar_db.build_engine("sqlite:///:memory:")
    first = dealradar_db.run_migrations(engine)
    second = dealradar_db.run_migrations(engine)
    assert first == ["001_init.sql"]
    assert second == []


def test_required_indexes_exist() -> None:
    engine = dealradar_db.build_engine("sqlite:///:memory:")
    dealradar_db.run_migrations(engine)
    with engine.connect() as conn:
        indexes = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'")).fetchall()
        }
    required = {
        "idx_price_point_offer_observed",
        "idx_offer_product",
        "idx_offer_source_external",
        "idx_offer_ean",
        "idx_offer_brand_category",
        "idx_raw_offer_source_hash",
    }
    assert required.issubset(indexes)


# --------------------------------------------------------------------------------------------------
# upsert_offer idempotency
# --------------------------------------------------------------------------------------------------


def _make_offer(source_id: int, **overrides: object) -> Offer:
    base: dict[str, object] = dict(
        source_id=source_id,
        external_id="ext-42",
        first_seen=NOW,
        last_seen=NOW,
        title="Dysk SSD Test 1TB",
        url="https://example.test/ssd-1tb",
        price_gross=29900,
        shipping_cost=0,
    )
    base.update(overrides)
    return Offer(**base)  # type: ignore[arg-type]


def test_upsert_offer_is_idempotent_on_source_and_external_id(db: Session) -> None:
    source_id = dealradar_db.upsert_source(
        db, Source(name="x-kom", kind="feed", base_url="https://x-kom.example", risk_prior=0.0)
    )
    db.commit()

    first_seen = NOW
    row_id_1 = dealradar_db.upsert_offer(db, _make_offer(source_id, first_seen=first_seen, last_seen=first_seen))
    db.commit()

    later = first_seen + timedelta(days=3)
    row_id_2 = dealradar_db.upsert_offer(
        db,
        _make_offer(
            source_id, first_seen=later, last_seen=later, title="Dysk SSD Test 1TB (nowa cena)", price_gross=27900
        ),
    )
    db.commit()

    assert row_id_1 == row_id_2

    count = db.execute(text("SELECT COUNT(*) FROM offer")).scalar_one()
    assert count == 1

    row = db.execute(text("SELECT first_seen, last_seen, title, price_gross FROM offer WHERE id = :id"), {"id": row_id_1}).one()
    assert row.first_seen == first_seen.isoformat()  # first_seen must NOT be overwritten
    assert row.last_seen == later.isoformat()  # last_seen must be updated
    assert row.title == "Dysk SSD Test 1TB (nowa cena)"
    assert row.price_gross == 27900


def test_upsert_offer_roundtrips_through_get_offer(db: Session) -> None:
    source_id = dealradar_db.upsert_source(
        db, Source(name="allegro", kind="api", base_url="https://allegro.example", risk_prior=0.1)
    )
    db.commit()
    dealradar_db.upsert_offer(db, _make_offer(source_id, ean="4006381333931", data_issues=["some note"]))
    db.commit()

    fetched = dealradar_db.get_offer_by_source_external_id(db, source_id, "ext-42")
    assert fetched is not None
    assert fetched.ean == "4006381333931"
    assert fetched.data_issues == ["some note"]
    assert fetched.price_total == 29900
    assert fetched.price_incomplete is False


# --------------------------------------------------------------------------------------------------
# append_price_point: at most one row per offer per day
# --------------------------------------------------------------------------------------------------


def test_append_price_point_one_per_offer_per_day(db: Session) -> None:
    source_id = dealradar_db.upsert_source(
        db, Source(name="x-kom", kind="feed", base_url="https://x-kom.example", risk_prior=0.0)
    )
    db.commit()
    offer_id = dealradar_db.upsert_offer(db, _make_offer(source_id))
    db.commit()

    morning = datetime(2026, 8, 9, 8, 0, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 8, 9, 20, 0, 0, tzinfo=timezone.utc)

    id_1 = dealradar_db.append_price_point(db, offer_id, price_total=29900, in_stock=True, observed_at=morning)
    db.commit()
    id_2 = dealradar_db.append_price_point(db, offer_id, price_total=27900, in_stock=True, observed_at=evening)
    db.commit()

    assert id_1 == id_2
    count = db.execute(text("SELECT COUNT(*) FROM price_point WHERE offer_id = :id"), {"id": offer_id}).scalar_one()
    assert count == 1
    price = db.execute(text("SELECT price_total FROM price_point WHERE offer_id = :id"), {"id": offer_id}).scalar_one()
    assert price == 27900  # updated to the later observation, not duplicated


def test_append_price_point_creates_new_row_on_a_new_day(db: Session) -> None:
    source_id = dealradar_db.upsert_source(
        db, Source(name="x-kom", kind="feed", base_url="https://x-kom.example", risk_prior=0.0)
    )
    db.commit()
    offer_id = dealradar_db.upsert_offer(db, _make_offer(source_id))
    db.commit()

    day1 = datetime(2026, 8, 9, 8, 0, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 8, 10, 8, 0, 0, tzinfo=timezone.utc)
    dealradar_db.append_price_point(db, offer_id, price_total=29900, in_stock=True, observed_at=day1)
    db.commit()
    dealradar_db.append_price_point(db, offer_id, price_total=28900, in_stock=True, observed_at=day2)
    db.commit()

    count = db.execute(text("SELECT COUNT(*) FROM price_point WHERE offer_id = :id"), {"id": offer_id}).scalar_one()
    assert count == 2


def test_append_price_point_rejects_float_price(db: Session) -> None:
    source_id = dealradar_db.upsert_source(
        db, Source(name="x-kom", kind="feed", base_url="https://x-kom.example", risk_prior=0.0)
    )
    db.commit()
    offer_id = dealradar_db.upsert_offer(db, _make_offer(source_id))
    db.commit()
    with pytest.raises(TypeError):
        dealradar_db.append_price_point(db, offer_id, price_total=299.5, in_stock=True, observed_at=NOW)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------------
# run_report / run_log (Z7)
# --------------------------------------------------------------------------------------------------


def test_run_report_writes_run_log_on_success(db: Session) -> None:
    with dealradar_db.run_report(db, "collect") as report:
        report.offers_fetched = 10
        report.offers_new = 4
        report.pct_with_ean = 80.0

    rows = db.execute(text("SELECT stage, status, offers_fetched, offers_new FROM run_log")).all()
    assert len(rows) == 1
    assert rows[0].stage == "collect"
    assert rows[0].status == "ok"
    assert rows[0].offers_fetched == 10
    assert rows[0].offers_new == 4


def test_run_report_marks_partial_when_sources_failed(db: Session) -> None:
    with dealradar_db.run_report(db, "collect") as report:
        report.sources_failed = ["aliexpress"]

    status = db.execute(text("SELECT status FROM run_log")).scalar_one()
    assert status == "partial"


def test_run_report_records_failure_and_reraises(db: Session) -> None:
    with pytest.raises(ValueError):
        with dealradar_db.run_report(db, "collect"):
            raise ValueError("boom")

    row = db.execute(text("SELECT status, notes FROM run_log")).one()
    assert row.status == "failed"
    assert "boom" in row.notes


def test_run_report_does_not_persist_partial_stage_work_on_failure(db: Session) -> None:
    source_id = dealradar_db.upsert_source(
        db, Source(name="x-kom", kind="feed", base_url="https://x-kom.example", risk_prior=0.0)
    )
    db.commit()

    with pytest.raises(RuntimeError):
        with dealradar_db.run_report(db, "collect"):
            dealradar_db.upsert_offer(db, _make_offer(source_id))
            raise RuntimeError("simulated mid-stage crash")

    # the offer insert was rolled back along with the rest of the failed transaction
    count = db.execute(text("SELECT COUNT(*) FROM offer")).scalar_one()
    assert count == 0
    # but the failure itself is recorded
    status = db.execute(text("SELECT status FROM run_log")).scalar_one()
    assert status == "failed"
