"""Unit tests for union-find clustering and the post-hoc transitivity consistency check.

The scenario every test here is built around: A-B and B-C are each individually gate-safe edges,
but A and C directly conflict (e.g. different condition). Naive union-find would silently merge
A, B, and C into one cluster through B as a bridge -- exactly the silent failure mode the task spec
calls out by name ("scalenie przeszlo przechodnio A-B, B-C, ale A i C sie wykluczaja").
"""

from __future__ import annotations

from dealradar.matching.cascade import Edge
from dealradar.matching.clustering import build_clusters
from dealradar.matching.config import AttributeGateConfig
from dealradar.matching.records import OfferRecord

GATE_CFG = AttributeGateConfig(exact_keys=[], tolerance_pct_keys={})


def _record(offer_id: int, condition: str | None) -> OfferRecord:
    return OfferRecord(
        id=offer_id, source_id=1, title="t", title_norm="t", brand="B", brand_norm="b", mpn=None, mpn_norm=None,
        ean=None, category_id=1, condition=condition, is_bundle=False, bundle_size=None, price_total=1000,
        product_id=None, match_confidence=None, match_method=None, attrs={},
    )


def test_transitivity_conflict_splits_the_cluster() -> None:
    # A (new) -- fuzzy, low score -- B (unknown condition) -- fuzzy, low score -- C (refurb)
    # A and C directly conflict (new vs refurb) but were never compared to each other directly.
    a, b, c = _record(1, "new"), _record(2, None), _record(3, "refurb")
    edges = [
        Edge(offer_a=1, offer_b=2, method="fuzzy", confidence=0.60, score=0.60),
        Edge(offer_a=2, offer_b=3, method="fuzzy", confidence=0.62, score=0.62),
    ]
    result = build_clusters([1, 2, 3], edges, {1: a, 2: b, 3: c}, GATE_CFG)

    assert len(result.conflicts) == 1
    assert {result.conflicts[0].offer_a, result.conflicts[0].offer_b} == {1, 3}

    # A and C must end up in different components after the split.
    root_of = {member: root for root, members in result.components.items() for member in members}
    assert root_of[1] != root_of[3]


def test_split_removes_only_the_weakest_edge_on_the_path() -> None:
    # Same bridge scenario, but the A-B edge is a strong ean match and B-C is a weak fuzzy match;
    # splitting should remove the weaker (fuzzy) edge, keeping A and B together.
    a, b, c = _record(1, "new"), _record(2, None), _record(3, "refurb")
    edges = [
        Edge(offer_a=1, offer_b=2, method="ean", confidence=0.99, score=1.0),
        Edge(offer_a=2, offer_b=3, method="fuzzy", confidence=0.61, score=0.61),
    ]
    result = build_clusters([1, 2, 3], edges, {1: a, 2: b, 3: c}, GATE_CFG)

    root_of = {member: root for root, members in result.components.items() for member in members}
    assert root_of[1] == root_of[2], "the strong ean edge (1-2) should survive the split"
    assert root_of[3] != root_of[1], "the weak fuzzy edge (2-3) should be the one removed"


def test_no_conflict_means_no_split() -> None:
    a, b, c = _record(1, "new"), _record(2, "new"), _record(3, "new")
    edges = [
        Edge(offer_a=1, offer_b=2, method="fuzzy", confidence=0.7, score=0.7),
        Edge(offer_a=2, offer_b=3, method="fuzzy", confidence=0.7, score=0.7),
    ]
    result = build_clusters([1, 2, 3], edges, {1: a, 2: b, 3: c}, GATE_CFG)
    assert not result.conflicts
    assert len(result.components) == 1


def test_singleton_offers_form_their_own_component() -> None:
    a = _record(1, "new")
    result = build_clusters([1], [], {1: a}, GATE_CFG)
    assert result.components == {1: [1]}
    assert result.best_edge_for == {}
