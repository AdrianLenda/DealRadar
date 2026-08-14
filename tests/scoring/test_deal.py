"""Tests for dealradar.scoring.deal -- deal_score (ARCHITEKTURA.md section 8) and shop_credibility.

Owner: A4. These are synthetic time-series tests per prompty/A4-historia-i-deal-score.md's "Definicja
ukończenia" -- there is no real collector output yet (A1/A2/A3 run in parallel), so every fixture here is
built directly against price_point/offer/product, which is also simply the right way to test statistical
behavior deterministically.
"""

from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.models import Offer, Source
from dealradar.scoring.deal import score_deals

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


# --------------------------------------------------------------------------------------------------
# Fixture helpers -- synthetic Source/Product/Offer/PricePoint, built directly (no collector involved)
# --------------------------------------------------------------------------------------------------


def _make_source(session: Session, name: str) -> int:
    return dealradar_db.upsert_source(
        session, Source(name=name, kind="feed", base_url=f"https://{name}.example", risk_prior=0.0)
    )


def _make_product(session: Session, title: str) -> int:
    result = session.execute(
        text(
            "INSERT INTO product (canonical_title, brand, mpn, ean, category_id, created_at) "
            "VALUES (:t, NULL, NULL, NULL, NULL, :c) RETURNING id"
        ),
        {"t": title, "c": NOW.isoformat()},
    )
    return int(result.scalar_one())


def _make_offer(
    session: Session,
    source_id: int,
    product_id: int,
    external_id: str,
    *,
    match_confidence: float = 0.99,
    condition: str = "new",
    list_price_claimed: int | None = None,
    shipping_cost: int | None = 0,
    in_stock: bool | None = True,
) -> int:
    offer = Offer(
        source_id=source_id,
        external_id=external_id,
        first_seen=NOW,
        last_seen=NOW,
        title=f"Test offer {external_id}",
        url=f"https://test.example/{external_id}",
        price_gross=100000,
        shipping_cost=shipping_cost,
        condition=condition,
        match_confidence=match_confidence,
        match_method="ean",
        product_id=product_id,
        list_price_claimed=list_price_claimed,
        in_stock=in_stock,
    )
    return dealradar_db.upsert_offer(session, offer)


def _write_price(session: Session, offer_id: int, days_ago: int, price_total: int, *, in_stock: bool = True) -> None:
    """Writes a price_point `days_ago` days before NOW (days_ago=0 means "today")."""
    observed = NOW - timedelta(days=days_ago)
    dealradar_db.append_price_point(session, offer_id, price_total=price_total, in_stock=in_stock, observed_at=observed)


def _get_deal(session: Session, offer_id: int) -> dict[str, Any] | None:
    """Returns the most recent deal row for offer_id as a plain dict with evidence_json parsed, or None."""
    row = session.execute(
        text("SELECT * FROM deal WHERE offer_id = :id ORDER BY id DESC LIMIT 1"), {"id": offer_id}
    ).mappings().first()
    if row is None:
        return None
    d = dict(row)
    d["evidence"] = json.loads(d["evidence_json"])
    return d


# --------------------------------------------------------------------------------------------------
# Definicja ukończenia #3, bullet 1: cena stała 90 dni, potem -40% -> wysoki deal_score
# --------------------------------------------------------------------------------------------------


def test_flat_price_90_days_then_40pct_drop_gives_high_deal_score(db: Session) -> None:
    src_a, src_b, src_c = _make_source(db, "a"), _make_source(db, "b"), _make_source(db, "c")
    pid = _make_product(db, "Flat then drop")
    off_a = _make_offer(db, src_a, pid, "a-1")
    off_b = _make_offer(db, src_b, pid, "b-1")
    off_c = _make_offer(db, src_c, pid, "c-1")
    for off in (off_a, off_b, off_c):
        for d in range(1, 90):
            _write_price(db, off, d, 100_000)  # 89 days at 1000 zl
    _write_price(db, off_a, 0, 60_000)  # today: A drops 40%
    _write_price(db, off_b, 0, 100_000)  # B and C stay at the normal price
    _write_price(db, off_c, 0, 100_000)
    db.commit()

    report = score_deals(db, at=TODAY)
    db.commit()

    assert report.basis_counts["history"] == 3
    deal_a = _get_deal(db, off_a)
    assert deal_a is not None
    assert deal_a["basis"] == "history"
    assert deal_a["coverage_cap"] == 1.0  # 90 days of history, 3 competing offers, 3 sources -- no ceiling binds
    assert deal_a["deal_score"] >= 90.0, deal_a

    deal_b = _get_deal(db, off_b)
    assert deal_b is not None
    assert deal_b["deal_score"] < 20.0  # unchanged price must not look like a deal


