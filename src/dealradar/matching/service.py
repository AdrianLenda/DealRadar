"""Top-level entry points: `match_offers` (etap 3) and `evaluate` (the mandatory precision/recall
harness). Owner: A3. This is the only module downstream code (cli.py's `match` command) should import.

Z5 in one sentence: everything in this module is built to make a false merge rare (target precision
>= 0.97 on the labeled match_review set) even at the cost of leaving offers unmatched.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pydantic import Field
from sqlalchemy.orm import Session

from dealradar.matching.cascade import run_cascade
from dealradar.matching.canonical import canonicalize, majority_existing_product, merge_attrs
from dealradar.matching.clustering import build_clusters
from dealradar.matching.config import MatchingConfig, load_matching_config
from dealradar.matching.records import OfferRecord
from dealradar.matching.review import ReviewOverrides
from dealradar.matching import store
from dealradar.models import DealRadarModel


class MatchReport(DealRadarModel):
    """Everything the "Definicja ukonczenia" / "Raport koncowy" sections of the task spec ask for, from one run."""

    full: bool
    offers_total: int
    offers_written: int
    """Offers whose offer.product_id/match_confidence/match_method were actually written this run
    (== offers_total when full=True; only newly-matched offers otherwise)."""
    offers_singleton: int
    """Offers that ended up alone in a cluster of size 1 (no accepted edge to any other offer)."""
    pct_matched: float | None
    """Percent of offers whose match_method != 'none' -- i.e. actually linked to at least one other
    offer, not just assigned a solo product. None when offers_total == 0."""
    by_method: dict[str, int] = Field(default_factory=dict)
    n_clusters: int
    largest_cluster_size: int
    large_clusters: list[str] = Field(default_factory=list)
    """One human-readable line per cluster exceeding config/matching.yaml's cluster.max_size_warn."""
    ean_conflicts: int
    """Pairs sharing an EAN that were deliberately not merged (hard-gate violation or title_sim too
    low) -- the source-quality "garbage EAN in feed" metric the task spec asks for."""
    cluster_split_conflicts: int
    """Pairs caught and separated by the post-hoc transitivity consistency check."""
    products_created: int
    products_updated: int
    notes: list[str] = Field(default_factory=list)

    def summary_line(self) -> str:
        """Returns one human-readable line summarizing this run, in the same spirit as RunReport.summary_line."""
        pct = f"{self.pct_matched:.1f}%" if self.pct_matched is not None else "n/a"
        methods = ", ".join(f"{k}={v}" for k, v in sorted(self.by_method.items()))
        return (
            f"[match] full={self.full} offers_total={self.offers_total} pct_matched={pct} "
            f"n_clusters={self.n_clusters} largest_cluster={self.largest_cluster_size} "
            f"by_method=[{methods}] ean_conflicts={self.ean_conflicts} "
            f"cluster_split_conflicts={self.cluster_split_conflicts} "
            f"products_created={self.products_created} products_updated={self.products_updated}"
        )


