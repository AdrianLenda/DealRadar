"""The three-step matching cascade -- ARCHITEKTURA.md section 7, implemented in the exact order the
task spec mandates: EAN exact (0.99) -> brand+MPN (0.95) -> blocking + fuzzy (0.60-0.85). Owner: A3.

Each step only ever *proposes* an edge between two offers; hard gates (gates.py) and match_review
overrides (review.py) can veto a pair at every step, including steps 1 and 2 ("Bramki obowiazuja
takze krok 1 i 2"). Building the final clusters from these edges is clustering.py's job -- this
module's output is a flat list of accepted edges plus the EAN-conflict pairs logged for the
"garbage EAN in feed" quality metric.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass

from dealradar.matching.blocking import group_for_step3, price_compatible
from dealradar.matching.config import MatchingConfig
from dealradar.matching.gates import hard_gate_conflict
from dealradar.matching.records import OfferRecord
from dealradar.matching.review import ReviewOverrides
from dealradar.matching.similarity import combined_score, tfidf_cosine_matrix, title_similarity

MatchMethodLiteral = str  # narrowed to models.MatchMethod values at the edges of this package

# Higher rank = a stronger edge to keep when a cluster must be split (clustering.py removes the
# weakest edge first). 'manual' (a human "same" label) is the strongest and last resort to remove.
METHOD_RANK: dict[str, int] = {"fuzzy": 0, "brand_mpn": 1, "ean": 2, "manual": 3}


@dataclass(frozen=True)
class Edge:
    """One accepted candidate-pair merge, produced by exactly one cascade step."""

    offer_a: int
    offer_b: int
    method: str
    confidence: float
    score: float
    """The raw signal that produced this edge (title similarity for EAN, combined_score for fuzzy,
    1.0 for brand_mpn/manual) -- used only to rank edges when a cluster must be split."""


@dataclass(frozen=True)
class EanConflict:
    """A pair sharing an EAN that was deliberately NOT merged -- the "garbage EAN in feed" signal."""

    offer_a: int
    offer_b: int
    ean: str
    reason: str


@dataclass(frozen=True)
class CascadeResult:
    """Everything the cascade produced: accepted edges plus the pairs it explicitly refused to merge."""

    edges: list[Edge]
    ean_conflicts: list[EanConflict]


def _step1_ean(records: list[OfferRecord], cfg: MatchingConfig, review: ReviewOverrides) -> tuple[list[Edge], list[EanConflict]]:
    """Returns accepted edges and logged conflicts for step 1: exact EAN match, confidence cfg.ean.confidence."""
    by_ean: dict[str, list[OfferRecord]] = defaultdict(list)
    for record in records:
        if record.ean:
            by_ean[record.ean].append(record)

    edges: list[Edge] = []
    conflicts: list[EanConflict] = []
    for ean, group in by_ean.items():
        for a, b in itertools.combinations(group, 2):
            if review.forbids(a.id, b.id):
                continue
            gate_reason = hard_gate_conflict(a, b, cfg.attribute_gate)
            if gate_reason is not None:
                conflicts.append(EanConflict(a.id, b.id, ean, gate_reason))
                continue
            sim = title_similarity(a.title_norm, b.title_norm)
            if sim < cfg.ean.conflict_title_sim_min:
                conflicts.append(
                    EanConflict(a.id, b.id, ean, f"title_sim={sim:.2f} < {cfg.ean.conflict_title_sim_min}")
                )
                continue
            edges.append(Edge(a.id, b.id, "ean", cfg.ean.confidence, sim))
    return edges, conflicts


def _step2_brand_mpn(records: list[OfferRecord], cfg: MatchingConfig, review: ReviewOverrides) -> list[Edge]:
    """Returns accepted edges for step 2: (brand_norm, mpn_norm) match, confidence cfg.brand_mpn.confidence."""
    by_brand_mpn: dict[tuple[str, str], list[OfferRecord]] = defaultdict(list)
    for record in records:
        if record.brand_norm and record.mpn_norm and len(record.mpn_norm) >= cfg.brand_mpn.mpn_min_length:
            by_brand_mpn[(record.brand_norm, record.mpn_norm)].append(record)

    edges: list[Edge] = []
    for group in by_brand_mpn.values():
        for a, b in itertools.combinations(group, 2):
            if review.forbids(a.id, b.id):
                continue
            if hard_gate_conflict(a, b, cfg.attribute_gate) is not None:
                continue
            edges.append(Edge(a.id, b.id, "brand_mpn", cfg.brand_mpn.confidence, 1.0))
    return edges


def _step3_fuzzy(records: list[OfferRecord], cfg: MatchingConfig, review: ReviewOverrides) -> list[Edge]:
    """Returns accepted edges for step 3: blocked fuzzy match, confidence scaled into [min_confidence, max_confidence]."""
    edges: list[Edge] = []
    groups = group_for_step3(records)
    span = max(1e-9, 1.0 - cfg.fuzzy.accept_threshold)

    for group in groups.values():
        if len(group) < 2:
            continue
        titles = [r.title_norm or "" for r in group]
        sim_matrix = tfidf_cosine_matrix(titles)
        for i, j in itertools.combinations(range(len(group)), 2):
            a, b = group[i], group[j]
            if review.forbids(a.id, b.id):
                continue
            if not price_compatible(a.price_total, b.price_total, cfg.fuzzy.price_bucket_pct):
                continue
            if hard_gate_conflict(a, b, cfg.attribute_gate) is not None:
                continue
            score = combined_score(float(sim_matrix[i][j]), a, b, cfg.fuzzy, cfg.attribute_gate)
            if score >= cfg.fuzzy.accept_threshold:
                headroom = min(1.0, (score - cfg.fuzzy.accept_threshold) / span)
                confidence = cfg.fuzzy.min_confidence + (cfg.fuzzy.max_confidence - cfg.fuzzy.min_confidence) * headroom
                edges.append(Edge(a.id, b.id, "fuzzy", confidence, score))
    return edges


def _manual_overrides(review: ReviewOverrides) -> list[Edge]:
    """Returns one forced edge per match_review pair labeled 'same', method='manual', confidence=1.0."""
    return [Edge(a, b, "manual", 1.0, 1.0) for a, b in sorted(review.forced_same)]


def run_cascade(records: list[OfferRecord], cfg: MatchingConfig, review: ReviewOverrides) -> CascadeResult:
    """Runs all three cascade steps plus manual overrides in order; returns every accepted edge and EAN conflict."""
    step1_edges, ean_conflicts = _step1_ean(records, cfg, review)
    step2_edges = _step2_brand_mpn(records, cfg, review)
    step3_edges = _step3_fuzzy(records, cfg, review)
    manual_edges = _manual_overrides(review)
    return CascadeResult(edges=[*step1_edges, *step2_edges, *step3_edges, *manual_edges], ean_conflicts=ean_conflicts)
