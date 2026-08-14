"""Etap 6 orchestration: glues the six pipeline stages into full/fast runs and from-raw rebuilds. Owner: A6.

Implements ARCHITEKTURA.md section 4 (pipeline order) and section 11 (schedule: nightly full run, hourly
fast track) plus Zadanie 1 of the A6 task spec:

    run_full(session, cfg)               -> RunReport   # nocny: collect -> normalize -> match -> snapshot
                                                          #        -> score --deals -> score --quality
    run_fast(session, cfg)                -> RunReport   # godzinowy: Pepper RSS + watchlist re-scoring only
    rebuild(session, from_stage, since)   -> RunReport   # Z1: recompute derived state from raw_offer

Design decisions worth stating up front (see the A6 final report for the full reasoning):

- **Failure isolation (Z7).** Every stage runs inside its own `db.run_report` transaction (the same
  primitive `collect`/`normalize`/`match`/`snapshot`/`score` already use standalone). A stage's exception is
  caught here, logged into the aggregate report's notes, and orchestration continues -- a broken stage never
  discards prior stages' already-committed work, and never aborts the ones after it that can still make
  sense. The one explicit dependency the task spec calls out: if `match` raises, `score --deals` and
  `score --quality` are skipped this run (not attempted against a half-finished match), with a note
  explaining why, rather than crashing.
- **Incremental `since` by default.** `normalize_batch` resets `offer.product_id`/`match_confidence`/
  `match_method` to NULL for every raw_offer row it reprocesses (see db.upsert_offer's ON CONFLICT clause --
  a fresh Offer from GenericNormalizer never carries a prior match forward). Re-normalizing *all* raw_offer
  rows on every nightly run would therefore force `match` to recreate a brand-new `product` row for every
  cluster every night (since `majority_existing_product` would see product_id=NULL for everyone), silently
  orphaning the previous night's product/deal/quality_score rows. `run_full`/`run_fast` avoid this by
  defaulting `since` to the finished_at of the last successful orchestrated run (None only on a cold start),
  so normalize only reprocesses genuinely new/changed raw_offer rows; unaffected offers keep their existing
  product_id, and `match`'s majority-vote logic reuses the existing product id for their cluster.
- **`rebuild --from raw` does not re-run `snapshot`.** snapshot_prices writes *today's wall-clock* price for
  whatever is in `offer` right now -- it is not a pure function of raw_offer, so re-running it during a
  rebuild would make two rebuilds of the same raw_offer state produce different price_point rows depending
  on which day the rebuild happened to run. The task spec's own definition-of-completion #3 only lists
  `offer`, `product`, `deal`, `quality_score` as what a from-raw rebuild reconstructs -- price_point is
  deliberately not in that list. `price_point` is treated as already-collected history (like raw_offer
  itself): untouched by rebuild, and the *input* score_deals reads to recompute `deal`.
- **Concurrency lock.** A file lock (`RunReport`'s sibling `RunLockError`, atomically created via
  `os.O_CREAT | os.O_EXCL`) next to the sqlite file, holding {pid, acquired_at} and a TTL for stale-lock
  takeover after a crash. Chosen over a `run_lock` DB table because the failure mode being defended against
  is two separate OS processes (two `dealradar run` invocations) racing on the same sqlite file --
  filesystem-level atomic file creation is the simplest correct primitive for that, and needs no schema.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from dealradar import db
from dealradar.collectors.runner import run_collect
from dealradar.config import AppConfig
from dealradar.errors import RunLockError
from dealradar.matching import match_offers
from dealradar.models import RunReport
from dealradar.normalize import fill_run_report as run_normalize
from dealradar.scoring.deal import score_deals
from dealradar.scoring.history import snapshot_prices
from dealradar.scoring.quality import score_quality

logger = logging.getLogger("dealradar.pipeline")

DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache" / "collectors"

# --------------------------------------------------------------------------------------------------
# Concurrency lock (Zadanie 1: "dwa rownolegle przebiegi na tej samej bazie to uszkodzone dane")
# --------------------------------------------------------------------------------------------------

RUN_LOCK_TTL_SECONDS = 6.0 * 3600.0
"""A real orchestrated run should finish in minutes, not hours; a lock older than this is assumed to belong
to a crashed process (no PID liveness check is attempted -- cross-platform PID checks are unreliable enough
that a generous, documented TTL is the more honest primitive) and is taken over, with a logged warning."""


def _lock_path_for(database_url: str) -> Path:
    """Returns the lock file path for database_url: next to the sqlite file itself, or a hashed shared path."""
    prefix = "sqlite:///"
    if database_url.startswith(prefix):
        raw_path = database_url[len(prefix) :]
        if raw_path and raw_path != ":memory:":
            db_path = Path(raw_path)
            return db_path.with_name(db_path.name + ".lock")
    # In-memory sqlite (tests) or a non-sqlite URL: fall back to a shared, hashed lock directory so
    # concurrent processes pointed at the *same* database_url still collide on the same lock file.
    lock_dir = Path(tempfile.gettempdir()) / "dealradar_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(database_url.encode("utf-8")).hexdigest()[:16]
    return lock_dir / f"{digest}.lock"


def _read_lock_payload(path: Path) -> dict[str, Any]:
    """Best-effort read of a lock file's {pid, acquired_at} JSON payload; returns {} if unreadable."""
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


