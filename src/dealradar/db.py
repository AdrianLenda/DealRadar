"""Sessions, engine, migration runner, and the small set of write helpers every stage shares. Owner: A0.

Design decision (see A0 final report for the full rationale): `session` throughout this codebase — and in
every downstream agent's function contract (`normalize_batch(session, ...)`, `match_offers(session, ...)`,
`score_deals(session, ...)`, etc.) — means a `sqlalchemy.orm.Session`. We use SQLAlchemy only as a thin
engine/transaction/connection-string wrapper: there are no declarative ORM-mapped classes anywhere. Reads
and writes go through raw SQL (`sqlalchemy.text`), and `pydantic` models are translated to/from rows at the
boundary. That keeps the access layer thin enough that moving to Postgres is a connection-string change,
per the hard requirement in ARCHITEKTURA.md §5, EXCEPT for `run_migrations` itself, which shells out to
SQLite's `executescript` and would need a different implementation (e.g. one statement at a time) on
Postgres — migrations are explicitly allowed to be SQLite-specific per the task spec, this is the one place
that is.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from dealradar.models import Offer, RawOffer, RunReport, Source

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


# --------------------------------------------------------------------------------------------------
# Engine / session plumbing
# --------------------------------------------------------------------------------------------------


def build_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Creates a SQLAlchemy engine for database_url, wiring WAL + foreign_keys PRAGMAs onto every SQLite connection."""
    is_sqlite = database_url.startswith("sqlite")
    is_memory = is_sqlite and ":memory:" in database_url
    connect_args: dict[str, Any] = {"check_same_thread": False} if is_memory else {}
    extra_kwargs: dict[str, Any] = {"poolclass": StaticPool} if is_memory else {}

    engine = create_engine(database_url, echo=echo, connect_args=connect_args, **extra_kwargs)

    if is_sqlite:

        @event.listens_for(engine, "connect")
        def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:
            """Enables foreign-key enforcement and WAL journaling on every new raw SQLite connection."""
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Returns a sessionmaker bound to engine; call it once per unit of work to open a Session."""
    return sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Opens a Session, commits on a clean exit, rolls back and re-raises on error; always closes the session."""
    session = make_session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------------------------------


def run_migrations(engine: Engine, migrations_dir: Path | None = None) -> list[str]:
    """Applies every not-yet-applied numbered .sql file in migrations_dir in order; returns the filenames applied."""
    directory = migrations_dir or MIGRATIONS_DIR
    files = sorted(directory.glob("*.sql"), key=lambda p: p.name)

    raw_conn = engine.raw_connection()
    applied: list[str] = []
    try:
        cursor = raw_conn.cursor()
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        raw_conn.commit()
        cursor.execute("SELECT version FROM schema_version")
        already_applied = {row[0] for row in cursor.fetchall()}

        for path in files:
            if path.name in already_applied:
                continue
            cursor.executescript(path.read_text(encoding="utf-8"))
            cursor.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (path.name, datetime.now(timezone.utc).isoformat()),
            )
            raw_conn.commit()
            applied.append(path.name)
        cursor.close()
    finally:
        raw_conn.close()
    return applied


# --------------------------------------------------------------------------------------------------
# Write helpers
# --------------------------------------------------------------------------------------------------


def upsert_source(session: Session, source: Source) -> int:
    """Inserts or updates a source row keyed by its unique name; returns the row's id."""
    result = session.execute(
        text(
            """
            INSERT INTO source (name, kind, base_url, enabled, risk_prior)
            VALUES (:name, :kind, :base_url, :enabled, :risk_prior)
            ON CONFLICT(name) DO UPDATE SET
                kind = excluded.kind,
                base_url = excluded.base_url,
                enabled = excluded.enabled,
                risk_prior = excluded.risk_prior
            RETURNING id
            """
        ),
        {
            "name": source.name,
            "kind": source.kind,
            "base_url": source.base_url,
            "enabled": int(source.enabled),
            "risk_prior": source.risk_prior,
        },
    )
    return int(result.scalar_one())


def insert_raw_offer(session: Session, raw: RawOffer) -> int:
    """Inserts raw into raw_offer, or returns the id of an existing row sharing its (source_id, content_hash)."""
    existing = session.execute(
        text("SELECT id FROM raw_offer WHERE source_id = :source_id AND content_hash = :content_hash"),
        {"source_id": raw.source_id, "content_hash": raw.content_hash},
    ).scalar_one_or_none()
    if existing is not None:
        return int(existing)

    result = session.execute(
        text(
            """
            INSERT INTO raw_offer (source_id, external_id, fetched_at, payload_json, content_hash)
            VALUES (:source_id, :external_id, :fetched_at, :payload_json, :content_hash)
            RETURNING id
            """
        ),
        {
            "source_id": raw.source_id,
            "external_id": raw.external_id,
            "fetched_at": raw.fetched_at.isoformat(),
            "payload_json": json.dumps(raw.payload_json, ensure_ascii=False, default=str),
            "content_hash": raw.content_hash,
        },
    )
    return int(result.scalar_one())


