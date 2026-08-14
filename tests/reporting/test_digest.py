"""Tests for dealradar.reporting.digest -- the daily report (Zadanie 3). Owner: A6.

Every scenario writes `deal`/`quality_score` rows directly (see conftest.py) so the exact evidence shown is
controlled -- these tests check that digest.py *displays* what's in the database faithfully, not that A4/A5's
formulas are correct (that is their own test suites' job).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from conftest import (
    NOW,
    make_category,
    make_deal,
    make_offer,
    make_price_point,
    make_product,
    make_quality_score,
    make_shop_credibility,
    make_source,
)

from dealradar.reporting.digest import build_digest, render_html, render_text


def test_empty_database_renders_all_five_sections_without_crashing(db: Session) -> None:
    digest = build_digest(db)
    text_out = render_text(digest)
    assert "UWAGI" in text_out
    assert "Top okazji" in text_out
    assert "Watchlista" in text_out
    assert "Wskaznik sciemy" in text_out
    assert "Stopka" in text_out
    html_out = render_html(digest, db)
    assert html_out.startswith("<!doctype html>")
    assert "prefers-color-scheme: dark" in html_out


def test_uwagi_section_is_always_first_even_when_empty(db: Session) -> None:
    digest = build_digest(db)
    out = render_text(digest)
    assert out.index("UWAGI") < out.index("Top okazji") < out.index("Watchlista") < out.index("Wskaznik sciemy") < out.index("Stopka")


def test_history_basis_deal_shows_median_and_quality_and_claimed_vs_real(db: Session) -> None:
    cat = make_category(db, "elektronika/dyski_ssd", "Dyski SSD")
    product_id = make_product(db, "Samsung 990 PRO 2TB M.2", category_id=cat)
    offer_id = make_offer(
        db, make_source(db, "x-kom"), product_id, "xk-1",
        title="Dysk SSD Samsung 990 PRO 2TB", price_gross=64900, shipping_cost=0,
        list_price_claimed=118000, warranty_months=60,
    )
    for days_ago in (5, 30, 60, 89):
        make_price_point(db, offer_id, days_ago, 87900)
    make_deal(
        db, offer_id, basis="history", deal_score=87.0, depth_vs_median_90d=0.26,
        n_competing_offers=7, n_sources=4, n_days_history=64, is_alltime_low=True, med=87900.0,
    )
    make_quality_score(db, product_id, total_1_10=8.1, peer_members=[(99, 60000), (98, 70000)], band=0.25, reference_price=65000)
    db.commit()

    digest = build_digest(db)
    assert len(digest.deals) == 1
    d = digest.deals[0]
    assert d.median_90d == 87900
    assert d.alltime_low_price is not None and d.alltime_low_price <= 87900
    assert d.is_alltime_low is True
    assert d.quality_total_1_10 == 8.1
    assert d.claimed_discount_pct is not None  # basis=history + list_price_claimed present -> shown
    assert d.real_discount_pct == 26.0

    out = render_text(digest)
    assert "649,00 zl" in out
    assert "mediana 90 dni" in out
    assert "nowe minimum" in out.lower() or "NOWE MINIMUM" in out
    assert "sklep deklaruje" in out
    assert "8,1/10" in out
    assert "x-kom" in out
    assert "gwarancja 60 mc" in out


def test_cross_shop_basis_is_visually_flagged_and_hides_claimed_vs_real(db: Session) -> None:
    product_id = make_product(db, "No-name gadget")
    offer_id = make_offer(db, make_source(db, "aliexpress"), product_id, "ali-1", price_gross=5000, shipping_cost=0, list_price_claimed=9000)
    make_deal(db, offer_id, basis="cross_shop", deal_score=55.0, depth_vs_median_90d=None, n_competing_offers=2, n_sources=1, n_days_history=0, med=None)
    db.commit()

    digest = build_digest(db)
    d = digest.deals[0]
    assert d.basis == "cross_shop"
    assert d.median_90d is None
    # Zadanie 3: claimed-vs-real is shown only when real history exists -- never for a cold start.
    assert d.claimed_discount_pct is None

    out = render_text(digest)
    assert "BEZ HISTORII" in out
    html_out = render_html(digest, db)
    assert "bez historii" in html_out.lower()


def test_missing_quality_score_shows_reason_never_zero_or_blank(db: Session) -> None:
    product_id = make_product(db, "Produkt bez formuly")
    offer_id = make_offer(db, make_source(db, "x-kom"), product_id, "xk-2", price_gross=10000, shipping_cost=0)
    make_deal(db, offer_id, basis="cross_shop", deal_score=40.0, n_competing_offers=2, n_sources=1, med=None)
    make_quality_score(db, product_id, total_1_10=None, missing_reason="no_utility_formula")
    db.commit()

    digest = build_digest(db)
    d = digest.deals[0]
    assert d.quality_total_1_10 is None
    assert d.quality_missing_reason == "no_utility_formula"

    out = render_text(digest)
    assert "brak oceny" in out
    assert "0/10" not in out  # never rendered as a fabricated zero (Z4)


def test_product_never_scored_at_all_also_shows_a_reason(db: Session) -> None:
    """A product with zero quality_score rows (never scored) must not render as a silent blank line."""
    product_id = make_product(db, "Nigdy nie oceniony")
    offer_id = make_offer(db, make_source(db, "x-kom"), product_id, "xk-3", price_gross=10000, shipping_cost=0)
    make_deal(db, offer_id, basis="cross_shop", deal_score=40.0, n_competing_offers=2, n_sources=1, med=None)
    db.commit()

    digest = build_digest(db)
    d = digest.deals[0]
    assert d.quality_total_1_10 is None
    assert d.quality_missing_reason is not None
    out = render_text(digest)
    assert "brak oceny" in out


def test_price_incomplete_offers_are_excluded_from_top_deals(db: Session) -> None:
    """F3: an offer without a known shipping cost (price_incomplete) never enters the ranking."""
    product_id = make_product(db, "Produkt bez ceny dostawy")
    offer_id = make_offer(db, make_source(db, "mediaexpert"), product_id, "me-1", price_gross=10000, shipping_cost=None)
    make_deal(db, offer_id, basis="cross_shop", deal_score=99.0, n_competing_offers=2, n_sources=1, med=None)
    db.commit()

    digest = build_digest(db)
    assert digest.deals == []


def test_top_deals_are_capped_and_sorted_by_deal_score_desc(db: Session) -> None:
    source_id = make_source(db, "x-kom")
    for i in range(5):
        product_id = make_product(db, f"Produkt {i}")
        offer_id = make_offer(db, source_id, product_id, f"xk-p{i}", price_gross=10000 + i, shipping_cost=0)
        make_deal(db, offer_id, basis="cross_shop", deal_score=float(i * 10), n_competing_offers=2, n_sources=1, med=None)
    db.commit()

    digest = build_digest(db, top_n=3)
    assert len(digest.deals) == 3
    scores = [d.deal_score for d in digest.deals]
    assert scores == sorted(scores, reverse=True)


def test_top_deals_dedupe_to_best_offer_per_product(db: Session) -> None:
    product_id = make_product(db, "Produkt z dwoma ofertami")
    o1 = make_offer(db, make_source(db, "x-kom"), product_id, "xk-a", price_gross=10000, shipping_cost=0)
    o2 = make_offer(db, make_source(db, "mediaexpert"), product_id, "me-a", price_gross=9500, shipping_cost=0)
    make_deal(db, o1, basis="cross_shop", deal_score=60.0, n_competing_offers=2, n_sources=2, med=None)
    make_deal(db, o2, basis="cross_shop", deal_score=75.0, n_competing_offers=2, n_sources=2, med=None)
    db.commit()

    digest = build_digest(db)
    assert len(digest.deals) == 1
    assert digest.deals[0].offer_id == o2  # the higher deal_score offer wins


def test_bullshit_index_ranks_worst_shops_first_with_sample_size(db: Session) -> None:
    honest = make_source(db, "honest-shop")
    dishonest = make_source(db, "dishonest-shop")
    make_shop_credibility(db, honest, claimed_avg=0.10, real_avg=0.08, n_offers=40)
    make_shop_credibility(db, dishonest, claimed_avg=0.55, real_avg=0.04, n_offers=25)
    db.commit()

    digest = build_digest(db)
    assert digest.bullshit[0].source_name == "dishonest-shop"
    assert digest.bullshit[0].n_offers == 25
    out = render_text(digest)
    assert "dishonest-shop" in out


def test_html_report_is_self_contained_and_has_no_external_resources(db: Session) -> None:
    digest = build_digest(db)
    html_out = render_html(digest, db)
    assert "http://" not in html_out
    assert "https://" not in html_out
    assert "<script src=" not in html_out
    assert "<link " not in html_out  # no external stylesheet


def test_html_report_writes_to_a_standalone_file(db: Session, tmp_path: Path) -> None:
    digest = build_digest(db)
    html_out = render_html(digest, db)
    out_path = tmp_path / "digest.html"
    out_path.write_text(html_out, encoding="utf-8")
    assert out_path.exists()
    assert out_path.read_text(encoding="utf-8").startswith("<!doctype html>")


def test_evidence_is_expandable_in_html_for_every_deal(db: Session) -> None:
    """Zadanie 3's overriding rule: every number must be checkable -- deal.evidence shown behind <details>."""
    product_id = make_product(db, "Produkt z dowodami")
    offer_id = make_offer(db, make_source(db, "x-kom"), product_id, "xk-ev", price_gross=10000, shipping_cost=0)
    make_deal(db, offer_id, basis="history", deal_score=70.0, n_competing_offers=5, n_sources=3, n_days_history=40, med=12000.0)
    db.commit()

    digest = build_digest(db)
    html_out = render_html(digest, db)
    assert "<details>" in html_out
    assert "dowody" in html_out
    # The evidence blob is HTML-escaped before embedding (safe by construction), so quotes come through
    # as &quot; -- this still proves the raw deal.evidence_json content is present, checkable, and correct.
    assert "n_competing_offers&quot;: 5" in html_out