@contextmanager
def acquire_run_lock(database_url: str, *, ttl_seconds: float = RUN_LOCK_TTL_SECONDS) -> Iterator[None]:
    """Holds an exclusive, cross-process file lock for database_url for the life of the `with` block.

    Raises RunLockError immediately if another live (non-stale) run already holds the lock. A lock older
    than `ttl_seconds` is assumed to belong to a crashed process and is taken over automatically (logged as
    a warning) rather than wedging the pipeline shut forever. Always releases the lock on exit, including
    on an exception.
    """
    path = _lock_path_for(database_url)
    payload = json.dumps(
        {"pid": os.getpid(), "acquired_at": datetime.now(timezone.utc).isoformat()}
    ).encode("utf-8")

    fd = _try_create_lock_file(path)
    if fd is None:
        age_s = _lock_age_seconds(path)
        if age_s is not None and age_s > ttl_seconds:
            logger.warning(
                "run_lock at %s is %.0fs old (> ttl=%.0fs); assuming a crashed run and taking it over",
                path, age_s, ttl_seconds,
            )
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            fd = _try_create_lock_file(path)
        if fd is None:
            holder = _read_lock_payload(path)
            detail = f"pid={holder.get('pid')}, since={holder.get('acquired_at')}" if holder else "unknown holder"
            raise RunLockError(
                f"another dealradar run is already in progress for this database (lock file: {path}, {detail}) "
                "-- refusing to start a second concurrent run"
            )

    with os.fdopen(fd, "wb") as fh:
        fh.write(payload)
    try:
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _try_create_lock_file(path: Path) -> int | None:
    """Atomically creates path exclusively, returning its open file descriptor, or None if it already exists."""
    try:
        return os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return None


def _lock_age_seconds(path: Path) -> float | None:
    """Returns how many seconds old path's mtime is, or None if the file cannot be stat'd (e.g. a race)."""
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------------------------------
# Stage execution + aggregation helpers
# --------------------------------------------------------------------------------------------------


def _run_stage(session: Session, agg: RunReport, stage: str, work: Callable[[RunReport], object]) -> RunReport | None:
    """Runs one stage inside its own db.run_report transaction; folds the outcome into `agg`; returns the
    stage's own RunReport on success, or None if the stage raised (already logged to run_log and to `agg`).

    This is the mechanism behind "awaria etapu nie kasuje pracy poprzednich" (Zadanie 1): `db.run_report`
    commits the stage's own writes and its own run_log row before this function is even called again for the
    next stage, and an exception here only prevents *this* stage's work from being committed -- it never
    touches what already landed in the database.
    """
    try:
        with db.run_report(session, stage) as report:
            work(report)
    except Exception as exc:  # noqa: BLE001 -- Zadanie 1: one stage's failure must never abort the run
        agg.status = "partial" if agg.status == "ok" else agg.status
        agg.notes.append(f"stage '{stage}' FAILED: {type(exc).__name__}: {exc}")
        logger.exception("pipeline stage %s failed", stage)
        return None
    else:
        return report