def match_offers(session: Session, full: bool = False, config_path: Path | None = None) -> MatchReport:
    """Runs the EAN -> brand+MPN -> fuzzy cascade over every offer, writes product/product_attr/offer, and returns a MatchReport.

    `full=False` (default, incremental): existing matches are left untouched; only offers with
    `product_id IS NULL` get their offer row written, though the whole offer table is still used as
    context so a new offer can be attached to an existing cluster. `full=True`: every offer is
    recomputed and rewritten, but `product.id` values are preserved via a majority vote of each
    cluster's prior `product_id` membership (task requirement: "Przy --full nie wolno przenumerowac
    produktow").
    """
    cfg = load_matching_config(config_path)
    records = store.load_offer_records(session)
    if not records:
        return MatchReport(
            full=full, offers_total=0, offers_written=0, offers_singleton=0, pct_matched=None,
            n_clusters=0, largest_cluster_size=0, ean_conflicts=0, cluster_split_conflicts=0,
            products_created=0, products_updated=0,
        )

    lookup: dict[int, OfferRecord] = {r.id: r for r in records}
    review = store.load_review_overrides(session)
    cascade_result = run_cascade(records, cfg, review)
    cluster_result = build_clusters([r.id for r in records], cascade_result.edges, lookup, cfg.attribute_gate)

    now = datetime.now(timezone.utc)
    store.log_ean_conflicts(session, cascade_result.ean_conflicts, now)
    store.log_cluster_conflicts(session, cluster_result.conflicts, now)

    by_method: Counter[str] = Counter()
    offers_written = 0
    offers_singleton = 0
    products_created = 0
    products_updated = 0
    n_clusters = 0
    largest_cluster_size = 0
    large_clusters: list[str] = []

    for root, member_ids in sorted(cluster_result.components.items()):
        n_clusters += 1
        largest_cluster_size = max(largest_cluster_size, len(member_ids))
        if len(member_ids) > cfg.cluster.max_size_warn:
            large_clusters.append(
                f"cluster root=offer:{root} size={len(member_ids)} "
                f"exceeds max_size_warn={cfg.cluster.max_size_warn} -- inspect for a false-merge bug"
            )
        if len(member_ids) == 1:
            offers_singleton += 1

        members = [lookup[i] for i in member_ids]
        target_product_id = majority_existing_product(members)
        canonical_title, ean, mpn, brand, category_id = canonicalize(members)
        merged_attrs = merge_attrs(members)

        if target_product_id is not None:
            store.update_product(session, target_product_id, canonical_title, brand, mpn, ean, category_id)
            products_updated += 1
            product_id = target_product_id
        else:
            product_id = store.create_product(session, canonical_title, brand, mpn, ean, category_id, now)
            products_created += 1
        store.replace_product_attrs(session, product_id, merged_attrs)

        for member in members:
            edge = cluster_result.best_edge_for.get(member.id)
            method = edge.method if edge is not None else "none"
            confidence = edge.confidence if edge is not None else None
            by_method[method] += 1

            should_write = full or member.product_id is None
            if should_write:
                store.update_offer_match(session, member.id, product_id, confidence, method)
                offers_written += 1

    matched_count = sum(count for method, count in by_method.items() if method != "none")
    pct_matched = 100.0 * matched_count / len(records)

    return MatchReport(
        full=full,
        offers_total=len(records),
        offers_written=offers_written,
        offers_singleton=offers_singleton,
        pct_matched=pct_matched,
        by_method=dict(by_method),
        n_clusters=n_clusters,
        largest_cluster_size=largest_cluster_size,
        large_clusters=large_clusters,
        ean_conflicts=len(cascade_result.ean_conflicts),
        cluster_split_conflicts=len(cluster_result.conflicts),
        products_created=products_created,
        products_updated=products_updated,
    )


# --------------------------------------------------------------------------------------------------
# evaluate() -- the mandatory precision/recall harness (task spec: "obowiazkowa czesc zadania")
# --------------------------------------------------------------------------------------------------


class ThresholdRow(DealRadarModel):
    """Precision/recall of the *whole pipeline* if config/matching.yaml's fuzzy.accept_threshold were set to `threshold`."""

    threshold: float
    precision: float | None
    recall: float | None
    true_positives: int
    false_positives: int
    false_negatives: int


class EvalReport(DealRadarModel):
    """Precision/recall of the matcher's own algorithmic decisions against match_review ground truth (Z5)."""

    n_labeled: int
    """Pairs with label in (same, different, conflict) -- the actual evaluation set."""
    n_pending_review: int
    """Pairs with label NULL/'unlabeled' -- task spec: "napisz w raporcie, ile par czeka na czlowieka"."""
    threshold_used: float
    """config/matching.yaml's current fuzzy.accept_threshold -- the precision/recall below are AT this value."""
    precision: float | None
    """None when there were zero pairs the matcher predicted 'same' (undefined, not zero)."""
    recall: float | None
    """None when there were zero pairs actually labeled 'same' (undefined, not zero)."""
    f1: float | None
    true_positives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    true_negatives: int = 0
    false_positive_pairs: list[tuple[int, int]] = Field(default_factory=list)
    """Up to 50 (offer_a, offer_b) pairs the matcher merged but a human labeled 'different'/'conflict' -- the
    dangerous error class (Z5): these are what a false-merge audit should look at first."""
    false_negative_pairs: list[tuple[int, int]] = Field(default_factory=list)
    """Up to 50 pairs labeled 'same' that the matcher left apart -- lost completeness, not a lie."""
    by_threshold: list[ThresholdRow] = Field(default_factory=list)
    """Precision/recall swept across several candidate fuzzy.accept_threshold values, so a threshold
    change can be chosen with evidence instead of a guess (task spec requirement)."""

    def summary_line(self) -> str:
        """Returns one human-readable line summarizing this evaluation run."""
        p = f"{self.precision:.3f}" if self.precision is not None else "n/a"
        r = f"{self.recall:.3f}" if self.recall is not None else "n/a"
        return (
            f"[match --eval] n_labeled={self.n_labeled} n_pending_review={self.n_pending_review} "
            f"threshold={self.threshold_used} precision={p} recall={r} "
            f"tp={self.true_positives} fp={self.false_positives} fn={self.false_negatives} tn={self.true_negatives}"
        )