# --------------------------------------------------------------------------------------------------
# Definicja ukończenia #3, bullet 2 (F1 defense): fake 30% markup for 10 days, then "discount" back to
# the original price -> deal_score near zero AND a high claimed-vs-real gap (bullshit_index).
# --------------------------------------------------------------------------------------------------


def test_fake_markup_then_fake_discount_gives_low_score_and_high_bullshit_index(db: Session) -> None:
    src_shady = _make_source(db, "shady-shop")
    src_b = _make_source(db, "b")
    src_c = _make_source(db, "c")
    shady_offer_ids: list[int] = []

    # 20 products from the same shop, all exhibiting the exact same pattern: 79 normal days at 1000 zl,
    # then 10 days marked up to 1300 zl, then "discounted" back to 1000 zl today, declaring
    # list_price_claimed=1300 (implying a ~23% discount that never actually happened). 20 offers is the
    # shop_credibility min_sample_size, so bullshit_index comes back as a real number, not None.
    for i in range(20):
        pid = _make_product(db, f"F1 product {i}")
        off_a = _make_offer(db, src_shady, pid, f"shady-{i}", list_price_claimed=130_000)
        off_b = _make_offer(db, src_b, pid, f"b-{i}")
        off_c = _make_offer(db, src_c, pid, f"c-{i}")
        for d in range(11, 90):
            _write_price(db, off_a, d, 100_000)  # 79 normal days
        for d in range(1, 11):
            _write_price(db, off_a, d, 130_000)  # 10-day fake markup
        _write_price(db, off_a, 0, 100_000)  # "discount" back to the original, normal price
        _write_price(db, off_b, 0, 95_000)
        _write_price(db, off_c, 0, 95_000)
        shady_offer_ids.append(off_a)
    db.commit()

    score_deals(db, at=TODAY)
    db.commit()

    for off_a in shady_offer_ids:
        deal_a = _get_deal(db, off_a)
        assert deal_a is not None
        assert deal_a["depth_vs_median_90d"] == 0.0  # price is exactly at the 90-day median: no real discount
        assert deal_a["deal_score"] < 20.0  # far below the >=90 a genuine 40% drop gets (previous test)

    row = db.execute(
        text(
            "SELECT bullshit_index, n_offers, claimed_discount_avg, real_discount_avg "
            "FROM shop_credibility WHERE source_id = :sid"
        ),
        {"sid": src_shady},
    ).mappings().first()
    assert row is not None
    assert row["n_offers"] == 20
    assert row["bullshit_index"] is not None
    assert row["real_discount_avg"] == 0.0
    assert row["bullshit_index"] > 0.15  # claimed ~23% off vs. 0% real -- a large, real gap


# --------------------------------------------------------------------------------------------------
# Definicja ukończenia #3, bullet 3: one outlier point in the series does not move the median (MAD).
# --------------------------------------------------------------------------------------------------


def test_median_and_mad_resist_a_single_outlier(db: Session) -> None:
    src = _make_source(db, "a")
    pid = _make_product(db, "Outlier resistant")
    off = _make_offer(db, src, pid, "a-1")
    for d in range(1, 90):
        _write_price(db, off, d, 100_000)  # 89 days at 1000 zl
    _write_price(db, off, 45, 20_000)  # overwrite one day with a one-off clearance to 200 zl
    _write_price(db, off, 0, 99_000)  # today: 990 zl -- essentially the normal price, not a real deal
    db.commit()

    score_deals(db, at=TODAY)
    db.commit()

    deal = _get_deal(db, off)
    assert deal is not None
    ev = deal["evidence"]
    assert ev["med"] == 100_000.0  # median stays anchored at the true normal price
    assert ev["mad"] == 0.0  # dominant 88-day cluster has zero deviation; the outlier is outvoted
    assert ev["sigma"] == 1_000.0  # sigma floor (0.01*med) governs since MAD collapsed to 0

    # Contrast against a naive mean/stdev computed on the exact same series: a single low outlier both
    # drags the mean down AND inflates stdev far more than it should -- the classic "one clearance sale
    # corrupts a whole quarter" failure this design exists to avoid.
    naive_series = [100_000] * 88 + [20_000, 99_000]
    naive_mean = statistics.mean(naive_series)
    naive_stdev = statistics.pstdev(naive_series)
    assert naive_mean < 100_000  # the mean is pulled down by the single outlier
    assert ev["sigma"] < naive_stdev  # MAD-derived sigma is far tighter than the outlier-inflated stdev


# --------------------------------------------------------------------------------------------------
# Definicja ukończenia #3, bullet 4: 5 punktów historii -> sufit 0,50 zastosowany i zapisany w evidence.
# --------------------------------------------------------------------------------------------------