def _fold_collect(agg: RunReport, report: RunReport) -> None:
    """Copies collect's counters into the aggregate report."""
    agg.offers_fetched += report.offers_fetched
    agg.offers_new += report.offers_new
    agg.sources_failed = [*agg.sources_failed, *report.sources_failed]
    agg.notes.extend(report.notes)


def _fold_normalize(agg: RunReport, report: RunReport) -> None:
    """Copies normalize's pct_with_ean into the aggregate report."""
    agg.pct_with_ean = report.pct_with_ean
    agg.notes.extend(report.notes)


def _fold_match(agg: RunReport, report: RunReport) -> None:
    """Copies match's pct_matched/products_new into the aggregate report."""
    agg.pct_matched = report.pct_matched
    agg.products_new = report.products_new
    agg.notes.extend(report.notes)


def _fold_snapshot(agg: RunReport, report: RunReport) -> None:
    """Copies snapshot's notes into the aggregate report (snapshot has no dedicated RunReport counters)."""
    agg.notes.extend(report.notes)


def _fold_score_deals(agg: RunReport, report: RunReport) -> None:
    """Copies score --deals's deals_scored into the aggregate report."""
    agg.deals_scored = report.deals_scored
    agg.notes.extend(report.notes)


def _fold_score_quality(agg: RunReport, report: RunReport) -> None:
    """Copies score --quality's quality_scored into the aggregate report."""
    agg.quality_scored = report.quality_scored
    agg.notes.extend(report.notes)


def _match_stage_work(session: Session, *, full: bool) -> Callable[[RunReport], None]:
    """Returns a `_run_stage` work callback that runs match_offers and writes its outcome onto a RunReport."""

    def _work(report: RunReport) -> None:
        match_report = match_offers(session, full=full)
        report.pct_matched = match_report.pct_matched
        report.products_new = match_report.products_created
        report.notes.append(match_report.summary_line())
        if match_report.large_clusters:
            report.notes.extend(match_report.large_clusters)

    return _work


def _snapshot_stage_work(session: Session, *, at: date | None = None) -> Callable[[RunReport], None]:
    """Returns a `_run_stage` work callback that runs snapshot_prices and writes its outcome onto a RunReport."""

    def _work(report: RunReport) -> None:
        result = snapshot_prices(session, at=at)
        report.notes.append(
            f"snapshot {result.snapshot_date}: seen_today={result.offers_seen_today} "
            f"out_of_stock_grace={result.offers_marked_out_of_stock} "
            f"stopped_tracking={result.offers_stopped_tracking}"
        )

    return _work


def _score_deals_stage_work(
    session: Session, *, product_ids: list[int] | None = None, at: date | None = None
) -> Callable[[RunReport], None]:
    """Returns a `_run_stage` work callback that runs score_deals and writes its outcome onto a RunReport."""

    def _work(report: RunReport) -> None:
        result = score_deals(session, product_ids, at=at)
        report.deals_scored = result.deals_scored
        report.notes.append(
            f"deal_score: scored={result.deals_scored} skipped_no_deal={result.skipped_no_deal} "
            f"basis={result.basis_counts} shop_credibility_written={result.shop_credibility_rows_written} "
            f"shop_credibility_null={result.shop_credibility_rows_null}"
        )

    return _work