def upsert_offer(session: Session, offer: Offer) -> int:
    """Inserts or updates an offer keyed by (source_id, external_id), preserving first_seen; returns the row id."""
    params: dict[str, Any] = {
        "source_id": offer.source_id,
        "external_id": offer.external_id,
        "first_seen": offer.first_seen.isoformat(),
        "last_seen": offer.last_seen.isoformat(),
        "title": offer.title,
        "title_norm": offer.title_norm,
        "brand": offer.brand,
        "brand_norm": offer.brand_norm,
        "mpn": offer.mpn,
        "mpn_norm": offer.mpn_norm,
        "ean": offer.ean,
        "category_raw": offer.category_raw,
        "category_id": offer.category_id,
        "url": offer.url,
        "seller": offer.seller,
        "seller_rating": offer.seller_rating,
        "condition": offer.condition,
        "is_bundle": int(offer.is_bundle),
        "bundle_size": offer.bundle_size,
        "price_gross": offer.price_gross,
        "shipping_cost": offer.shipping_cost,
        "price_total": offer.price_total,
        "currency": offer.currency,
        "price_incomplete": int(offer.price_incomplete),
        "list_price_claimed": offer.list_price_claimed,
        "omnibus_min_30d": offer.omnibus_min_30d,
        "rating_avg": offer.rating_avg,
        "rating_count": offer.rating_count,
        "warranty_months": offer.warranty_months,
        "in_stock": None if offer.in_stock is None else int(offer.in_stock),
        "product_id": offer.product_id,
        "match_confidence": offer.match_confidence,
        "match_method": offer.match_method,
        "data_issues": json.dumps(offer.data_issues, ensure_ascii=False),
        "evidence": json.dumps(offer.evidence, ensure_ascii=False, default=str),
    }
    result = session.execute(
        text(
            """
            INSERT INTO offer (
                source_id, external_id, first_seen, last_seen, title, title_norm,
                brand, brand_norm, mpn, mpn_norm, ean, category_raw, category_id,
                url, seller, seller_rating, condition, is_bundle, bundle_size,
                price_gross, shipping_cost, price_total, currency, price_incomplete,
                list_price_claimed, omnibus_min_30d, rating_avg, rating_count,
                warranty_months, in_stock, product_id, match_confidence, match_method,
                data_issues, evidence
            ) VALUES (
                :source_id, :external_id, :first_seen, :last_seen, :title, :title_norm,
                :brand, :brand_norm, :mpn, :mpn_norm, :ean, :category_raw, :category_id,
                :url, :seller, :seller_rating, :condition, :is_bundle, :bundle_size,
                :price_gross, :shipping_cost, :price_total, :currency, :price_incomplete,
                :list_price_claimed, :omnibus_min_30d, :rating_avg, :rating_count,
                :warranty_months, :in_stock, :product_id, :match_confidence, :match_method,
                :data_issues, :evidence
            )
            ON CONFLICT(source_id, external_id) DO UPDATE SET
                last_seen = excluded.last_seen,
                title = excluded.title,
                title_norm = excluded.title_norm,
                brand = excluded.brand,
                brand_norm = excluded.brand_norm,
                mpn = excluded.mpn,
                mpn_norm = excluded.mpn_norm,
                ean = excluded.ean,
                category_raw = excluded.category_raw,
                category_id = excluded.category_id,
                url = excluded.url,
                seller = excluded.seller,
                seller_rating = excluded.seller_rating,
                condition = excluded.condition,
                is_bundle = excluded.is_bundle,
                bundle_size = excluded.bundle_size,
                price_gross = excluded.price_gross,
                shipping_cost = excluded.shipping_cost,
                price_total = excluded.price_total,
                currency = excluded.currency,
                price_incomplete = excluded.price_incomplete,
                list_price_claimed = excluded.list_price_claimed,
                omnibus_min_30d = excluded.omnibus_min_30d,
                rating_avg = excluded.rating_avg,
                rating_count = excluded.rating_count,
                warranty_months = excluded.warranty_months,
                in_stock = excluded.in_stock,
                product_id = excluded.product_id,
                match_confidence = excluded.match_confidence,
                match_method = excluded.match_method,
                data_issues = excluded.data_issues,
                evidence = excluded.evidence
            RETURNING id
            -- Deliberately NOT updating first_seen: idempotent upsert must not overwrite the original sighting.
            """
        ),
        params,
    )
    return int(result.scalar_one())