def test_five_history_points_applies_050_ceiling_recorded_in_evidence(db: Session) -> None:
    src = _make_source(db, "a")
    pid = _make_product(db, "Thin history")
    off = _make_offer(db, src, pid, "a-1")
    for d in range(1, 5):
        _write_price(db, off, d, 100_000)  # 4 prior days
    _write_price(db, off, 0, 50_000)  # today: an apparent 50% drop
    db.commit()

    score_deals(db, at=TODAY)
    db.commit()

    deal = _get_deal(db, off)
    assert deal is not None
    assert deal["evidence"]["n_days_history"] == 5
    assert deal["coverage_cap"] == 0.50
    assert deal["deal_score"] == 50.0  # raw would be 1.0 (maxed) without the ceiling -- the ceiling binds
    assert any("14d" in reason for reason in deal["evidence"]["applied_cap_reasons"])


# --------------------------------------------------------------------------------------------------
# Definicja ukończenia #3, bullet 5 (Z4): a lone offer with no history gets NO deal record, not a zero.
# --------------------------------------------------------------------------------------------------


def test_lone_offer_with_no_history_gets_no_deal_record_not_zero(db: Session) -> None:
    src = _make_source(db, "a")
    pid = _make_product(db, "Brand new, single shop")
    off = _make_offer(db, src, pid, "a-1")
    _write_price(db, off, 0, 100_000)  # only today's point: zero real history, zero competitors
    db.commit()

    report = score_deals(db, at=TODAY)
    db.commit()

    assert _get_deal(db, off) is None
    assert report.skipped_no_deal == 1
    assert report.deals_scored == 0


# --------------------------------------------------------------------------------------------------
# Z3: list_price_claimed must never influence depth/z/deal_score -- only the bullshit index may read it.
# --------------------------------------------------------------------------------------------------


def test_list_price_claimed_never_affects_deal_score(db: Session) -> None:
    src_a, src_b, src_c = _make_source(db, "a"), _make_source(db, "b"), _make_source(db, "c")

    def _build(title: str, claimed: int | None) -> int:
        pid = _make_product(db, title)
        o1 = _make_offer(db, src_a, pid, f"{title}-a", list_price_claimed=claimed)
        o2 = _make_offer(db, src_b, pid, f"{title}-b")
        o3 = _make_offer(db, src_c, pid, f"{title}-c")
        for off in (o1, o2, o3):
            for d in range(1, 90):
                _write_price(db, off, d, 100_000)
        _write_price(db, o1, 0, 70_000)
        _write_price(db, o2, 0, 100_000)
        _write_price(db, o3, 0, 100_000)
        return o1

    off_no_claim = _build("no-claim", None)
    off_wild_claim = _build("wild-claim", 100_000_000)  # an absurd, implausible claimed "before" price
    db.commit()

    score_deals(db, at=TODAY)
    db.commit()

    deal_no_claim = _get_deal(db, off_no_claim)
    deal_wild_claim = _get_deal(db, off_wild_claim)
    assert deal_no_claim is not None and deal_wild_claim is not None
    assert deal_no_claim["deal_score"] == deal_wild_claim["deal_score"]
    assert deal_no_claim["depth_vs_median_90d"] == deal_wild_claim["depth_vs_median_90d"]
    assert deal_no_claim["z_robust"] == deal_wild_claim["z_robust"]
    for d in (deal_no_claim, deal_wild_claim):
        assert "list_price_claimed" not in json.dumps(d["evidence"])


# --------------------------------------------------------------------------------------------------
# F3: an incomplete-price offer never enters ranking, and never gets its own deal_score.
# --------------------------------------------------------------------------------------------------


def test_price_incomplete_offer_excluded_from_ranking_and_scoring(db: Session) -> None:
    src_a, src_b, src_c = _make_source(db, "a"), _make_source(db, "b"), _make_source(db, "c")
    pid = _make_product(db, "F3 test")
    off_complete_1 = _make_offer(db, src_a, pid, "a-1")
    off_complete_2 = _make_offer(db, src_b, pid, "b-1")
    off_incomplete = _make_offer(db, src_c, pid, "c-1", shipping_cost=None)  # price_incomplete=True

    for off in (off_complete_1, off_complete_2):
        for d in range(1, 90):
            _write_price(db, off, d, 100_000)
    _write_price(db, off_complete_1, 0, 90_000)
    _write_price(db, off_complete_2, 0, 95_000)
    # A headline price far below the complete offers -- if it leaked into ranking, it would wrongly win "cheapest".
    _write_price(db, off_incomplete, 0, 10_000)
    db.commit()

    score_deals(db, at=TODAY)
    db.commit()

    assert _get_deal(db, off_incomplete) is None  # never gets its own deal row

    deal1 = _get_deal(db, off_complete_1)
    assert deal1 is not None
    assert deal1["price_rank_today"] == 1  # cheapest among *complete* offers, unaffected by the incomplete one
    assert off_incomplete not in deal1["evidence"]["competitor_offer_ids"]
    assert deal1["n_competing_offers"] == 2