def _cluster_roots(records: list[OfferRecord], cfg: MatchingConfig) -> dict[int, int]:
    """Runs the cascade with no match_review overrides and returns {offer_id: cluster_root_id} -- pure algorithmic output.

    Deliberately ignores match_review's forced/forbidden pairs: evaluate() measures what the
    cascade itself would decide, not what it decides after being told the answer by the very
    labels it is being scored against -- using the overrides here would make precision trivially
    ~1.0 and the whole harness meaningless.
    """
    lookup = {r.id: r for r in records}
    cascade_result = run_cascade(records, cfg, ReviewOverrides())
    cluster_result = build_clusters([r.id for r in records], cascade_result.edges, lookup, cfg.attribute_gate)
    roots: dict[int, int] = {}
    for root, members in cluster_result.components.items():
        for member_id in members:
            roots[member_id] = root
    return roots


def _sweep_thresholds(
    records: list[OfferRecord], labeled_pairs: list[tuple[int, int, str]], cfg: MatchingConfig
) -> list[ThresholdRow]:
    """Returns precision/recall of the whole pipeline at each of several candidate fuzzy.accept_threshold values."""
    candidates = sorted({0.60, 0.65, 0.70, 0.75, 0.80, 0.83, 0.85, 0.90, 0.95, cfg.fuzzy.accept_threshold})
    rows: list[ThresholdRow] = []
    for threshold in candidates:
        trial_fuzzy = cfg.fuzzy.model_copy(update={"accept_threshold": threshold})
        trial_cfg = cfg.model_copy(update={"fuzzy": trial_fuzzy})
        roots = _cluster_roots(records, trial_cfg)
        tp = fp = fn = 0
        for offer_a, offer_b, label in labeled_pairs:
            if offer_a not in roots or offer_b not in roots:
                continue
            predicted_same = roots[offer_a] == roots[offer_b]
            actual_same = label == "same"
            if predicted_same and actual_same:
                tp += 1
            elif predicted_same:
                fp += 1
            elif actual_same:
                fn += 1
        precision = tp / (tp + fp) if (tp + fp) > 0 else None
        recall = tp / (tp + fn) if (tp + fn) > 0 else None
        rows.append(ThresholdRow(threshold=threshold, precision=precision, recall=recall, true_positives=tp, false_positives=fp, false_negatives=fn))
    return rows


def evaluate(session: Session, config_path: Path | None = None) -> EvalReport:
    """Scores the matcher's algorithmic decisions against match_review's manually labeled pairs; returns an EvalReport.

    Never writes to the database -- this is a read-only measurement, safe to run at any time
    against the live offer/match_review state.
    """
    cfg = load_matching_config(config_path)
    records = store.load_offer_records(session)
    all_review_rows = store.load_all_match_review_rows(session)

    labeled_pairs = [(a, b, label) for a, b, label in all_review_rows if label in ("same", "different", "conflict")]
    n_pending_review = sum(1 for _, _, label in all_review_rows if label in (None, "unlabeled"))

    if not labeled_pairs or not records:
        return EvalReport(
            n_labeled=len(labeled_pairs), n_pending_review=n_pending_review,
            threshold_used=cfg.fuzzy.accept_threshold, precision=None, recall=None, f1=None,
        )

    roots = _cluster_roots(records, cfg)
    tp = fp = fn = tn = 0
    fp_pairs: list[tuple[int, int]] = []
    fn_pairs: list[tuple[int, int]] = []
    for offer_a, offer_b, label in labeled_pairs:
        if offer_a not in roots or offer_b not in roots:
            continue  # pair references an offer no longer in `offer` -- can't be scored
        predicted_same = roots[offer_a] == roots[offer_b]
        actual_same = label == "same"
        if predicted_same and actual_same:
            tp += 1
        elif predicted_same and not actual_same:
            fp += 1
            fp_pairs.append((offer_a, offer_b))
        elif not predicted_same and actual_same:
            fn += 1
            fn_pairs.append((offer_a, offer_b))
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    f1 = (2 * precision * recall / (precision + recall)) if precision is not None and recall is not None and (precision + recall) > 0 else None

    return EvalReport(
        n_labeled=len(labeled_pairs),
        n_pending_review=n_pending_review,
        threshold_used=cfg.fuzzy.accept_threshold,
        precision=precision,
        recall=recall,
        f1=f1,
        true_positives=tp,
        false_positives=fp,
        false_negatives=fn,
        true_negatives=tn,
        false_positive_pairs=fp_pairs[:50],
        false_negative_pairs=fn_pairs[:50],
        by_threshold=_sweep_thresholds(records, labeled_pairs, cfg),
    )
