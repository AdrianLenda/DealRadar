"""Tests for dealradar.scoring.history -- snapshot_prices, price_series, compact_price_history. Owner: A4."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.models import Offer, Source
from dealradar.scoring.history import compact_price_history, price_series, snapshot_prices

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


def _make_source(session: Session, name: str = "x-kom") -> int:
    return dealradar_db.upsert_source(
        session, Source(name=name, kind="feed", base_url=f"https://{name}.example", risk_prior=0.0)
    )


def _make_offer(session: Session, source_id: int, external_id: str, *, last_seen: datetime, in_stock: bool | None = True) -> int:
    offer = Offer(
        source_id=source_id,
        external_id=external_id,
        first_seen=last_seen,
        last_seen=last_seen,
        title=f"Test offer {external_id}",
        url=f"https://test.example/{external_id}",
        price_gross=29900,
        shipping_cost=0,
        condition="new",
        in_stock=in_stock,
    )
    offer_id = dealradar_db.upsert_offer(session, offer)
    session.commit()
    return offer_id


# --------------------------------------------------------------------------------------------------
# snapshot_prices
# --------------------------------------------------------------------------------------------------


def test_snapshot_prices_writes_one_point_for_an_offer_seen_today(db: Session) -> None:
    source_id = _make_source(db)
    offer_id = _make_offer(db, source_id, "xk-1", last_seen=NOW)

    report = snapshot_prices(db, at=TODAY)
    db.commit()

    assert report.offers_seen_today == 1
    assert report.offers_marked_out_of_stock == 0
    assert report.offers_stopped_tracking == 0

    rows = db.execute(text("SELECT price_total, in_stock FROM price_point WHERE offer_id = :id"), {"id": offer_id}).all()
    assert len(rows) == 1
    assert rows[0].price_total == 29900
    assert bool(rows[0].in_stock) is True


def test_snapshot_prices_is_idempotent_run_twice_same_day(db: Session) -> None:
    """Running `dealradar snapshot` twice on the same day must not create duplicate price_point rows."""
    source_id = _make_source(db)
    offer_id = _make_offer(db, source_id, "xk-1", last_seen=NOW)

    snapshot_prices(db, at=TODAY)
    db.commit()
    snapshot_prices(db, at=TODAY)
    db.commit()

    count = db.execute(text("SELECT COUNT(*) FROM price_point WHERE offer_id = :id"), {"id": offer_id}).scalar_one()
    assert count == 1


def test_snapshot_prices_marks_disappeared_offer_out_of_stock_within_grace(db: Session) -> None:
    source_id = _make_source(db)
    last_seen = NOW - timedelta(days=3)
    offer_id = _make_offer(db, source_id, "xk-1", last_seen=last_seen)

    report = snapshot_prices(db, at=TODAY)
    db.commit()

    assert report.offers_seen_today == 0
    assert report.offers_marked_out_of_stock == 1
    row = db.execute(text("SELECT in_stock FROM price_point WHERE offer_id = :id"), {"id": offer_id}).one()
    assert bool(row.in_stock) is False


def test_snapshot_prices_stops_tracking_after_grace_period(db: Session) -> None:
    source_id = _make_source(db)
    last_seen = NOW - timedelta(days=10)  # beyond the 7-day default grace window
    offer_id = _make_offer(db, source_id, "xk-1", last_seen=last_seen)

    report = snapshot_prices(db, at=TODAY)
    db.commit()

    assert report.offers_seen_today == 0
    assert report.offers_marked_out_of_stock == 0
    assert report.offers_stopped_tracking == 1
    count = db.execute(text("SELECT COUNT(*) FROM price_point WHERE offer_id = :id"), {"id": offer_id}).scalar_one()
    assert count == 0


def test_snapshot_prices_boundary_day_seven_still_tracked_day_eight_stopped(db: Session) -> None:
    source_id = _make_source(db)
    offer_day7 = _make_offer(db, source_id, "xk-day7", last_seen=NOW - timedelta(days=7))
    offer_day8 = _make_offer(db, source_id, "xk-day8", last_seen=NOW - timedelta(days=8))

    report = snapshot_prices(db, at=TODAY)
    db.commit()

    assert report.offers_marked_out_of_stock == 1
    assert report.offers_stopped_tracking == 1
    count_day7 = db.execute(text("SELECT COUNT(*) FROM price_point WHERE offer_id = :id"), {"id": offer_day7}).scalar_one()
    count_day8 = db.execute(text("SELECT COUNT(*) FROM price_point WHERE offer_id = :id"), {"id": offer_day8}).scalar_one()
    assert count_day7 == 1
    assert count_day8 == 0


# --------------------------------------------------------------------------------------------------
# price_series
# --------------------------------------------------------------------------------------------------


def test_price_series_returns_history_ordered_oldest_first(db: Session) -> None:
    source_id = _make_source(db)
    offer_id = _make_offer(db, source_id, "xk-1", last_seen=NOW)
    product_id = _insert_product(db, "Test Product")
    db.execute(text("UPDATE offer SET product_id = :pid WHERE id = :oid"), {"pid": product_id, "oid": offer_id})
    db.commit()

    for i, price in enumerate([1000, 950, 900]):
        observed = NOW - timedelta(days=2 - i)
        dealradar_db.append_price_point(db, offer_id, price_total=price * 100, in_stock=True, observed_at=observed)
    db.commit()

    series = price_series(db, product_id, days=10, at=TODAY)
    assert [p.price_total for p in series] == [100000, 95000, 90000]
    assert series[0].observed_at < series[-1].observed_at


def _insert_product(session: Session, title: str) -> int:
    result = session.execute(
        text(
            "INSERT INTO product (canonical_title, brand, mpn, ean, category_id, created_at) "
            "VALUES (:t, NULL, NULL, NULL, NULL, :c) RETURNING id"
        ),
        {"t": title, "c": NOW.isoformat()},
    )
    return int(result.scalar_one())


# --------------------------------------------------------------------------------------------------
# compact_price_history
# --------------------------------------------------------------------------------------------------


def test_compact_price_history_reduces_old_week_to_min_max_median(db: Session) -> None:
    source_id = _make_source(db)
    offer_id = _make_offer(db, source_id, "xk-1", last_seen=NOW)

    # One ISO week (7 daily points), all older than the 180-day compaction threshold.
    old_monday = TODAY - timedelta(days=200)
    old_monday -= timedelta(days=old_monday.weekday())  # snap to Monday for a clean single ISO week
    prices = [1000, 900, 1100, 950, 1050, 800, 1200]  # min=800, max=1200, median=1000
    for i, price in enumerate(prices):
        observed = datetime.combine(old_monday + timedelta(days=i), datetime.min.time(), tzinfo=timezone.utc)
        dealradar_db.append_price_point(db, offer_id, price_total=price * 100, in_stock=True, observed_at=observed)
    db.commit()

    before_count = db.execute(text("SELECT COUNT(*) FROM price_point WHERE offer_id = :id"), {"id": offer_id}).scalar_one()
    assert before_count == 7

    report = compact_price_history(db, before=TODAY)
    db.commit()

    after_rows = db.execute(
        text("SELECT price_total FROM price_point WHERE offer_id = :id ORDER BY price_total"), {"id": offer_id}
    ).all()
    after_prices = sorted(r.price_total for r in after_rows)
    assert after_prices == [80000, 100000, 120000]  # min, median, max kept; the other 4 rows deleted
    assert report.rows_deleted == 4
    assert report.offers_affected == 1


def test_compact_price_history_leaves_recent_weeks_untouched(db: Session) -> None:
    source_id = _make_source(db)
    offer_id = _make_offer(db, source_id, "xk-1", last_seen=NOW)

    for i in range(7):
        observed = NOW - timedelta(days=6 - i)  # last 7 days: well within the 180-day threshold
        dealradar_db.append_price_point(db, offer_id, price_total=(1000 + i) * 100, in_stock=True, observed_at=observed)
    db.commit()

    report = compact_price_history(db, before=TODAY)
    db.commit()

    count = db.execute(text("SELECT COUNT(*) FROM price_point WHERE offer_id = :id"), {"id": offer_id}).scalar_one()
    assert count == 7  # untouched -- none of these rows are older than the 180-day threshold
    assert report.rows_deleted == 0