# --------------------------------------------------------------------------------------------------
# Cross-shop cold start: a brand-new, multi-shop product with no history yet.
# --------------------------------------------------------------------------------------------------


def test_cross_shop_cold_start_basis_for_brand_new_product(db: Session) -> None:
    src_a, src_b = _make_source(db, "a"), _make_source(db, "b")
    pid = _make_product(db, "Brand new, multi-shop")
    off_a = _make_offer(db, src_a, pid, "a-1")
    off_b = _make_offer(db, src_b, pid, "b-1")
    _write_price(db, off_a, 0, 90_000)
    _write_price(db, off_b, 0, 100_000)
    db.commit()

    report = score_deals(db, at=TODAY)
    db.commit()

    assert report.basis_counts["cross_shop"] == 2
    deal_a = _get_deal(db, off_a)
    assert deal_a is not None
    assert deal_a["basis"] == "cross_shop"
    assert deal_a["coverage_cap"] <= 0.70
    assert deal_a["z_robust"] is None
    assert deal_a["evidence"]["s_z"] == 0.0


# --------------------------------------------------------------------------------------------------
# Performance: scoring 10 000 products must complete in under 60s on SQLite.
# --------------------------------------------------------------------------------------------------


def test_score_deals_10000_products_under_60_seconds(db: Session) -> None:
    n_products = 10_000
    n_sources = 5
    source_ids = [_make_source(db, f"perf-src-{i}") for i in range(n_sources)]
    db.commit()

    now_iso = NOW.isoformat()
    db.execute(
        text(
            "INSERT INTO product (canonical_title, brand, mpn, ean, category_id, created_at) "
            "VALUES (:t, NULL, NULL, NULL, NULL, :c)"
        ),
        [{"t": f"Perf product {i}", "c": now_iso} for i in range(n_products)],
    )
    db.commit()
    product_ids = [row[0] for row in db.execute(text("SELECT id FROM product ORDER BY id")).all()]
    assert len(product_ids) == n_products

    db.execute(
        text(
            """
            INSERT INTO offer (
                source_id, external_id, first_seen, last_seen, title, url,
                price_gross, shipping_cost, price_total, price_incomplete,
                condition, match_confidence, match_method, product_id, in_stock,
                is_bundle, currency, data_issues, evidence
            ) VALUES (
                :source_id, :external_id, :first_seen, :last_seen, :title, :url,
                :price_gross, :shipping_cost, :price_total, 0,
                'new', :match_confidence, 'ean', :product_id, 1,
                0, 'PLN', '[]', '{}'
            )
            """
        ),
        [
            {
                "source_id": source_ids[i % n_sources],
                "external_id": f"perf-{i}",
                "first_seen": now_iso,
                "last_seen": now_iso,
                "title": f"Perf offer {i}",
                "url": f"https://perf.example/{i}",
                "price_gross": 100_000,
                "shipping_cost": 0,
                "price_total": 100_000,
                "match_confidence": 0.99,
                "product_id": pid,
            }
            for i, pid in enumerate(product_ids)
        ],
    )
    db.commit()
    offer_ids = [row[0] for row in db.execute(text("SELECT id FROM offer ORDER BY id")).all()]
    assert len(offer_ids) == n_products

    price_rows: list[dict[str, Any]] = []
    for off_id in offer_ids:
        for d in range(4, -1, -1):  # 5 days of history: 4 normal days + today (a 20% drop)
            observed = NOW - timedelta(days=d)
            price_rows.append(
                {
                    "offer_id": off_id,
                    "observed_at": observed.isoformat(),
                    "price_total": 100_000 if d > 0 else 80_000,
                    "in_stock": 1,
                }
            )
    db.execute(
        text(
            "INSERT INTO price_point (offer_id, observed_at, price_total, in_stock) "
            "VALUES (:offer_id, :observed_at, :price_total, :in_stock)"
        ),
        price_rows,
    )
    db.commit()

    start = time.perf_counter()
    report = score_deals(db, at=TODAY)
    db.commit()
    elapsed = time.perf_counter() - start

    print(f"\n[perf] score_deals for {n_products} products took {elapsed:.2f}s (deals_scored={report.deals_scored})")
    assert report.deals_scored == n_products
    assert elapsed < 60.0, f"score_deals took {elapsed:.2f}s for {n_products} products, exceeding the 60s budget"
