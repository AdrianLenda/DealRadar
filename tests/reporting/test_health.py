"""Tests for dealradar.reporting.health -- F5/Z7 system-health alarms. Owner: A6."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from conftest import insert_run_log

from dealradar.models import RunReport
from dealradar.reporting.health import check_health, latest_stage_reports

BASE = datetime(2026, 8, 1, 6, 0, 0, tzinfo=timezone.utc)


def _day(n: int) -> datetime:
    return BASE + timedelta(days=n)


def test_no_alerts_on_a_cold_start_with_no_prior_history(db: Session) -> None:
    current = {"collect": RunReport(stage="collect", started_at=_day(10), offers_fetched=100)}
    alerts = check_health(db, current)
    assert alerts == []  # nothing to compare against yet -- not an alarm (Z4)


def test_flags_a_greater_than_30_percent_drop_vs_7_run_median(db: Session) -> None:
    for i in range(7):
        insert_run_log(db, "collect", started_at=_day(i), offers_fetched=1000)
    db.commit()
    current = {"collect": RunReport(stage="collect", started_at=_day(10), offers_fetched=600)}  # -40%
    alerts = check_health(db, current)
    assert any(a.kind == "metric_drop" and a.stage == "collect" and a.metric == "offers_fetched" for a in alerts)


def test_does_not_flag_a_drop_under_the_30_percent_threshold(db: Session) -> None:
    for i in range(7):
        insert_run_log(db, "collect", started_at=_day(i), offers_fetched=1000)
    db.commit()
    current = {"collect": RunReport(stage="collect", started_at=_day(10), offers_fetched=800)}  # -20%
    alerts = check_health(db, current)
    assert alerts == []


def test_flags_pct_matched_drop_on_the_match_stage(db: Session) -> None:
    for i in range(7):
        insert_run_log(db, "match", started_at=_day(i), pct_matched=90.0)
    db.commit()
    current = {"match": RunReport(stage="match", started_at=_day(10), pct_matched=40.0)}
    alerts = check_health(db, current)
    assert any(a.metric == "pct_matched" for a in alerts)


def test_source_returning_zero_after_nonempty_history_is_always_flagged(db: Session) -> None:
    collect_report = RunReport(
        stage="collect", started_at=_day(10), offers_fetched=0,
        sources_failed=["pepper"],
        notes=["pepper, fetched=0, new=0, errors=0, duration_s=0.10 -- F5: returned 0 records this run but 500 exist from earlier runs (possible feed format change)"],
    )
    alerts = check_health(db, {"collect": collect_report})
    source_alerts = [a for a in alerts if a.kind == "source_zero"]
    assert len(source_alerts) == 1
    assert source_alerts[0].source_name == "pepper"


def test_baseline_excludes_runs_at_or_after_the_current_run(db: Session) -> None:
    """The 7-run median must never include the run currently being evaluated (that would hide its own drop)."""
    for i in range(7):
        insert_run_log(db, "collect", started_at=_day(i), offers_fetched=1000)
    # A row *later* than "current" must not count toward current's baseline either.
    insert_run_log(db, "collect", started_at=_day(20), offers_fetched=1000)
    db.commit()
    current = {"collect": RunReport(stage="collect", started_at=_day(10), offers_fetched=500)}
    alerts = check_health(db, current)
    assert any(a.kind == "metric_drop" for a in alerts)


def test_latest_stage_reports_reads_the_most_recent_row_per_stage(db: Session) -> None:
    insert_run_log(db, "collect", started_at=_day(1), offers_fetched=10)
    insert_run_log(db, "collect", started_at=_day(2), offers_fetched=20)
    db.commit()
    latest = latest_stage_reports(db)
    assert latest["collect"].offers_fetched == 20