def get_offer_by_source_external_id(session: Session, source_id: int, external_id: str) -> Offer | None:
    """Returns the offer matching (source_id, external_id), or None if no such offer has been stored yet."""
    row = session.execute(
        text("SELECT * FROM offer WHERE source_id = :source_id AND external_id = :external_id"),
        {"source_id": source_id, "external_id": external_id},
    ).mappings().first()
    if row is None:
        return None
    return _row_to_offer(row)


def _row_to_offer(row: Any) -> Offer:
    """Converts an `offer` table row mapping into an Offer, dropping computed columns pydantic will recompute."""
    data = dict(row)
    data.pop("price_total", None)
    data.pop("price_incomplete", None)
    data["is_bundle"] = bool(data["is_bundle"])
    data["in_stock"] = None if data["in_stock"] is None else bool(data["in_stock"])
    data["data_issues"] = json.loads(data["data_issues"]) if data.get("data_issues") else []
    data["evidence"] = json.loads(data["evidence"]) if data.get("evidence") else {}
    return Offer(**data)


def append_price_point(
    session: Session, offer_id: int, price_total: int, in_stock: bool, observed_at: datetime
) -> int:
    """Writes/updates the single price_point for offer_id on observed_at's calendar day; returns the row id."""
    if isinstance(price_total, float):
        raise TypeError("price_total must be an int number of grosze, not a float")
    result = session.execute(
        text(
            """
            INSERT INTO price_point (offer_id, observed_at, price_total, in_stock)
            VALUES (:offer_id, :observed_at, :price_total, :in_stock)
            ON CONFLICT(offer_id, date(observed_at)) DO UPDATE SET
                observed_at = excluded.observed_at,
                price_total = excluded.price_total,
                in_stock = excluded.in_stock
            RETURNING id
            """
        ),
        {
            "offer_id": offer_id,
            "observed_at": observed_at.isoformat(),
            "price_total": price_total,
            "in_stock": int(in_stock),
        },
    )
    return int(result.scalar_one())


# --------------------------------------------------------------------------------------------------
# run_report (Z7)
# --------------------------------------------------------------------------------------------------


@contextmanager
def run_report(session: Session, stage: str) -> Iterator[RunReport]:
    """Times `stage`, yields a mutable RunReport for the caller to fill in, and always persists it to run_log.

    On a clean exit the report's own status/finished_at/duration_s are filled in and both the stage's work
    and the report row are committed. On an exception, the stage's work is rolled back, the report is marked
    'failed' with the exception recorded in `notes`, that failure is committed as its own transaction, and
    the exception is re-raised — a broken stage never silently disappears from run_log (Z7).
    """
    started = datetime.now(timezone.utc)
    report = RunReport(stage=stage, started_at=started)
    try:
        yield report
    except Exception as exc:
        session.rollback()
        report.status = "failed"
        report.notes = [*report.notes, f"{type(exc).__name__}: {exc}"]
        report.finished_at = datetime.now(timezone.utc)
        report.duration_s = (report.finished_at - started).total_seconds()
        _insert_run_log(session, report)
        session.commit()
        raise
    else:
        if report.sources_failed and report.status == "ok":
            report.status = "partial"
        report.finished_at = datetime.now(timezone.utc)
        report.duration_s = (report.finished_at - started).total_seconds()
        session.commit()
        _insert_run_log(session, report)
        session.commit()


def _insert_run_log(session: Session, report: RunReport) -> int:
    """Writes report as a new row in run_log; returns the row id."""
    result = session.execute(
        text(
            """
            INSERT INTO run_log (
                stage, started_at, finished_at, offers_fetched, offers_new,
                pct_with_ean, pct_matched, products_new, deals_scored,
                quality_scored, sources_failed, duration_s, status, notes
            ) VALUES (
                :stage, :started_at, :finished_at, :offers_fetched, :offers_new,
                :pct_with_ean, :pct_matched, :products_new, :deals_scored,
                :quality_scored, :sources_failed, :duration_s, :status, :notes
            ) RETURNING id
            """
        ),
        {
            "stage": report.stage,
            "started_at": report.started_at.isoformat(),
            "finished_at": report.finished_at.isoformat() if report.finished_at else None,
            "offers_fetched": report.offers_fetched,
            "offers_new": report.offers_new,
            "pct_with_ean": report.pct_with_ean,
            "pct_matched": report.pct_matched,
            "products_new": report.products_new,
            "deals_scored": report.deals_scored,
            "quality_scored": report.quality_scored,
            "sources_failed": json.dumps(report.sources_failed),
            "duration_s": report.duration_s,
            "status": report.status,
            "notes": json.dumps(report.notes),
        },
    )
    return int(result.scalar_one())
