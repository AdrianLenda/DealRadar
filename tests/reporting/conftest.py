"""Shared fixture-building helpers for tests/reporting/.

Every helper here writes directly against the schema (no collector/normalizer/matcher/scorer involved),
same spirit as tests/scoring/test_deal.py and test_quality.py -- reporting/digest.py's job is to *display*
already-computed deal/quality_score/shop_credibility rows faithfully, so these tests control exactly what
those rows contain rather than depending on A4/A5's formulas producing a particular number.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.models import Offer, Source

NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def make_source(session: Session, name: str, *, risk_prior: float = 0.0) -> int:
    """Registers a source and returns its id."""
    return dealradar_db.upsert_source(
        session, Source(name=name, kind="feed", base_url=f"https://{name}.example", risk_prior=risk_prior)
    )


def make_category(session: Session, path: str, name: str, parent_id: int | None = None) -> int:
    """Inserts a category row and returns its id."""
    result = session.execute(
        text("INSERT INTO category (parent_id, name, path) VALUES (:parent_id, :name, :path) RETURNING id"),
        {"parent_id": parent_id, "name": name, "path": path},
    )
    return int(result.scalar_one())


def make_product(session: Session, title: str, category_id: int | None = None) -> int:
    """Inserts a product row and returns its id."""
    result = session.execute(
        text(
            "INSERT INTO product (canonical_title, brand, mpn, ean, category_id, created_at) "
            "VALUES (:t, NULL, NULL, NULL, :c, :ca) RETURNING id"
        ),
        {"t": title, "c": category_id, "ca": NOW.isoformat()},
    )
    return int(result.scalar_one())


def make_offer(
    session: Session,
    source_id: int,
    product_id: int | None,
    external_id: str,
    *,
    title: str = "Test offer",
    price_gross: int = 100000,
    shipping_cost: int | None = 0,
    list_price_claimed: int | None = None,
    warranty_months: int | None = None,
    match_confidence: float = 0.99,
) -> int:
    """Inserts an offer row (already matched to product_id, if given) and returns its id."""
    offer = Offer(
        source_id=source_id,
        external_id=external_id,
        first_seen=NOW,
        last_seen=NOW,
        title=title,
        url=f"https://test.example/{external_id}",
        price_gross=price_gross,
        shipping_cost=shipping_cost,
        condition="new",
        match_confidence=match_confidence,
        match_method="ean" if product_id is not None else None,
        product_id=product_id,
        list_price_claimed=list_price_claimed,
        warranty_months=warranty_months,
        in_stock=True,
    )
    return dealradar_db.upsert_offer(session, offer)


def make_deal(
    session: Session,
    offer_id: int,
    *,
    basis: str = "history",
    deal_score: float = 80.0,
    depth_vs_median_90d: float | None = 0.25,
    z_robust: float | None = 2.0,
    n_competing_offers: int = 3,
    n_days_history: int = 60,
    n_sources: int = 2,
    is_alltime_low: bool = False,
    med: float | None = 87900.0,
    computed_at: datetime = NOW,
) -> int:
    """Inserts a `deal` row with a realistic evidence_json (matching scoring.deal's own shape)."""
    evidence = {
        "basis": basis,
        "med": med,
        "mad": 500.0,
        "sigma": 1200.0,
        "n_history_points": n_days_history,
        "n_days_history": n_days_history,
        "history_date_range": [None, None],
        "competitor_offer_ids": [],
        "n_competing_offers": n_competing_offers,
        "n_sources": n_sources,
        "rank_today": 1,
    }
    result = session.execute(
        text(
            """
            INSERT INTO deal (
                offer_id, computed_at, basis, deal_score, depth_vs_median_90d, z_robust,
                price_rank_today, is_alltime_low, n_competing_offers, n_days_history,
                coverage_cap, evidence_json
            ) VALUES (
                :offer_id, :computed_at, :basis, :deal_score, :depth, :z,
                1, :is_atl, :n_competing, :n_days, 1.0, :evidence
            ) RETURNING id
            """
        ),
        {
            "offer_id": offer_id,
            "computed_at": computed_at.isoformat(),
            "basis": basis,
            "deal_score": deal_score,
            "depth": depth_vs_median_90d,
            "z": z_robust,
            "is_atl": int(is_alltime_low),
            "n_competing": n_competing_offers,
            "n_days": n_days_history,
            "evidence": json.dumps(evidence),
        },
    )
    return int(result.scalar_one())


def make_quality_score(
    session: Session,
    product_id: int,
    *,
    total_1_10: float | None = 8.1,
    missing_reason: str | None = None,
    peer_members: list[tuple[int, int]] | None = None,
    band: float = 0.25,
    reference_price: int = 65000,
    computed_at: datetime = NOW,
) -> int:
    """Inserts a `quality_score` row with a realistic evidence_json (peer_group shape matches scoring.quality)."""
    evidence: dict[str, object] = {}
    if peer_members is not None:
        evidence["peer_group"] = {
            "band": band,
            "confidence": "high",
            "reference_price": reference_price,
            "members": [{"product_id": pid, "price_total": price} for pid, price in peer_members],
        }
    result = session.execute(
        text(
            """
            INSERT INTO quality_score (
                product_id, computed_at, peer_group_id, value_score, reliability_score,
                deal_strength, risk_score, total_1_10, confidence, missing_reason, evidence_json
            ) VALUES (
                :product_id, :computed_at, 'test-peer-group', 0.8, 0.7, 0.6, 0.9,
                :total, :confidence, :missing_reason, :evidence
            ) RETURNING id
            """
        ),
        {
            "product_id": product_id,
            "computed_at": computed_at.isoformat(),
            "total": total_1_10,
            "confidence": "high" if total_1_10 is not None else None,
            "missing_reason": missing_reason,
            "evidence": json.dumps(evidence),
        },
    )
    return int(result.scalar_one())


def make_price_point(session: Session, offer_id: int, days_ago: int, price_total: int, *, in_stock: bool = True) -> None:
    """Writes a price_point `days_ago` days before NOW."""
    from datetime import timedelta

    observed = NOW - timedelta(days=days_ago)
    dealradar_db.append_price_point(session, offer_id, price_total=price_total, in_stock=in_stock, observed_at=observed)


def make_shop_credibility(
    session: Session, source_id: int, *, period: str = "2026-08", claimed_avg: float = 0.45, real_avg: float = 0.05, n_offers: int = 20
) -> None:
    """Inserts a shop_credibility row directly."""
    session.execute(
        text(
            """
            INSERT INTO shop_credibility (source_id, period, claimed_discount_avg, real_discount_avg, bullshit_index, n_offers)
            VALUES (:sid, :period, :claimed, :real, :bs, :n)
            """
        ),
        {"sid": source_id, "period": period, "claimed": claimed_avg, "real": real_avg, "bs": claimed_avg - real_avg, "n": n_offers},
    )


def insert_run_log(
    session: Session,
    stage: str,
    *,
    started_at: datetime,
    status: str = "ok",
    offers_fetched: int = 0,
    pct_with_ean: float | None = None,
    pct_matched: float | None = None,
    deals_scored: int = 0,
    sources_failed: list[str] | None = None,
    notes: list[str] | None = None,
) -> None:
    """Inserts a run_log row directly (bypassing db.run_report), for controlled health-check fixtures."""
    session.execute(
        text(
            """
            INSERT INTO run_log (
                stage, started_at, finished_at, offers_fetched, offers_new, pct_with_ean, pct_matched,
                products_new, deals_scored, quality_scored, sources_failed, duration_s, status, notes
            ) VALUES (
                :stage, :started_at, :started_at, :offers_fetched, 0, :pct_with_ean, :pct_matched,
                0, :deals_scored, 0, :sources_failed, 1.0, :status, :notes
            )
            """
        ),
        {
            "stage": stage,
            "started_at": started_at.isoformat(),
            "offers_fetched": offers_fetched,
            "pct_with_ean": pct_with_ean,
            "pct_matched": pct_matched,
            "deals_scored": deals_scored,
            "sources_failed": json.dumps(sources_failed or []),
            "status": status,
            "notes": json.dumps(notes or []),
        },
    )
