"""Builds the starter, stratified `match_review` evaluation set the task spec requires. Owner: A3.

"Bez zbioru testowego strojenie progow to zgadywanie" -- without an eval set, threshold tuning is
guesswork. This module generates realistic *candidate* pairs (ones the cascade would actually
consider: shared EAN, shared brand+MPN, or blocking-eligible), scores them with the same combined
signal cascade step 3 uses, buckets them by score, and samples toward config/matching.yaml's
`eval.target_band_share` so most of the starter set lands in the 0.5-0.8 "decision boundary" band.
Pairs are auto-labeled only where a hard, unambiguous rule applies (identical EAN + no gate conflict
= same; any hard-gate violation, or two different validated EANs = different); everything else is
left `unlabeled` for a human to resolve later.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from dealradar.matching import store
from dealradar.matching.blocking import group_for_step3, price_compatible
from dealradar.matching.config import EvalConfig, MatchingConfig
from dealradar.matching.gates import hard_gate_conflict
from dealradar.matching.records import OfferRecord
from dealradar.matching.similarity import combined_score, pairwise_tfidf_cosine
from dealradar.models import DealRadarModel


@dataclass(frozen=True)
class _ScoredPair:
    a: OfferRecord
    b: OfferRecord
    score: float
    label: str  # 'same' | 'different' | 'unlabeled'


class EvalDatasetReport(DealRadarModel):
    """Summary of one build_eval_dataset() run."""

    candidates_considered: int
    pairs_written: int
    pairs_already_present: int
    """Candidate pairs skipped because match_review already had a row for that pair (idempotent rerun)."""
    n_same: int
    n_different: int
    n_unlabeled: int
    """Pairs left for a human to review -- task spec: report this number explicitly."""
    band_counts: dict[str, int] = field(default_factory=dict)


def _generate_candidates(records: list[OfferRecord], cfg: MatchingConfig) -> list[tuple[OfferRecord, OfferRecord]]:
    """Returns deduplicated (a, b) pairs the cascade would realistically compare: shared EAN, shared

    brand+MPN, blocking-eligible (brand+category+price window) pairs, plus a small deterministic
    sample of cross-brand pairs so the low-similarity band has real examples too.
    """
    seen: set[tuple[int, int]] = set()
    pairs: list[tuple[OfferRecord, OfferRecord]] = []

    def add(x: OfferRecord, y: OfferRecord) -> None:
        key = (min(x.id, y.id), max(x.id, y.id))
        if key in seen:
            return
        seen.add(key)
        pairs.append((x, y) if x.id < y.id else (y, x))

    by_ean: dict[str, list[OfferRecord]] = defaultdict(list)
    for r in records:
        if r.ean:
            by_ean[r.ean].append(r)
    for group in by_ean.values():
        for a, b in itertools.combinations(group, 2):
            add(a, b)

    by_brand_mpn: dict[tuple[str, str], list[OfferRecord]] = defaultdict(list)
    for r in records:
        if r.brand_norm and r.mpn_norm and len(r.mpn_norm) >= cfg.brand_mpn.mpn_min_length:
            by_brand_mpn[(r.brand_norm, r.mpn_norm)].append(r)
    for group in by_brand_mpn.values():
        for a, b in itertools.combinations(group, 2):
            add(a, b)

    for group in group_for_step3(records).values():
        for a, b in itertools.combinations(group, 2):
            if price_compatible(a.price_total, b.price_total, cfg.fuzzy.price_bucket_pct):
                add(a, b)

    # Same (brand, category) but OUTSIDE the production price window -- e.g. a 512GB and a 2TB
    # drive from the same brand, priced too far apart to ever become a step-3 candidate in
    # production. These are exactly the kind of high-text-similarity, different-attribute "hard
    # negative" pairs the 0.5-0.8 decision boundary is made of, so the eval set deliberately casts
    # a wider net than production blocking does -- this is a discovery/labeling tool, not a preview
    # of what match_offers would actually compare.
    for group in group_for_step3(records).values():
        for a, b in itertools.combinations(group, 2):
            add(a, b)

    # Same category, ANY brand (including two different brands, or one/both brandless) -- two SSDs
    # from different manufacturers share enough vocabulary ("dysk", "ssd", "m.2") to be a real
    # "would a human even glance at this pair" candidate, without being a step-3 blocking candidate
    # (blocking requires matching brand_norm). Bounded per category so one huge category can't
    # swamp the sample; taken in a fixed stride so the result is deterministic.
    by_category: dict[int, list[OfferRecord]] = defaultdict(list)
    for r in records:
        if r.category_id is not None:
            by_category[r.category_id].append(r)
    max_per_category = 12
    for group in by_category.values():
        count = 0
        for a, b in itertools.combinations(group, 2):
            if count >= max_per_category:
                break
            before = len(pairs)
            add(a, b)
            if len(pairs) > before:
                count += 1

    # Deterministic cross-brand sample: gives the low-similarity band a handful of real "obviously
    # different" examples without an O(n^2) scan of the whole offer table. Capped so the easy
    # "obviously different" band stays a minority of the dataset (task spec wants most pairs near
    # the 0.5-0.8 boundary).
    branded = [r for r in records if r.brand_norm]
    max_cross_brand_samples = 40
    stride = max(1, len(branded) // 20)
    added_cross_brand = 0
    for i in range(0, len(branded)):
        if added_cross_brand >= max_cross_brand_samples:
            break
        j = i + stride
        if j >= len(branded):
            continue
        a, b = branded[i], branded[j]
        if a.brand_norm != b.brand_norm:
            before = len(pairs)
            add(a, b)
            if len(pairs) > before:
                added_cross_brand += 1

    return pairs


def _auto_label(a: OfferRecord, b: OfferRecord, cfg: MatchingConfig) -> str:
    """Returns 'same'/'different' when an unambiguous rule applies, else 'unlabeled' for human review."""
    gate_reason = hard_gate_conflict(a, b, cfg.attribute_gate)
    if gate_reason is not None:
        return "different"
    if a.ean and b.ean:
        return "same" if a.ean == b.ean else "different"
    return "unlabeled"


def _band_index(score: float, bands: list[tuple[float, float]]) -> int:
    """Returns the index of the band containing score, clamped to the last band if score == its upper edge."""
    for index, (low, high) in enumerate(bands):
        if low <= score < high:
            return index
    return len(bands) - 1


def build_eval_dataset(session: Session, config: MatchingConfig, observed_at: datetime) -> EvalDatasetReport:
    """Generates and writes a stratified starter match_review dataset; returns a summary (never overwrites existing pairs).

    Deterministic and idempotent: candidate generation, scoring, and stratified selection use no
    randomness, and a candidate pair already present in match_review (from a prior run of this
    function, or from production conflict-logging) is counted but not re-inserted.
    """
    records = store.load_offer_records(session)
    candidates = _generate_candidates(records, config)

    scored: list[_ScoredPair] = []
    for a, b in candidates:
        tfidf = pairwise_tfidf_cosine(a.title_norm, b.title_norm)
        score = combined_score(tfidf, a, b, config.fuzzy, config.attribute_gate)
        scored.append(_ScoredPair(a, b, score, _auto_label(a, b, config)))

    eval_cfg: EvalConfig = config.eval
    bands = eval_cfg.score_bands
    band_labels = list(eval_cfg.target_band_share.keys())
    band_shares = list(eval_cfg.target_band_share.values())

    buckets: list[list[_ScoredPair]] = [[] for _ in bands]
    for pair in scored:
        buckets[_band_index(pair.score, bands)].append(pair)
    for bucket in buckets:
        bucket.sort(key=lambda p: (p.a.id, p.b.id))  # deterministic order within a band

    selected: list[_ScoredPair] = []
    leftover_target = 0
    for bucket, share in zip(buckets, band_shares):
        want = round(eval_cfg.candidate_pairs_min * share) + leftover_target
        take = bucket[:want]
        selected.extend(take)
        leftover_target = max(0, want - len(take))  # redistribute an under-full band's shortfall forward

    # If bands (after their target share) still leave us short of candidate_pairs_min (e.g. a niche
    # band was too small), top up from whatever is left, deterministically, preferring the 0.5-0.8
    # decision-boundary bands first per the task spec's emphasis.
    if len(selected) < min(eval_cfg.candidate_pairs_min, len(scored)):
        already = {(p.a.id, p.b.id) for p in selected}
        remaining_by_band = sorted(
            range(len(buckets)), key=lambda i: abs((bands[i][0] + bands[i][1]) / 2 - 0.65)
        )
        for band_index in remaining_by_band:
            for pair in buckets[band_index]:
                if len(selected) >= eval_cfg.candidate_pairs_min:
                    break
                if (pair.a.id, pair.b.id) not in already:
                    selected.append(pair)
                    already.add((pair.a.id, pair.b.id))
            if len(selected) >= eval_cfg.candidate_pairs_min:
                break

    band_counts: dict[str, int] = {label: 0 for label in band_labels}
    pairs_written = 0
    pairs_already_present = 0
    n_same = n_different = n_unlabeled = 0

    for pair in selected:
        band_counts[band_labels[_band_index(pair.score, bands)]] += 1
        if store.match_review_pair_exists(session, pair.a.id, pair.b.id):
            pairs_already_present += 1
            continue
        labeled_at = observed_at if pair.label != "unlabeled" else None
        store.insert_eval_pair(session, pair.a.id, pair.b.id, pair.score, "fuzzy", pair.label, labeled_at)
        pairs_written += 1
        if pair.label == "same":
            n_same += 1
        elif pair.label == "different":
            n_different += 1
        else:
            n_unlabeled += 1

    return EvalDatasetReport(
        candidates_considered=len(scored),
        pairs_written=pairs_written,
        pairs_already_present=pairs_already_present,
        n_same=n_same,
        n_different=n_different,
        n_unlabeled=n_unlabeled,
        band_counts=band_counts,
    )
