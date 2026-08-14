"""System-health alarms (F5, Z7): the feature every deal-aggregator skips, per the A6 task spec.

A feed can change format, a collector can start returning zero records, or a matcher regression can tank
`pct_matched` -- and the digest would still render a perfectly normal-looking "no deals today" report unless
something explicitly compares this run against recent history and says so. This module does exactly that:
for `offers_fetched`, `pct_with_ean`, `pct_matched`, and `deals_scored`, it compares the latest value per
stage against the median of the previous 7 runs of that stage (read straight from `run_log`, A0's own
Z7 ledger -- no separate bookkeeping) and flags a drop greater than 30%. It also promotes collect's own F5
detection (a source returning 0 records after previously returning some, already computed by
`collectors/runner.py`) into the same alert list, so the digest has one place to pull "UWAGI" from.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.models import RunReport

DROP_THRESHOLD = 0.30
"""ARCHITEKTURA.md Z7: "Spadek ktoregokolwiek wskaznika o > 30% ... to alarm, nie ciekawostka"."""
LOOKBACK_RUNS = 7
"""ARCHITEKTURA.md Zadanie 2: "porownaj z mediana z 7 ostatnich przebiegow"."""

# {stage name this pipeline writes to run_log: metric attribute on RunReport} -- see pipeline.py's
# _run_stage naming: collect/normalize/match/score_deals/score_quality are each their own run_log stage,
# whether invoked standalone (cli.py) or via pipeline.run_full/run_fast.
_METRIC_STAGES: tuple[tuple[str, str], ...] = (
    ("collect", "offers_fetched"),
    ("normalize", "pct_with_ean"),
    ("match", "pct_matched"),
    ("score_deals", "deals_scored"),
)


@dataclass(frozen=True)
class HealthAlert:
    """One F5-style health warning: either a metric dropped >30% vs. its 7-run baseline, or a source that
    silently went quiet. Rendered as the digest's UWAGI section, always above the deals list."""

    kind: Literal["metric_drop", "source_zero"]
    stage: str
    metric: str | None
    source_name: str | None
    current_value: float | None
    baseline_median: float | None
    detail: str


def _recent_metric_values(session: Session, stage: str, metric: str, *, before: datetime, limit: int) -> list[float]:
    """Returns up to `limit` non-null values of `metric` from run_log for `stage`, most recent first, strictly
    before `before` (so the run currently being evaluated never pollutes its own baseline)."""
    rows = session.execute(
        text(
            f"SELECT {metric} FROM run_log WHERE stage = :stage AND started_at < :before "
            f"AND {metric} IS NOT NULL ORDER BY started_at DESC LIMIT :limit"
        ),
        {"stage": stage, "before": before.isoformat(), "limit": limit},
    ).scalars().all()
    return [float(v) for v in rows if v is not None]


def _metric_drop_alerts(session: Session, current: dict[str, RunReport]) -> list[HealthAlert]:
    """Compares each stage's current run to its 7-run median baseline; returns one alert per >30% drop."""
    alerts: list[HealthAlert] = []
    for stage, metric in _METRIC_STAGES:
        report = current.get(stage)
        if report is None:
            continue
        current_value = getattr(report, metric)
        if current_value is None:
            continue
        baseline_values = _recent_metric_values(session, stage, metric, before=report.started_at, limit=LOOKBACK_RUNS)
        if not baseline_values:
            continue  # cold start: nothing to compare against yet, not an alarm (Z4: absence isn't a lie)
        baseline = statistics.median(baseline_values)
        if baseline <= 0:
            continue
        drop = (baseline - current_value) / baseline
        if drop > DROP_THRESHOLD:
            alerts.append(
                HealthAlert(
                    kind="metric_drop",
                    stage=stage,
                    metric=metric,
                    source_name=None,
                    current_value=current_value,
                    baseline_median=baseline,
                    detail=(
                        f"{stage}.{metric} spadl o {drop * 100:.0f}% wzgledem mediany z ostatnich "
                        f"{len(baseline_values)} przebiegow ({current_value:g} vs mediana {baseline:.1f})"
                    ),
                )
            )
    return alerts


def _source_zero_alerts(collect_report: RunReport | None) -> list[HealthAlert]:
    """Promotes collect's own F5 detection (source returned 0 after previously non-empty) into HealthAlerts.

    `collectors/runner.py` already writes a note containing "F5:" for exactly this situation (see
    `_run_one_source`) -- reused verbatim here rather than re-deriving it, per the task spec's "nie liczysz
    tego u siebie po swojemu" rule: this module never recomputes a stage's own pass/fail judgment.
    """
    if collect_report is None:
        return []
    alerts: list[HealthAlert] = []
    for note in collect_report.notes:
        if "F5:" not in note:
            continue
        source_name = note.split(",", 1)[0].strip()
        alerts.append(
            HealthAlert(
                kind="source_zero",
                stage="collect",
                metric="offers_fetched",
                source_name=source_name,
                current_value=0.0,
                baseline_median=None,
                detail=note,
            )
        )
    return alerts


def check_health(session: Session, current: dict[str, RunReport]) -> list[HealthAlert]:
    """Returns every health alarm for this run: >30% metric drops vs. the 7-run median, plus any source that
    returned zero offers after previously returning some. `current` maps stage name (e.g. "collect",
    "normalize", "match", "score_deals") to that stage's just-produced RunReport; a stage missing from
    `current` (e.g. it was skipped or failed before writing a report) is simply not checked.
    """
    return _source_zero_alerts(current.get("collect")) + _metric_drop_alerts(session, current)


def latest_stage_reports(session: Session) -> dict[str, RunReport]:
    """Returns the most recent run_log row for each stage in _METRIC_STAGES, as {stage: RunReport}.

    Used by `dealradar report` (standalone digest generation, not right after a pipeline run) to feed
    check_health with "the current run" when there is no in-memory RunReport available -- the latest logged
    run of each stage stands in for "current".
    """
    result: dict[str, RunReport] = {}
    stages = {stage for stage, _ in _METRIC_STAGES}
    for stage in stages:
        row = session.execute(
            text(
                "SELECT stage, started_at, finished_at, status, offers_fetched, offers_new, pct_with_ean, "
                "pct_matched, products_new, deals_scored, quality_scored, sources_failed, duration_s, notes "
                "FROM run_log WHERE stage = :stage ORDER BY started_at DESC LIMIT 1"
            ),
            {"stage": stage},
        ).mappings().first()
        if row is None:
            continue
        result[stage] = _row_to_run_report(row)
    return result


def _row_to_run_report(row: object) -> RunReport:
    """Converts one run_log row mapping into a RunReport, parsing its JSON list columns back to lists."""
    data = dict(row)  # type: ignore[call-overload]
    return RunReport(
        stage=data["stage"],
        started_at=datetime.fromisoformat(data["started_at"]),
        finished_at=datetime.fromisoformat(data["finished_at"]) if data["finished_at"] else None,
        status=data["status"],
        offers_fetched=data["offers_fetched"],
        offers_new=data["offers_new"],
        pct_with_ean=data["pct_with_ean"],
        pct_matched=data["pct_matched"],
        products_new=data["products_new"],
        deals_scored=data["deals_scored"],
        quality_scored=data["quality_scored"],
        sources_failed=json.loads(data["sources_failed"]) if data["sources_failed"] else [],
        duration_s=data["duration_s"],
        notes=json.loads(data["notes"]) if data["notes"] else [],
    )
