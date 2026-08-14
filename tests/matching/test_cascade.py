"""Unit tests for the three cascade steps in cascade.py, independent of the DB layer."""

from __future__ import annotations

from dealradar.matching.attrs import extract_numeric_attrs
from dealradar.matching.cascade import run_cascade
from dealradar.matching.config import load_matching_config
from dealradar.matching.records import OfferRecord
from dealradar.matching.review import ReviewOverrides

CFG = load_matching_config()


def _record(offer_id: int, **overrides: object) -> OfferRecord:
    """Builds an OfferRecord for cascade tests.

    `category_id` defaults to None (not some fixed id) so a test aimed at step 1 or step 2 does
    not accidentally also satisfy step 3's blocking requirement (brand_norm + category_id) and
    pick up an extra, unrelated fuzzy edge. `attrs` defaults to whatever
    extract_numeric_attrs(title_norm) would produce, matching how OfferRecord.from_row really
    builds it -- callers who need a *different* attrs set (or none) pass attrs= explicitly.
    """
    title_norm = overrides.get("title_norm", "t")
    base: dict[str, object] = dict(
        id=offer_id, source_id=1, title="t", title_norm=title_norm, brand="B", brand_norm="b", mpn=None, mpn_norm=None,
        ean=None, category_id=None, condition="new", is_bundle=False, bundle_size=None, price_total=10000,
        product_id=None, match_confidence=None, match_method=None,
        attrs=extract_numeric_attrs(title_norm if isinstance(title_norm, str) else None),
    )
    base.update(overrides)
    return OfferRecord(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------------
# Step 1: EAN
# --------------------------------------------------------------------------------------------------


def test_ean_step_merges_matching_ean_with_similar_titles() -> None:
    a = _record(1, ean="4006381333931", title_norm="dysk ssd samsung 990 pro 2tb", brand_norm="samsung")
    b = _record(2, ean="4006381333931", title_norm="ssd samsung 990 pro 2tb m.2", brand_norm="samsung")
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert len(result.edges) == 1
    assert result.edges[0].method == "ean"
    assert result.edges[0].confidence == CFG.ean.confidence
    assert not result.ean_conflicts


def test_ean_step_flags_conflict_when_titles_are_unrelated() -> None:
    # Same EAN (a shop pasted the manufacturer's code onto the wrong listing), but the titles have
    # nothing to do with each other -- must NOT merge, must be logged as an EAN conflict.
    a = _record(1, ean="4006381333931", title_norm="dysk ssd samsung 990 pro 2tb", brand_norm="samsung")
    b = _record(2, ean="4006381333931", title_norm="sluchawki bezprzewodowe bluetooth sportowe", brand_norm="samsung")
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert not result.edges
    assert len(result.ean_conflicts) == 1
    assert {result.ean_conflicts[0].offer_a, result.ean_conflicts[0].offer_b} == {1, 2}


def test_ean_step_flags_conflict_when_hard_gate_violated() -> None:
    # Same EAN, similar titles, but different condition class -- a new/refurb pair sharing the
    # manufacturer EAN. Hard gates apply to step 1 too (task spec).
    a = _record(1, ean="4006381333931", title_norm="laptop dell latitude 5420", condition="new")
    b = _record(2, ean="4006381333931", title_norm="laptop dell latitude 5420 poleasingowy", condition="refurb")
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert not result.edges
    assert len(result.ean_conflicts) == 1


def test_ean_step_ignores_offers_without_ean() -> None:
    a = _record(1, ean=None, title_norm="x")
    b = _record(2, ean=None, title_norm="x")
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert not result.edges
    assert not result.ean_conflicts


# --------------------------------------------------------------------------------------------------
# Step 2: brand + MPN
# --------------------------------------------------------------------------------------------------


def test_brand_mpn_step_merges_matching_pair() -> None:
    a = _record(1, brand_norm="sony", mpn_norm="WH1000XM5")
    b = _record(2, brand_norm="sony", mpn_norm="WH1000XM5")
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert len(result.edges) == 1
    assert result.edges[0].method == "brand_mpn"
    assert result.edges[0].confidence == CFG.brand_mpn.confidence


def test_brand_mpn_step_rejects_short_mpn() -> None:
    # "A1" is not a real identifier (task spec: reject MPNs shorter than 4 chars).
    a = _record(1, brand_norm="acme", mpn_norm="A1")
    b = _record(2, brand_norm="acme", mpn_norm="A1")
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert not result.edges


def test_brand_mpn_step_respects_hard_gate() -> None:
    a = _record(1, brand_norm="apple", mpn_norm="MACBOOKAIRM2", is_bundle=False)
    b = _record(2, brand_norm="apple", mpn_norm="MACBOOKAIRM2", is_bundle=True, bundle_size=2)
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert not result.edges


# --------------------------------------------------------------------------------------------------
# Step 3: blocking + fuzzy
# --------------------------------------------------------------------------------------------------


def test_fuzzy_step_requires_brand_and_category() -> None:
    a = _record(1, brand_norm=None, category_id=1, title_norm="powerbank xiaomi 10000mah")
    b = _record(2, brand_norm=None, category_id=1, title_norm="powerbank xiaomi 10000 mah")
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert not result.edges  # neither offer has a brand -- step 3 never even considers them


def test_fuzzy_step_respects_price_bucket() -> None:
    a = _record(1, brand_norm="kingston", category_id=1, title_norm="kingston nv2 1tb ssd m.2 pcie nvme", price_total=25900)
    b = _record(2, brand_norm="kingston", category_id=1, title_norm="kingston nv2 1tb dysk ssd m.2 pcie", price_total=100000)
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert not result.edges  # far outside the +/-35% price window despite near-identical titles


def test_fuzzy_step_merges_similar_titles_within_price_window() -> None:
    a = _record(1, brand_norm="kingston", category_id=1, title_norm="kingston nv2 1tb ssd m.2 pcie nvme", price_total=25900)
    b = _record(2, brand_norm="kingston", category_id=1, title_norm="kingston nv2 1tb dysk ssd m.2 pcie", price_total=24900)
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert len(result.edges) == 1
    assert result.edges[0].method == "fuzzy"
    assert CFG.fuzzy.min_confidence <= result.edges[0].confidence <= CFG.fuzzy.max_confidence


def test_fuzzy_step_blocked_by_g2_even_with_high_text_similarity() -> None:
    a = _record(1, brand_norm="samsung", category_id=1, title_norm="dysk ssd samsung 990 pro 512gb m.2 nvme", price_total=39900)
    b = _record(2, brand_norm="samsung", category_id=1, title_norm="dysk ssd samsung 990 pro 1tb m.2 nvme", price_total=45900)
    result = run_cascade([a, b], CFG, ReviewOverrides())
    assert not result.edges  # 512GB vs 1TB -- text is almost identical, G2 must reject anyway


# --------------------------------------------------------------------------------------------------
# match_review overrides
# --------------------------------------------------------------------------------------------------


def test_review_forbid_blocks_an_otherwise_accepted_ean_match() -> None:
    a = _record(1, ean="4006381333931", title_norm="dysk ssd samsung 990 pro 2tb")
    b = _record(2, ean="4006381333931", title_norm="ssd samsung 990 pro 2tb m.2")
    review = ReviewOverrides(forbidden=frozenset({(1, 2)}))
    result = run_cascade([a, b], CFG, review)
    assert not result.edges


def test_review_force_same_bypasses_a_hard_gate() -> None:
    a = _record(1, condition="new")
    b = _record(2, condition="refurb")
    review = ReviewOverrides(forced_same=frozenset({(1, 2)}))
    result = run_cascade([a, b], CFG, review)
    assert len(result.edges) == 1
    assert result.edges[0].method == "manual"
    assert result.edges[0].confidence == 1.0