def _score_quality_stage_work(session: Session, *, product_ids: list[int] | None = None) -> Callable[[RunReport], None]:
    """Returns a `_run_stage` work callback that runs score_quality and writes its outcome onto a RunReport."""

    def _work(report: RunReport) -> None:
        result = score_quality(session, product_ids)
        report.quality_scored = result.products_scored
        report.notes.append(result.summary_line())

    return _work


def _last_successful_finished_at(session: Session, stages: tuple[str, ...]) -> datetime | None:
    """Returns the most recent finished_at among run_log rows for any of `stages` with status != 'failed'.

    Used to compute a safe default `since` for collect/normalize on an orchestrated run: None on a cold
    start (no prior run), otherwise the last run's finish time, so normalize only reprocesses raw_offer rows
    collected since then (see this module's docstring for why re-normalizing untouched rows is unsafe).
    """
    placeholders = ", ".join(f":s{i}" for i in range(len(stages)))
    params: dict[str, Any] = {f"s{i}": s for i, s in enumerate(stages)}
    row = session.execute(
        text(
            f"SELECT finished_at FROM run_log WHERE stage IN ({placeholders}) "
            "AND status != 'failed' AND finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
        ),
        params,
    ).scalar_one_or_none()
    if row is None:
        return None
    return datetime.fromisoformat(str(row))


# --------------------------------------------------------------------------------------------------
# run_full / run_fast (Zadanie 1)
# --------------------------------------------------------------------------------------------------


def run_full(
    session: Session,
    cfg: AppConfig,
    *,
    since: datetime | None = None,
    cache_dir: Path | None = None,
    offline: bool = False,
) -> RunReport:
    """Runs the full nightly pipeline end to end (ARCHITEKTURA.md sec. 4/11): collect -> normalize -> match
    -> snapshot -> score --deals -> score --quality, and returns one aggregate RunReport (stage="run")
    summarizing the whole orchestrated run; each stage also writes its own separate run_log row.

    `since` defaults to the last successful orchestrated run's finish time (None on a cold start) so
    normalize does not reprocess -- and thereby un-match -- offers nothing changed for (see module
    docstring). If `match` raises, score --deals/--quality are skipped this run and a note explains why
    ("bez match nie ma score"), matching the task spec's explicit failure-handling requirement.
    """
    with acquire_run_lock(cfg.database_url):
        with db.run_report(session, "run") as agg:
            effective_since = since if since is not None else _last_successful_finished_at(session, ("run",))

            collect_report = _run_stage(
                session, agg, "collect",
                lambda r: run_collect(
                    session, cfg, r, since=effective_since, cache_dir=cache_dir or DEFAULT_CACHE_DIR, offline=offline
                ),
            )
            if collect_report is not None:
                _fold_collect(agg, collect_report)

            normalize_report = _run_stage(session, agg, "normalize", lambda r: run_normalize(session, r, since=effective_since))
            if normalize_report is not None:
                _fold_normalize(agg, normalize_report)

            match_report = _run_stage(session, agg, "match", _match_stage_work(session, full=False))
            if match_report is not None:
                _fold_match(agg, match_report)

            snapshot_report = _run_stage(session, agg, "snapshot", _snapshot_stage_work(session))
            if snapshot_report is not None:
                _fold_snapshot(agg, snapshot_report)

            if match_report is not None:
                deals_report = _run_stage(session, agg, "score_deals", _score_deals_stage_work(session))
                if deals_report is not None:
                    _fold_score_deals(agg, deals_report)

                quality_report = _run_stage(session, agg, "score_quality", _score_quality_stage_work(session))
                if quality_report is not None:
                    _fold_score_quality(agg, quality_report)
            else:
                agg.notes.append("skipped score --deals/--quality: match stage failed this run (bez match nie ma score)")

        return agg


