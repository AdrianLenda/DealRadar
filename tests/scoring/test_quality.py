"""Tests for dealradar.scoring.quality -- quality_score (ARCHITEKTURA.md section 9). Owner: A5.

Synthetic fixtures throughout (no real collector/matcher output), same approach as A4's test_deal.py: every
Product/ProductAttr/Offer/Deal/Source row is built directly against the schema so the statistical behavior
(bayesian shrinkage, peer-group widening, the AST evaluator's whitelist) is tested deterministically.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.models import Offer, Source
from dealradar.scoring.quality import (
    AGREEMENT_BONUS_AMOUNT,
    PEER_GROUP_MAX_BAND,
    PEER_GROUP_MIN_BAND,
    CategoryFormula,
    ComponentWeights,
    FormulaError,
    QualityReport,
    _percentile,
    _ProductOfferSignals,
    _reliability_raw,
    _score_bucket,
    build_peer_group,
    compile_formula,
    evaluate_formula,
    formula_coverage,
    load_category_formulas,
    score_quality,
    utility_for,
)

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
CATEGORIES_DIR = Path(__file__).resolve().parents[2] / "config" / "categories"


# --------------------------------------------------------------------------------------------------
# Fixture helpers -- synthetic Source/Category/Product/ProductAttr/Offer/Deal, built directly (no
# collector/normalizer/matcher involved), same spirit as tests/scoring/test_deal.py.
# --------------------------------------------------------------------------------------------------


def _make_source(session: Session, name: str, *, risk_prior: float = 0.0) -> int:
    return dealradar_db.upsert_source(
        session, Source(name=name, kind="feed", base_url=f"https://{name}.example", risk_prior=risk_prior)
    )


def _make_category(session: Session, path: str, name: str, parent_id: int | None = None) -> int:
    result = session.execute(
        text("INSERT INTO category (parent_id, name, path) VALUES (:parent_id, :name, :path) RETURNING id"),
        {"parent_id": parent_id, "name": name, "path": path},
    )
    return int(result.scalar_one())


def _ssd_category(session: Session) -> int:
    """Inserts the elektronika/dyski_ssd leaf category (a formula exists for it) and returns its id."""
    root = _make_category(session, "elektronika", "Elektronika")
    return _make_category(session, "elektronika/dyski_ssd", "Dyski SSD", parent_id=root)


def _make_product(session: Session, title: str, category_id: int | None) -> int:
    result = session.execute(
        text(
            "INSERT INTO product (canonical_title, brand, mpn, ean, category_id, created_at) "
            "VALUES (:t, NULL, NULL, NULL, :c, :ca) RETURNING id"
        ),
        {"t": title, "c": category_id, "ca": NOW.isoformat()},
    )
    return int(result.scalar_one())


def _set_attrs(session: Session, product_id: int, attrs: dict[str, float]) -> None:
    for key, value in attrs.items():
        session.execute(
            text(
                'INSERT INTO product_attr (product_id, "key", value_num, value_text, unit, extracted_from) '
                "VALUES (:pid, :key, :val, NULL, NULL, 'title')"
            ),
            {"pid": product_id, "key": key, "val": value},
        )


def _make_offer(
    session: Session,
    source_id: int,
    product_id: int | None,
    external_id: str,
    *,
    price_gross: int,
    shipping_cost: int | None = 0,
    condition: str = "new",
    is_bundle: bool = False,
    in_stock: bool | None = True,
    rating_avg: float | None = None,
    rating_count: int | None = None,
    warranty_months: int | None = None,
    seller_rating: float | None = None,
) -> int:
    offer = Offer(
        source_id=source_id,
        external_id=external_id,
        first_seen=NOW,
        last_seen=NOW,
        title=f"Test offer {external_id}",
        url=f"https://test.example/{external_id}",
        price_gross=price_gross,
        shipping_cost=shipping_cost,
        condition=condition,  # type: ignore[arg-type]
        is_bundle=is_bundle,
        in_stock=in_stock,
        rating_avg=rating_avg,
        rating_count=rating_count,
        warranty_months=warranty_months,
        seller_rating=seller_rating,
        match_confidence=0.99,
        match_method="ean",
        product_id=product_id,
    )
    offer_id = dealradar_db.upsert_offer(session, offer)
    if product_id is not None:
        session.execute(
            text("UPDATE offer SET category_id = (SELECT category_id FROM product WHERE id = :pid) WHERE id = :oid"),
            {"pid": product_id, "oid": offer_id},
        )
    return offer_id


def _make_deal(session: Session, offer_id: int, deal_score: float, *, computed_at: datetime = NOW) -> None:
    session.execute(
        text(
            "INSERT INTO deal (offer_id, computed_at, basis, deal_score) "
            "VALUES (:offer_id, :computed_at, 'history', :deal_score)"
        ),
        {"offer_id": offer_id, "computed_at": computed_at.isoformat(), "deal_score": deal_score},
    )


def _quality_row(session: Session, product_id: int) -> dict[str, Any]:
    row = session.execute(
        text("SELECT * FROM quality_score WHERE product_id = :pid ORDER BY id DESC LIMIT 1"), {"pid": product_id}
    ).mappings().first()
    assert row is not None, f"no quality_score row written for product {product_id}"
    return dict(row)


# --------------------------------------------------------------------------------------------------
# Safe AST expression evaluator (Zadanie 2: whitelist, no eval())
# --------------------------------------------------------------------------------------------------


def test_evaluator_accepts_whitelisted_arithmetic_and_norm() -> None:
    tree = compile_formula("(capacity_gb / 1024) * (1 + 0.3 * norm(read_mbs, 500, 7000))")
    value = evaluate_formula(tree, {"capacity_gb": 2048.0, "read_mbs": 3750.0})
    expected = (2048.0 / 1024) * (1 + 0.3 * ((3750.0 - 500.0) / (7000.0 - 500.0)))
    assert value == pytest.approx(expected)


def test_evaluator_missing_optional_variable_defaults_to_zero_not_a_fabricated_bonus() -> None:
    tree = compile_formula("capacity_gb / 1024 * (1 + 0.3 * norm(read_mbs, 500, 7000))")
    value = evaluate_formula(tree, {"capacity_gb": 1024.0})  # read_mbs absent
    assert value == pytest.approx(1.0)  # bonus factor collapses to (1 + 0.3*0) = 1, not a boosted number


def test_evaluator_supports_min_max_log() -> None:
    assert evaluate_formula(compile_formula("min(a, b)"), {"a": 3.0, "b": 5.0}) == 3.0
    assert evaluate_formula(compile_formula("max(a, b)"), {"a": 3.0, "b": 5.0}) == 5.0
    assert evaluate_formula(compile_formula("log(a)"), {"a": 1.0}) == pytest.approx(0.0)


def test_evaluator_rejects_dunder_import() -> None:
    with pytest.raises(FormulaError):
        compile_formula("__import__('os').system('echo hi')")


def test_evaluator_rejects_attribute_access() -> None:
    with pytest.raises(FormulaError):
        compile_formula("os.system('rm -rf /')")


def test_evaluator_rejects_call_outside_whitelist() -> None:
    with pytest.raises(FormulaError):
        compile_formula("open('/etc/passwd').read()")
    with pytest.raises(FormulaError):
        compile_formula("eval('1+1')")


def test_evaluator_rejects_string_literals_and_subscripts() -> None:
    with pytest.raises(FormulaError):
        compile_formula("'a' + 'b'")
    with pytest.raises(FormulaError):
        compile_formula("attrs[0]")


def test_utility_for_returns_none_when_required_attr_missing() -> None:
    formula = CategoryFormula(
        slug="test",
        required_attrs=("capacity_gb",),
        weights=ComponentWeights(value=1.0, reliability=0.0, deal=0.0, risk=0.0),
        compiled_utility=compile_formula("capacity_gb"),
        source_path=Path("test.yaml"),
    )
    assert utility_for(formula, {}) is None
    assert utility_for(formula, {"capacity_gb": 512.0}) == 512.0


def test_all_six_category_yaml_files_load_and_compile() -> None:
    formulas = load_category_formulas(CATEGORIES_DIR)
    expected = {"dyski_ssd", "monitory", "powerbanki", "laptopy", "telefony", "sluchawki"}
    assert expected <= formulas.keys()
    for slug, formula in formulas.items():
        weights = formula.weights.as_dict()
        assert sum(weights.values()) == pytest.approx(1.0), f"{slug}: weights must sum to 1.0, got {weights}"


# --------------------------------------------------------------------------------------------------
# Reliability: bayesian shrinkage (F4 defense) -- the literal "5.0 z trzech opinii nizej niz 4.6 z
# dwoch tysiecy" requirement, plus the cross-shop agreement/disagreement modifier.
# --------------------------------------------------------------------------------------------------


def _signals(ratings: list[tuple[int, float, int]]) -> _ProductOfferSignals:
    return _ProductOfferSignals(ratings=ratings, warranty_months=[], seller_ratings=[], risk_priors=[], condition="new")


def test_bayesian_shrinkage_favors_many_reviews_over_few_perfect_ones() -> None:
    m = 4.5
    few_perfect = _reliability_raw(_signals([(1, 5.0, 3)]), m)
    many_good = _reliability_raw(_signals([(1, 4.6, 2000)]), m)
    assert few_perfect is not None and many_good is not None
    assert few_perfect.shrunk_rating < many_good.shrunk_rating, (few_perfect, many_good)
    # And matches the literal formula from ARCHITEKTURA.md sec. 9: r* = (C*m + sum_r) / (C + n)
    assert few_perfect.shrunk_rating == pytest.approx((30.0 * m + 5.0 * 3) / (30.0 + 3))
    assert many_good.shrunk_rating == pytest.approx((30.0 * m + 4.6 * 2000) / (30.0 + 2000))


def test_reliability_none_when_no_ratings_at_all() -> None:
    assert _reliability_raw(_signals([]), 4.0) is None


def test_cross_shop_disagreement_penalizes_the_higher_reading() -> None:
    m = 4.0
    signals = _signals([(1, 4.9, 100), (2, 3.9, 100)])  # spread = 1.0 > 0.8 disagreement threshold
    result = _reliability_raw(signals, m)
    assert result is not None
    assert result.modifier == "disagreement_penalty"
    # The higher-average source (source 1, 4.9) must have been dropped before shrinking:
    expected_after_drop = (30.0 * m + 3.9 * 100) / (30.0 + 100)
    assert result.shrunk_rating == pytest.approx(expected_after_drop)
    naive_pooled = (30.0 * m + 4.9 * 100 + 3.9 * 100) / (30.0 + 200)
    assert result.shrunk_rating < naive_pooled, "the higher reading must be penalized, not pooled in"


def test_cross_shop_agreement_within_threshold_gets_a_bonus() -> None:
    m = 4.0
    signals = _signals([(1, 4.5, 50), (2, 4.3, 50)])  # spread = 0.2 <= 0.4 agreement threshold
    result = _reliability_raw(signals, m)
    assert result is not None
    assert result.modifier == "agreement_bonus"
    plain = (30.0 * m + 4.5 * 50 + 4.3 * 50) / (30.0 + 100)
    assert result.shrunk_rating == pytest.approx(min(5.0, plain + AGREEMENT_BONUS_AMOUNT))
    assert result.shrunk_rating > plain


def test_single_source_gets_no_agreement_or_disagreement_modifier() -> None:
    result = _reliability_raw(_signals([(1, 4.7, 40)]), 4.0)
    assert result is not None
    assert result.modifier == "none"


# --------------------------------------------------------------------------------------------------
# Percentile helper
# --------------------------------------------------------------------------------------------------


def test_percentile_mid_rank_handles_ties_and_lone_value() -> None:
    assert _percentile(5.0, [5.0]) == 0.5  # no comparison possible -> neutral, not fabricated 1.0
    assert _percentile(1.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.1)  # lowest of 5
    assert _percentile(5.0, [1.0, 2.0, 3.0, 4.0, 5.0]) == pytest.approx(0.9)  # highest of 5
    assert _percentile(3.0, [1.0, 3.0, 3.0, 3.0, 5.0]) == pytest.approx(0.5)  # tied mid-rank


def test_score_bucket_boundaries() -> None:
    assert _score_bucket(1.0) == "1-2"
    assert _score_bucket(9.99) == "9-10"
    assert _score_bucket(10.0) == "9-10"
    assert _score_bucket(5.4) == "5-6"


# --------------------------------------------------------------------------------------------------
# Peer group (Zadanie 1): band widening from +/-25% to +/-60%, and stopping there.
# --------------------------------------------------------------------------------------------------


def test_peer_group_widens_band_to_reach_five_members_then_stops_widening(db: Session) -> None:
    cat = _ssd_category(db)
    src = _make_source(db, "shop")
    p = _make_product(db, "P", cat)
    _make_offer(db, src, p, "p-1", price_gross=100_000)

    # 2 members within +/-25% (5%, -5%); 3 more only within +/-35% (32%, 33%, 34%) -- not +/-25% or +/-30%.
    for i, price in enumerate([105_000, 95_000]):
        pid = _make_product(db, f"close-{i}", cat)
        _make_offer(db, src, pid, f"close-{i}-1", price_gross=price)
    for i, price in enumerate([132_000, 133_000, 134_000]):
        pid = _make_product(db, f"far-{i}", cat)
        _make_offer(db, src, pid, f"far-{i}-1", price_gross=price)
    db.commit()

    group = build_peer_group(db, p)
    assert group is not None
    assert group.band == pytest.approx(0.35)  # widened past 0.25 and 0.30 (still <5 members) to reach 5 at 0.35
    assert len(group.members) == 5
    assert group.confidence == "medium"  # >=5 members, but only after widening


def test_peer_group_stops_at_max_band_when_still_below_target(db: Session) -> None:
    cat = _ssd_category(db)
    src = _make_source(db, "shop")
    p = _make_product(db, "P", cat)
    _make_offer(db, src, p, "p-1", price_gross=100_000)

    # Only 2 candidates, both requiring the widest band (+50%), and never reaching the 5-member target.
    for i, price in enumerate([150_000, 55_000]):
        pid = _make_product(db, f"far-{i}", cat)
        _make_offer(db, src, pid, f"far-{i}-1", price_gross=price)
    db.commit()

    group = build_peer_group(db, p)
    assert group is not None
    assert group.band == pytest.approx(PEER_GROUP_MAX_BAND)  # widened all the way to +/-60% and stopped there
    assert group.band <= PEER_GROUP_MAX_BAND + 1e-9
    assert len(group.members) == 2  # still short of 5 -- widening does not overshoot 60% chasing more
    assert group.confidence == "low"


def test_peer_group_excludes_bundle_and_refurb_offers_from_price_band(db: Session) -> None:
    cat = _ssd_category(db)
    src = _make_source(db, "shop")
    p = _make_product(db, "P", cat)
    _make_offer(db, src, p, "p-1", price_gross=100_000)

    # A refurb "competitor" and a bundle "competitor", both artificially cheap -- must not appear as members
    # even though their raw price would fall inside the band (the exact trap named in the task spec).
    cheap_refurb = _make_product(db, "cheap-refurb", cat)
    _make_offer(db, src, cheap_refurb, "refurb-1", price_gross=30_000, condition="refurb")
    cheap_bundle = _make_product(db, "cheap-bundle", cat)
    _make_offer(db, src, cheap_bundle, "bundle-1", price_gross=30_000, is_bundle=True)
    genuinely_cheap_but_not_new_enough_offers = _make_product(db, "out-of-stock", cat)
    _make_offer(db, src, genuinely_cheap_but_not_new_enough_offers, "oos-1", price_gross=100_000, in_stock=False)
    db.commit()

    group = build_peer_group(db, p)
    assert group is not None
    assert group.members == []  # none of the three traps qualify


def test_build_peer_group_returns_none_without_category_or_reference_price(db: Session) -> None:
    src = _make_source(db, "shop")
    no_category = _make_product(db, "no-cat", None)
    _make_offer(db, src, no_category, "nc-1", price_gross=10_000)
    db.commit()
    assert build_peer_group(db, no_category) is None

    cat = _ssd_category(db)
    no_offers = _make_product(db, "no-offers", cat)
    db.commit()
    assert build_peer_group(db, no_offers) is None

    assert build_peer_group(db, 999_999) is None  # product does not exist


# --------------------------------------------------------------------------------------------------
# score_quality / gate G3
# --------------------------------------------------------------------------------------------------


def _five_peers(db: Session, cat: int, src: int, center_price: int = 100_000) -> None:
    """Creates 5 peer products within +/-10% of center_price, each with capacity_gb set, for a clean pass gate."""
    for i, offset in enumerate([-0.10, -0.05, 0.0, 0.05, 0.10]):
        pid = _make_product(db, f"peer-{i}", cat)
        price = int(center_price * (1 + offset))
        _make_offer(db, src, pid, f"peer-{i}-1", price_gross=price)
        _set_attrs(db, pid, {"capacity_gb": 1024.0})


def test_missing_reason_no_utility_formula_for_unmapped_category(db: Session) -> None:
    root = _make_category(db, "elektronika", "Elektronika")
    unmapped = _make_category(db, "elektronika/zabawki", "Zabawki", parent_id=root)  # no config/categories/*.yaml
    src = _make_source(db, "shop")
    p = _make_product(db, "Zabawka", unmapped)
    _make_offer(db, src, p, "p-1", price_gross=5_000)
    db.commit()

    report = score_quality(db, [p], config_dir=CATEGORIES_DIR)
    db.commit()

    row = _quality_row(db, p)
    assert row["total_1_10"] is None
    assert row["missing_reason"] == "no_utility_formula"
    assert report.missing_by_reason == {"no_utility_formula": 1}
    assert report.products_scored == 0


def test_missing_reason_no_utility_formula_when_category_id_is_none(db: Session) -> None:
    src = _make_source(db, "shop")
    p = _make_product(db, "Uncategorized", None)
    _make_offer(db, src, p, "p-1", price_gross=5_000)
    db.commit()
    score_quality(db, [p], config_dir=CATEGORIES_DIR)
    db.commit()
    row = _quality_row(db, p)
    assert row["total_1_10"] is None
    assert row["missing_reason"] == "no_utility_formula"


def test_missing_reason_peer_group_too_small(db: Session) -> None:
    cat = _ssd_category(db)
    src = _make_source(db, "shop")
    p = _make_product(db, "P", cat)
    _make_offer(db, src, p, "p-1", price_gross=100_000)
    _set_attrs(db, p, {"capacity_gb": 1024.0})
    # Only 2 same-category competitors even at the widest band -- below PEER_GROUP_MIN_MEMBERS=3.
    for i in range(2):
        pid = _make_product(db, f"peer-{i}", cat)
        _make_offer(db, src, pid, f"peer-{i}-1", price_gross=100_000)
    db.commit()

    report = score_quality(db, [p], config_dir=CATEGORIES_DIR)
    db.commit()

    row = _quality_row(db, p)
    assert row["total_1_10"] is None
    assert row["missing_reason"] == "peer_group_too_small"
    assert report.missing_by_reason == {"peer_group_too_small": 1}


def test_missing_reason_insufficient_signals(db: Session) -> None:
    cat = _ssd_category(db)
    src = _make_source(db, "shop", risk_prior=0.1)
    p = _make_product(db, "P", cat)
    # P: priced, but NO product_attr (-> value None), NO rating (-> reliability None), NO deal row
    # (-> deal None). Only risk is available (source risk_prior always present) -- 1 of 4, below the
    # G3 "insufficient_signals" threshold of 2.
    _make_offer(db, src, p, "p-1", price_gross=100_000)
    _five_peers(db, cat, src)
    db.commit()

    report = score_quality(db, [p], config_dir=CATEGORIES_DIR)
    db.commit()

    row = _quality_row(db, p)
    assert row["total_1_10"] is None
    assert row["missing_reason"] == "insufficient_signals"
    assert row["risk_score"] is not None  # the one signal that WAS available is still recorded
    assert row["value_score"] is None
    assert row["reliability_score"] is None
    assert row["deal_strength"] is None
    assert report.missing_by_reason == {"insufficient_signals": 1}


def test_value_component_favors_the_cheaper_product_given_identical_attrs(db: Session) -> None:
    """Zadanie 2.2: 'wartosc' is utility PER ZLOTY -- identical specs, different price -> cheaper scores higher."""
    cat = _ssd_category(db)
    src = _make_source(db, "shop", risk_prior=0.0)

    cheap = _make_product(db, "Cheap 1TB SSD", cat)
    _make_offer(db, src, cheap, "cheap-1", price_gross=80_000)
    _set_attrs(db, cheap, {"capacity_gb": 1024.0})

    pricey = _make_product(db, "Pricey 1TB SSD", cat)
    _make_offer(db, src, pricey, "pricey-1", price_gross=160_000)
    _set_attrs(db, pricey, {"capacity_gb": 1024.0})  # identical attrs to `cheap`

    # 3 more same-category peers, at a third price point, purely to satisfy the >=3-members gate.
    for i in range(3):
        pid = _make_product(db, f"filler-{i}", cat)
        _make_offer(db, src, pid, f"filler-{i}-1", price_gross=120_000)
        _set_attrs(db, pid, {"capacity_gb": 512.0})

    db.commit()
    score_quality(db, [cheap, pricey], config_dir=CATEGORIES_DIR)
    db.commit()

    cheap_row = _quality_row(db, cheap)
    pricey_row = _quality_row(db, pricey)
    assert cheap_row["value_score"] is not None and pricey_row["value_score"] is not None
    assert cheap_row["value_score"] > pricey_row["value_score"], (cheap_row, pricey_row)


def test_total_1_10_is_rounded_to_one_decimal_and_within_bounds(db: Session) -> None:
    cat = _ssd_category(db)
    src_a = _make_source(db, "shop-a", risk_prior=0.05)
    src_b = _make_source(db, "shop-b", risk_prior=0.1)

    p = _make_product(db, "P", cat)
    off_p = _make_offer(
        db, src_a, p, "p-1", price_gross=100_000, rating_avg=4.6, rating_count=500, warranty_months=24, seller_rating=4.8
    )
    _set_attrs(db, p, {"capacity_gb": 2048.0})
    _make_deal(db, off_p, 72.3)

    for i in range(5):
        pid = _make_product(db, f"peer-{i}", cat)
        price = 100_000 + i * 2_000
        off = _make_offer(
            db, src_b, pid, f"peer-{i}-1", price_gross=price, rating_avg=4.2, rating_count=100, warranty_months=12
        )
        _set_attrs(db, pid, {"capacity_gb": 1024.0})
        _make_deal(db, off, 30.0 + i)

    db.commit()
    report = score_quality(db, [p], config_dir=CATEGORIES_DIR)
    db.commit()

    row = _quality_row(db, p)
    assert row["total_1_10"] is not None
    total = row["total_1_10"]
    assert 1.0 <= total <= 10.0
    assert round(total, 1) == total, f"total_1_10 must already be rounded to 1 decimal, got {total}"
    assert row["missing_reason"] is None
    assert row["confidence"] in ("high", "medium", "low")
    assert report.products_scored == 1
    assert report.score_distribution


def test_every_product_in_scope_gets_a_row_even_with_no_formula_and_no_data(db: Session) -> None:
    """Z4: a product without a score must still appear in the results, not silently disappear."""
    p = _make_product(db, "totally bare product", None)
    db.commit()
    report = score_quality(db, [p], config_dir=CATEGORIES_DIR)
    row = _quality_row(db, p)
    assert row["total_1_10"] is None
    assert row["missing_reason"] == "no_utility_formula"
    assert report.products_considered == 1
    assert report.products_missing == 1


def test_quality_report_headline_flags_majority_missing_as_main_finding() -> None:
    report = QualityReport(
        computed_at=NOW,
        products_considered=10,
        products_scored=2,
        products_missing=8,
        missing_by_reason={"no_utility_formula": 8},
    )
    assert report.pct_missing == pytest.approx(80.0)
    assert "MAIN FINDING" in report.headline()
    assert "80.0%" in report.headline()


def test_quality_report_headline_no_main_finding_when_coverage_is_good() -> None:
    report = QualityReport(computed_at=NOW, products_considered=10, products_scored=9, products_missing=1)
    assert "MAIN FINDING" not in report.headline()


def test_formula_coverage_reports_offer_counts_per_leaf_category(db: Session) -> None:
    cat = _ssd_category(db)  # also creates the "elektronika" root category
    unmapped = _make_category(db, "elektronika/zabawki", "Zabawki", parent_id=None)
    src = _make_source(db, "shop")
    p1 = _make_product(db, "P1", cat)
    _make_offer(db, src, p1, "p1-1", price_gross=1000)
    p2 = _make_product(db, "P2", unmapped)
    _make_offer(db, src, p2, "p2-1", price_gross=1000)
    _make_offer(db, src, p2, "p2-2", price_gross=1000)
    db.commit()

    coverage = formula_coverage(db, config_dir=CATEGORIES_DIR)
    by_path = {c.category_path: c for c in coverage}
    assert by_path["elektronika/dyski_ssd"].has_formula is True
    assert by_path["elektronika/dyski_ssd"].n_offers == 1
    assert by_path["elektronika/zabawki"].has_formula is False
    assert by_path["elektronika/zabawki"].n_offers == 2
