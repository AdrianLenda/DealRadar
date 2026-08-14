"""DealRadar CLI (typer). Owner: A0.

Every stage command is currently a stub: it migrates the schema, opens a run_report for its stage, logs a
"not implemented yet" note naming the owning agent, and prints + persists the resulting RunReport (Z7).
Downstream agents replace the body of their own stage's stub with real work; they do not need to touch the
command wiring itself.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import typer
from sqlalchemy.orm import Session

from dealradar import db, pipeline, reporting
from dealradar.collectors.runner import run_collect
from dealradar.config import AppConfig, load_config
from dealradar.errors import ConfigError, RunLockError
from dealradar.matching import build_eval_dataset, load_matching_config
from dealradar.matching import evaluate as run_match_eval
from dealradar.matching import match_offers as run_match
from dealradar.models import RunReport, Watch
from dealradar.normalize import fill_run_report as run_normalize
from dealradar.scoring.deal import score_deals as run_score_deals
from dealradar.scoring.history import snapshot_prices as run_snapshot_prices
from dealradar.scoring.quality import score_quality as run_score_quality

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("dealradar.cli")

app = typer.Typer(help="DealRadar — deterministic discount and value-for-money detector. No LLM calls anywhere.")
db_app = typer.Typer(help="Database schema management.")
app.add_typer(db_app, name="db")
watch_app = typer.Typer(help="Watchlist CRUD (Zadanie 4: max-price / min-deal-score / any-alltime-low alerts).")
app.add_typer(watch_app, name="watch")

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _default_db_url() -> str:
    """Returns the sqlite connection string for the project's default database file."""
    return f"sqlite:///{PROJECT_ROOT / 'dealradar.db'}"