def run_fast(
    session: Session,
    cfg: AppConfig,
    *,
    cache_dir: Path | None = None,
    offline: bool = False,
) -> RunReport:
    """Runs the hourly fast track (ARCHITEKTURA.md sec. 11: "tylko Pepper RSS + watchlista"): collects only
    the `pepper` source, matches/snapshots incrementally, and re-scores deal_score for only (a) products
    newly touched by this run's Pepper offers and (b) products on an active watch -- never a full
    score --quality pass (too expensive to run hourly; quality rarely changes within a day). Returns one
    aggregate RunReport (stage="run_fast").

    Design choice (ARCHITEKTURA.md doesn't spell out the exact scoring scope, only "Pepper + watchlist"):
    scoring newly-touched-by-Pepper products in addition to watched ones is what makes the "fast track"
    actually useful for catching a fresh hot Pepper deal same-hour, not just re-checking existing watches.
    """
    with acquire_run_lock(cfg.database_url):
        with db.run_report(session, "run_fast") as agg:
            effective_since = _last_successful_finished_at(session, ("run", "run_fast"))

            collect_report = _run_stage(
                session, agg, "collect",
                lambda r: run_collect(
                    session, cfg, r, source_name="pepper", since=effective_since,
                    cache_dir=cache_dir or DEFAULT_CACHE_DIR, offline=offline,
                ),
            )
            touched_product_ids: list[int] = []
            if collect_report is not None:
                _fold_collect(agg, collect_report)
                touched_product_ids = _products_touched_since(session, "pepper", effective_since)

            normalize_report = _run_stage(session, agg, "normalize", lambda r: run_normalize(session, r, since=effective_since))
            if normalize_report is not None:
                _fold_normalize(agg, normalize_report)

            match_report = _run_stage(session, agg, "match", _match_stage_work(session, full=False))
            if match_report is not None:
                _fold_match(agg, match_report)

            snapshot_report = _run_stage(session, agg, "snapshot", _snapshot_stage_work(session))
            if snapshot_report is not None:
                _fold_snapshot(agg, snapshot_report)

            if match_report is not None:
                watched_ids = _active_watch_product_ids(session)
                scope = sorted(set(touched_product_ids) | set(watched_ids))
                deals_report = _run_stage(session, agg, "score_deals", _score_deals_stage_work(session, product_ids=scope))
                if deals_report is not None:
                    _fold_score_deals(agg, deals_report)
                agg.notes.append(
                    f"run_fast score scope: {len(touched_product_ids)} pepper-touched + {len(watched_ids)} watched "
                    f"= {len(scope)} distinct product(s); score --quality skipped (hourly track)"
                )
            else:
                agg.notes.append("skipped score --deals: match stage failed this run (bez match nie ma score)")

        return agg


def _products_touched_since(session: Session, source_name: str, since: datetime | None) -> list[int]:
    """Returns distinct product_id values for offers of `source_name` with last_seen >= since (or all, if None)."""
    if since is None:
        row_sql = (
            "SELECT DISTINCT o.product_id FROM offer o JOIN source s ON s.id = o.source_id "
            "WHERE s.name = :name AND o.product_id IS NOT NULL"
        )
        params: dict[str, Any] = {"name": source_name}
    else:
        row_sql = (
            "SELECT DISTINCT o.product_id FROM offer o JOIN source s ON s.id = o.source_id "
            "WHERE s.name = :name AND o.product_id IS NOT NULL AND o.last_seen >= :since"
        )
        params = {"name": source_name, "since": since.isoformat()}
    return [int(r) for r in session.execute(text(row_sql), params).scalars().all()]


def _active_watch_product_ids(session: Session) -> list[int]:
    """Returns distinct product_id values from active watch rows that target a specific product."""
    rows = session.execute(
        text("SELECT DISTINCT product_id FROM watch WHERE active = 1 AND product_id IS NOT NULL")
    ).scalars().all()
    return [int(r) for r in rows]


# --------------------------------------------------------------------------------------------------
# rebuild (Zadanie 1, Z1)
# --------------------------------------------------------------------------------------------------

REBUILDABLE_STAGES = ("raw", "match", "score")
"""Valid `--from` values: "raw" (full re-derivation from raw_offer, the Z1 guarantee), "match" (re-run
match onward, keeping the current `offer` table as-is), "score" (just re-run score --deals/--quality)."""


