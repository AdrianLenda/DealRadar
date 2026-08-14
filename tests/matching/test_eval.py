"""Tests for the mandatory evaluation harness: build_eval_dataset() and evaluate() (task spec, Z5)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from dealradar.matching.config import load_matching_config
from dealradar.matching.eval_dataset import build_eval_dataset
from dealradar.matching.service import evaluate

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_build_eval_dataset_produces_at_least_200_pairs_stratified(matching_world: Any) -> None:
    cfg = load_matching_config()
    report = build_eval_dataset(matching_world.session, cfg, NOW)
    matching_world.session.commit()

    assert report.pairs_written >= cfg.eval.candidate_pairs_min
    assert report.n_unlabeled > 0, "some pairs must be left for a human to review, per the task spec"
    assert report.n_same > 0
    assert report.n_different > 0

    # Task spec: "najwiecej par z zakresu 0.5-0.8" -- the 0.5-0.8 decision-boundary range should be
    # the single largest slice of the starter set (a plurality), not necessarily an absolute
    # majority once the "obviously different" and "obviously same" control pairs are included.
    mid_bands = ["0.5-0.65", "0.65-0.8"]
    mid_band_total = sum(report.band_counts.get(b, 0) for b in mid_bands)
    other_bands = {band: count for band, count in report.band_counts.items() if band not in mid_bands}
    assert mid_band_total > 0
    assert mid_band_total >= max(other_bands.values()), (
        f"0.5-0.8 (n={mid_band_total}) should be at least as large as any single other band: "
        f"band_counts={report.band_counts}"
    )


def test_build_eval_dataset_is_idempotent(matching_world: Any) -> None:
    cfg = load_matching_config()
    first = build_eval_dataset(matching_world.session, cfg, NOW)
    matching_world.session.commit()
    second = build_eval_dataset(matching_world.session, cfg, NOW)
    matching_world.session.commit()

    assert first.pairs_written >= cfg.eval.candidate_pairs_min
    assert second.pairs_written == 0
    assert second.pairs_already_present == first.pairs_written


def test_evaluate_reports_zero_pairs_before_eval_dataset_is_built(matching_world: Any) -> None:
    report = evaluate(matching_world.session)
    assert report.n_labeled == 0
    assert report.precision is None
    assert report.recall is None


def test_evaluate_precision_meets_z5_target(matching_world: Any) -> None:
    """The task's core acceptance gate: precision >= 0.97 on the labeled set at the configured threshold (Z5)."""
    cfg = load_matching_config()
    build_eval_dataset(matching_world.session, cfg, NOW)
    matching_world.session.commit()

    report = evaluate(matching_world.session)
    assert report.n_labeled > 0
    assert report.precision is not None, "matcher predicted zero 'same' pairs -- cannot compute precision"
    assert report.precision >= 0.97, (
        f"precision {report.precision:.4f} is below the Z5 target of 0.97 "
        f"(tp={report.true_positives} fp={report.false_positives}); "
        f"false positives: {report.false_positive_pairs}"
    )


def test_evaluate_threshold_sweep_has_multiple_rows(matching_world: Any) -> None:
    cfg = load_matching_config()
    build_eval_dataset(matching_world.session, cfg, NOW)
    matching_world.session.commit()

    report = evaluate(matching_world.session)
    assert len(report.by_threshold) >= 5
    thresholds = [row.threshold for row in report.by_threshold]
    assert thresholds == sorted(thresholds)
    assert cfg.fuzzy.accept_threshold in thresholds
