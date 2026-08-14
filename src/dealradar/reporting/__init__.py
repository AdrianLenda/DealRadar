"""Etap 6: report, watchlist, notifications, and pipeline orchestration. Owner: A6 (also owns pipeline.py).

Public surface:

    digest.build_digest(session) -> Digest        # assembles the daily report from already-computed data
    digest.render_text(digest) -> str             # terminal / mail body
    digest.render_html(digest, session) -> str     # standalone HTML, evidence expandable per deal

    health.check_health(session, current) -> list[HealthAlert]   # F5/Z7 alarms, feeds Digest.alerts

    watch.add_watch / list_watches / get_watch / update_watch / remove_watch / set_watch_active   # CRUD
    watch.evaluate_watches(session) -> list[WatchHit]             # condition matching, no scoring done here
    watch.notify_watch_hits(session, hits) -> NotifyReport        # antispam + delivery via Notifier plugins

See ARCHITEKTURA.md sections 4 and 11 and prompty/A6-raporty-i-orkiestracja.md for the contract, and
../pipeline.py for `run_full`/`run_fast`/`rebuild`, which call into `collectors`/`normalize`/`matching`/
`scoring` and fold their results into the RunReports this package's digest reads back out of `run_log`.
"""

from __future__ import annotations

from dealradar.reporting.digest import (
    DEFAULT_TOP_N,
    BullshitLine,
    DealLine,
    Digest,
    WatchHitLine,
    build_digest,
    render_html,
    render_text,
)
from dealradar.reporting.health import HealthAlert, check_health, latest_stage_reports
from dealradar.reporting.watch import (
    ConsoleNotifier,
    FileNotifier,
    Notifier,
    NotifyReport,
    SmtpNotifier,
    TelegramNotifier,
    WatchHit,
    add_watch,
    build_notifier,
    evaluate_watches,
    get_watch,
    get_watch_any_alltime_low,
    list_watches,
    notify_watch_hits,
    remove_watch,
    resolve_query_watch_product_id,
    set_watch_active,
    set_watch_any_alltime_low,
    should_notify,
    update_watch,
)

__all__ = [
    "DEFAULT_TOP_N",
    "BullshitLine",
    "DealLine",
    "Digest",
    "WatchHitLine",
    "build_digest",
    "render_html",
    "render_text",
    "HealthAlert",
    "check_health",
    "latest_stage_reports",
    "ConsoleNotifier",
    "FileNotifier",
    "Notifier",
    "NotifyReport",
    "SmtpNotifier",
    "TelegramNotifier",
    "WatchHit",
    "add_watch",
    "build_notifier",
    "evaluate_watches",
    "get_watch",
    "get_watch_any_alltime_low",
    "list_watches",
    "notify_watch_hits",
    "remove_watch",
    "resolve_query_watch_product_id",
    "set_watch_active",
    "set_watch_any_alltime_low",
    "should_notify",
    "update_watch",
]