def rebuild(session: Session, from_stage: str, since: date | None = None) -> RunReport:
    """Recomputes derived state from `from_stage` onward and returns an aggregate RunReport (Z1).

    `from_stage="raw"`: re-derives `offer` (via normalize_batch on every raw_offer), then wipes and
    recreates `product`/`product_attr`/`deal`/`quality_score`/`shop_credibility` before re-running match
    (full=True) and score --deals/--quality. Deliberately does *not* re-run snapshot_prices -- price_point is
    wall-clock-dependent collected history, not a pure function of raw_offer, and is not one of the tables
    the task spec lists as rebuild's output (see module docstring). `since` restricts *which* raw_offer rows
    normalize reprocesses (None = every row ever collected, the default "odtworz od zera" reading of Z1).

    `from_stage="match"`: re-runs match (full=True) onward without touching `offer` (useful after only the
    matcher's config/thresholds changed). `from_stage="score"`: re-runs score --deals/--quality only (useful
    after only scoring/quality formulas changed) -- the common case Z1 exists for ("algorytm scoringu
    zmienisz dziesiec razy").

    Raises ValueError for any other `from_stage` value.
    """
    if from_stage not in REBUILDABLE_STAGES:
        raise ValueError(f"unsupported rebuild --from {from_stage!r}; expected one of {REBUILDABLE_STAGES}")

    since_dt = datetime(since.year, since.month, since.day, tzinfo=timezone.utc) if since is not None else None
    database_url = _database_url_from_session(session)

    with acquire_run_lock(database_url):
        with db.run_report(session, f"rebuild --from {from_stage}") as agg:
            match_ok = True

            if from_stage == "raw":
                normalize_report = _run_stage(session, agg, "normalize", lambda r: run_normalize(session, r, since=since_dt))
                if normalize_report is not None:
                    _fold_normalize(agg, normalize_report)
                _clear_match_and_score_tables(session)
                agg.notes.append("rebuild --from raw: cleared product/product_attr/deal/quality_score/shop_credibility")
                match_report = _run_stage(session, agg, "match", _match_stage_work(session, full=True))
                match_ok = match_report is not None
                if match_report is not None:
                    _fold_match(agg, match_report)
            elif from_stage == "match":
                match_report = _run_stage(session, agg, "match", _match_stage_work(session, full=True))
                match_ok = match_report is not None
                if match_report is not None:
                    _fold_match(agg, match_report)
            # from_stage == "score": no re-matching requested, go straight to scoring on the current match.

            if match_ok:
                deals_report = _run_stage(session, agg, "score_deals", _score_deals_stage_work(session))
                if deals_report is not None:
                    _fold_score_deals(agg, deals_report)
                quality_report = _run_stage(session, agg, "score_quality", _score_quality_stage_work(session))
                if quality_report is not None:
                    _fold_score_quality(agg, quality_report)
            else:
                agg.notes.append("skipped score --deals/--quality: match stage failed this rebuild")

        return agg


def _database_url_from_session(session: Session) -> str:
    """Returns the connection-string URL of the engine session is bound to, for keying the run lock file."""
    bind = session.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    return str(engine.url)


def _clear_match_and_score_tables(session: Session) -> None:
    """Deletes every row from product-derived tables, in FK-safe order, ahead of a from-raw rebuild.

    Must run only *after* normalize_batch has already reset every offer.product_id to NULL this run (see
    module docstring) -- otherwise deleting `product` while `offer.product_id` still references it would
    violate the FK constraint (foreign_keys=ON, see db.build_engine). Safe to call on an empty database.
    """
    for stmt in (
        "DELETE FROM deal",
        "DELETE FROM shop_credibility",
        "DELETE FROM quality_score",
        "DELETE FROM product_attr",
        "DELETE FROM product",
    ):
        session.execute(text(stmt))
    session.commit()