def _load_app_config(db_url: Optional[str]) -> AppConfig:
    """Loads AppConfig from config/, exiting the process with an error message if it is missing or invalid."""
    try:
        return load_config(database_url=db_url or _default_db_url())
    except ConfigError as exc:
        typer.secho(f"config error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc


def _print_report(report: RunReport) -> None:
    """Prints a run report's one-line summary to stdout."""
    typer.echo(report.summary_line())


@db_app.command("migrate")
def db_migrate(
    db_url: Optional[str] = typer.Option(None, "--db-url", help="Override the database connection string."),
) -> None:
    """Applies all pending schema migrations, creating the database file if it does not exist yet."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    applied = db.run_migrations(engine)
    if applied:
        typer.echo(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        typer.echo("database already up to date, nothing to apply")


def _parse_since(value: Optional[str]) -> Optional[datetime]:
    """Parses an ISO date/datetime string into a timezone-aware datetime (assuming UTC if none given)."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


@app.command()
def collect(
    source: Optional[str] = typer.Option(None, "--source", help="Only collect from this source name."),
    since: Optional[str] = typer.Option(None, "--since", help="ISO date; only fetch offers newer than this."),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Runs etap 1 (collect): fetches raw offers from configured sources (see collectors/runner.py, owner A1)."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        with db.run_report(session, "collect") as report:
            run_collect(
                session,
                cfg,
                report,
                source_name=source,
                since=_parse_since(since),
                cache_dir=PROJECT_ROOT / ".cache" / "collectors",
                offline=False,
            )
    _print_report(report)


@app.command()
def normalize(
    since: Optional[str] = typer.Option(None, "--since"),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Runs etap 2 (normalize): raw_offer -> offer, with rejections and coverage in the report (see normalize/pipeline.py, owner A2)."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        with db.run_report(session, "normalize") as report:
            run_normalize(session, report, since=_parse_since(since))
    _print_report(report)


@app.command()
def match(
    full: bool = typer.Option(False, "--full"),
    eval_: bool = typer.Option(False, "--eval"),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Runs etap 3 (match): offer -> product clusters via the EAN/brand+MPN/fuzzy cascade (see matching/, owner A3).

    `--eval` additionally prints precision/recall against match_review's manually labeled pairs
    (Z5) instead of running the cascade against the live offer table.
    """
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        if eval_:
            eval_report = run_match_eval(session)
            typer.echo(eval_report.summary_line())
            for row in eval_report.by_threshold:
                p = f"{row.precision:.3f}" if row.precision is not None else "n/a"
                r = f"{row.recall:.3f}" if row.recall is not None else "n/a"
                typer.echo(f"  threshold={row.threshold:.2f} precision={p} recall={r} tp={row.true_positives} fp={row.false_positives} fn={row.false_negatives}")
            if eval_report.false_positive_pairs:
                typer.echo(f"  false positives (offer_a, offer_b): {eval_report.false_positive_pairs}")
            return
        with db.run_report(session, "match") as report:
            match_report = run_match(session, full=full)
            report.pct_matched = match_report.pct_matched
            report.products_new = match_report.products_created
            report.notes.append(match_report.summary_line())
            if match_report.large_clusters:
                report.notes.extend(match_report.large_clusters)
    _print_report(report)


@app.command()
def snapshot(db_url: Optional[str] = typer.Option(None, "--db-url")) -> None:
    """Runs etap 4 (snapshot): writes today's price_point for every active offer (see scoring/history.py, owner A4)."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        with db.run_report(session, "snapshot") as report:
            result = run_snapshot_prices(session)
            report.notes.append(
                f"snapshot {result.snapshot_date}: seen_today={result.offers_seen_today} "
                f"out_of_stock_grace={result.offers_marked_out_of_stock} "
                f"stopped_tracking={result.offers_stopped_tracking}"
            )
    _print_report(report)


@app.command()
def score(
    deals: bool = typer.Option(False, "--deals"),
    quality: bool = typer.Option(False, "--quality"),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Runs etap 5 (score): --deals computes deal_score + shop_credibility (scoring/deal.py, owner A4);
    --quality computes the 1-10 value-for-money rubric (scoring/quality.py, owner A5)."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        with db.run_report(session, "score") as report:
            if deals:
                result = run_score_deals(session)
                report.deals_scored = result.deals_scored
                report.notes.append(
                    f"deal_score: scored={result.deals_scored} skipped_no_deal={result.skipped_no_deal} "
                    f"basis={result.basis_counts} shop_credibility_written={result.shop_credibility_rows_written} "
                    f"shop_credibility_null={result.shop_credibility_rows_null}"
                )
            if quality:
                quality_result = run_score_quality(session)
                report.quality_scored = quality_result.products_scored
                report.notes.append(quality_result.summary_line())
            if not deals and not quality:
                report.notes.append("no --deals or --quality given; nothing to do")
    _print_report(report)


@app.command()
def report(
    format: str = typer.Option("text", "--format", help="'text' (terminal/mail) or 'html' (standalone file)."),
    out: Optional[Path] = typer.Option(None, "--out", help="Output path. text: stdout if omitted. html: defaults to ./digest.html."),
    top_n: int = typer.Option(reporting.DEFAULT_TOP_N, "--top-n", help="How many top deals to include."),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Runs etap 6 (report): assembles the daily digest purely from already-computed deal/quality_score/
    run_log data (see reporting/digest.py, owner A6) and prints it or writes it to disk. Never mutates the
    database and never sends notifications -- see `dealradar watch check` for that.
    """
    if format not in ("text", "html"):
        typer.secho(f"unknown --format {format!r}; expected 'text' or 'html'", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        digest = reporting.build_digest(session, top_n=top_n)
        if format == "html":
            content = reporting.render_html(digest, session)
            target = out or Path("digest.html")
            target.write_text(content, encoding="utf-8")
            typer.echo(f"wrote HTML digest to {target}")
        else:
            content = reporting.render_text(digest)
            if out is not None:
                out.write_text(content, encoding="utf-8")
                typer.echo(f"wrote text digest to {out}")
            else:
                typer.echo(content)


@app.command()
def run(
    since: Optional[str] = typer.Option(
        None, "--since", help="ISO date/datetime; overrides the automatic incremental default (last successful run)."
    ),
    offline: bool = typer.Option(False, "--offline", help="Never touch the network; only use cached collector responses."),
    notify: bool = typer.Option(True, "--notify/--no-notify", help="Evaluate the watchlist and send notifications afterwards."),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Runs the full nightly pipeline end to end -- collect -> normalize -> match -> snapshot ->
    score --deals -> score --quality (pipeline.run_full, owner A6) -- then evaluates the watchlist and
    prints the resulting digest. Refuses to start (RunLockError) if another run is already in progress on
    this database (see pipeline.acquire_run_lock).
    """
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    try:
        with db.session_scope(engine) as session:
            full_report = pipeline.run_full(
                session, cfg, since=_parse_since(since), cache_dir=PROJECT_ROOT / ".cache" / "collectors", offline=offline
            )
    except RunLockError as exc:
        typer.secho(f"run locked: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    _print_report(full_report)

    with db.session_scope(engine) as session:
        if notify:
            hits = reporting.evaluate_watches(session)
            notify_report = reporting.notify_watch_hits(session, hits)
            typer.echo(
                f"watchlist: hits_found={notify_report.hits_found} sent={notify_report.sent} "
                f"suppressed_antispam={notify_report.suppressed_antispam} channel_disabled={notify_report.channel_disabled}"
            )
        digest = reporting.build_digest(session)
        typer.echo(reporting.render_text(digest))


@app.command()
def rebuild(
    from_: str = typer.Option(..., "--from", help="Stage to rebuild from: 'raw' (Z1), 'match', or 'score'."),
    since: Optional[str] = typer.Option(None, "--since", help="ISO date; only reprocess raw_offer rows fetched since then (--from raw only)."),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Runs Z1's rebuild: recomputes offer/product/deal/quality_score from raw_offer (or resumes from a
    later stage) without re-collecting anything over the network (pipeline.rebuild, owner A6).
    """
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    since_date = date.fromisoformat(since) if since is not None else None
    try:
        with db.session_scope(engine) as session:
            rebuild_report = pipeline.rebuild(session, from_, since_date)
    except RunLockError as exc:
        typer.secho(f"rebuild locked: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    except ValueError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    _print_report(rebuild_report)


# --------------------------------------------------------------------------------------------------
# watch (Zadanie 4: CRUD + manual notification check)
# --------------------------------------------------------------------------------------------------


@watch_app.command("add")
def watch_add(
    product_id: Optional[int] = typer.Option(None, "--product-id", help="Existing product.id to watch."),
    query: Optional[str] = typer.Option(
        None, "--query", help="Free-text product name, for a product not yet in the database (resolved on each evaluation)."
    ),
    max_price: Optional[float] = typer.Option(None, "--max-price", help="Zloty; alert when price_total drops to or below this."),
    min_deal_score: Optional[float] = typer.Option(None, "--min-deal-score", help="0-100; alert when deal_score reaches this."),
    any_alltime_low: bool = typer.Option(False, "--any-alltime-low", help="Alert whenever the price hits a new all-time low."),
    channel: str = typer.Option("console", "--channel", help="console, file, smtp, or telegram."),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Adds a new watch. Requires --product-id or --query (Watch's own validator enforces this) and at least
    one condition (--max-price / --min-deal-score / --any-alltime-low), or it will never fire.
    """
    if product_id is None and not query:
        typer.secho("watch add needs --product-id or --query", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)
    if max_price is None and min_deal_score is None and not any_alltime_low:
        typer.secho(
            "warning: no condition given (--max-price / --min-deal-score / --any-alltime-low) -- this watch will never fire",
            fg=typer.colors.YELLOW, err=True,
        )
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        watch = Watch(
            product_id=product_id,
            query=query,
            max_price=int(round(max_price * 100)) if max_price is not None else None,
            min_deal_score=min_deal_score,
            channel=channel,  # type: ignore[arg-type]
        )
        new_id = reporting.add_watch(session, watch, any_alltime_low=any_alltime_low)
    typer.echo(f"added watch #{new_id}")


@watch_app.command("list")
def watch_list(
    active_only: bool = typer.Option(False, "--active-only"),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Lists every watch (or only active ones with --active-only)."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        watches = reporting.list_watches(session, active_only=active_only)
        if not watches:
            typer.echo("no watches configured")
            return
        for w in watches:
            target = f"product_id={w.product_id}" if w.product_id is not None else f"query={w.query!r}"
            max_price_zl = f"{w.max_price / 100:.2f} zl" if w.max_price is not None else "n/a"
            alltime = reporting.get_watch_any_alltime_low(session, w.id) if w.id is not None else False
            typer.echo(
                f"#{w.id} {target} active={w.active} channel={w.channel} "
                f"max_price={max_price_zl} min_deal_score={w.min_deal_score} any_alltime_low={alltime}"
            )


@watch_app.command("remove")
def watch_remove(watch_id: int = typer.Argument(...), db_url: Optional[str] = typer.Option(None, "--db-url")) -> None:
    """Deletes a watch by id."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        removed = reporting.remove_watch(session, watch_id)
    if removed:
        typer.echo(f"removed watch #{watch_id}")
    else:
        typer.secho(f"no watch #{watch_id}", fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1)


@watch_app.command("update")
def watch_update(
    watch_id: int = typer.Argument(...),
    max_price: Optional[float] = typer.Option(None, "--max-price", help="Zloty. Pass -1 to clear an existing threshold."),
    min_deal_score: Optional[float] = typer.Option(None, "--min-deal-score", help="0-100. Pass -1 to clear an existing threshold."),
    any_alltime_low: Optional[bool] = typer.Option(None, "--any-alltime-low/--no-any-alltime-low"),
    channel: Optional[str] = typer.Option(None, "--channel"),
    db_url: Optional[str] = typer.Option(None, "--db-url"),
) -> None:
    """Edits an existing watch's conditions/channel. Only the flags you pass are changed; a bare "-1" for
    --max-price/--min-deal-score clears that threshold back to "no condition" (None).
    """
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        kwargs: dict[str, object] = {}
        if max_price is not None:
            kwargs["max_price"] = None if max_price < 0 else int(round(max_price * 100))
        if min_deal_score is not None:
            kwargs["min_deal_score"] = None if min_deal_score < 0 else min_deal_score
        if channel is not None:
            kwargs["channel"] = channel
        if any_alltime_low is not None:
            kwargs["any_alltime_low"] = any_alltime_low
        updated = reporting.update_watch(session, watch_id, **kwargs)  # type: ignore[arg-type]
    typer.echo(f"watch #{watch_id} updated" if updated else f"no watch #{watch_id} or nothing to change")


@watch_app.command("pause")
def watch_pause(watch_id: int = typer.Argument(...), db_url: Optional[str] = typer.Option(None, "--db-url")) -> None:
    """Deactivates a watch (active=False) without deleting it."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        ok = reporting.set_watch_active(session, watch_id, False)
    typer.echo(f"watch #{watch_id} paused" if ok else f"no watch #{watch_id}")


@watch_app.command("resume")
def watch_resume(watch_id: int = typer.Argument(...), db_url: Optional[str] = typer.Option(None, "--db-url")) -> None:
    """Reactivates a previously paused watch (active=True)."""
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        ok = reporting.set_watch_active(session, watch_id, True)
    typer.echo(f"watch #{watch_id} resumed" if ok else f"no watch #{watch_id}")


@watch_app.command("check")
def watch_check(db_url: Optional[str] = typer.Option(None, "--db-url")) -> None:
    """Evaluates every active watch against the latest deal_score data and sends notifications (with
    antispam) right now, without running the rest of the pipeline -- useful between scheduled runs.
    """
    cfg = _load_app_config(db_url)
    engine = db.build_engine(cfg.database_url)
    db.run_migrations(engine)
    with db.session_scope(engine) as session:
        hits = reporting.evaluate_watches(session)
        notify_report = reporting.notify_watch_hits(session, hits)
    typer.echo(
        f"watchlist: hits_found={notify_report.hits_found} sent={notify_report.sent} "
        f"suppressed_antispam={notify_report.suppressed_antispam} channel_disabled={notify_report.channel_disabled}"
    )


def main() -> None:
    """Entry point for `python -m dealradar.cli`; runs the typer app."""
    app()


if __name__ == "__main__":
    main()
