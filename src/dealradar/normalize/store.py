"""Persistence for RejectedOffer: a real table, not an in-memory list (A2 task spec: "ladduje w tabeli, nie w koszu").

**Deviation flagged for the team (see A2 final report):** the `rejected_offer` table is not part of A0's
`migrations/001_init.sql` and A2 does not own (and was told not to touch) `migrations/` or `db.py`. Rather
than block on that, this module creates its own table with `CREATE TABLE IF NOT EXISTS` the first time
normalize runs, using plain SQL through the same `Session`/`text()` pattern `db.py` uses elsewhere -- no ORM
mapped classes, no change to A0's files, safe to run concurrently with sibling agents (a fresh, uniquely-
named table create does not touch anything they own). If A0/A6 later fold this into a numbered migration,
this file's `ensure_rejected_offer_table` becomes a no-op and can be deleted.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.normalize.report import RejectedOffer

_DDL = """
CREATE TABLE IF NOT EXISTS rejected_offer (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_offer_id   INTEGER NOT NULL,
    source_id      INTEGER NOT NULL,
    external_id    TEXT NOT NULL,
    reason         TEXT NOT NULL,
    detail         TEXT NOT NULL,
    rejected_at    TEXT NOT NULL
)
"""
_INDEX_DDL = "CREATE INDEX IF NOT EXISTS idx_rejected_offer_reason ON rejected_offer (reason)"


def ensure_rejected_offer_table(session: Session) -> None:
    """Creates the rejected_offer table and its reason index if they do not already exist; idempotent, no-op after."""
    session.execute(text(_DDL))
    session.execute(text(_INDEX_DDL))


def insert_rejected_offer(session: Session, rejected: RejectedOffer) -> int:
    """Inserts one RejectedOffer row; returns its id. Assumes ensure_rejected_offer_table has already run."""
    result = session.execute(
        text(
            """
            INSERT INTO rejected_offer (raw_offer_id, source_id, external_id, reason, detail, rejected_at)
            VALUES (:raw_offer_id, :source_id, :external_id, :reason, :detail, :rejected_at)
            RETURNING id
            """
        ),
        {
            "raw_offer_id": rejected.raw_offer_id,
            "source_id": rejected.source_id,
            "external_id": rejected.external_id,
            "reason": rejected.reason,
            "detail": rejected.detail,
            "rejected_at": rejected.rejected_at.isoformat(),
        },
    )
    return int(result.scalar_one())


def count_rejected_by_reason(session: Session) -> dict[str, int]:
    """Returns {reason: count} across every row ever written to rejected_offer, for ad-hoc reporting/debugging."""
    rows = session.execute(text("SELECT reason, COUNT(*) FROM rejected_offer GROUP BY reason")).all()
    return {str(reason): int(count) for reason, count in rows}


def fetch_raw_offers(
    session: Session, *, since_iso: str | None
) -> list[dict[str, Any]]:
    """Returns raw_offer rows (as plain dicts, payload_json still a JSON string) with fetched_at >= since_iso.

    Ordered by id for deterministic, reproducible normalize runs -- rerunning `dealradar normalize` on the
    same raw_offer table processes rows in the same order every time.
    """
    if since_iso is None:
        rows = session.execute(
            text("SELECT id, source_id, external_id, fetched_at, payload_json, content_hash FROM raw_offer ORDER BY id")
        ).mappings().all()
    else:
        rows = session.execute(
            text(
                "SELECT id, source_id, external_id, fetched_at, payload_json, content_hash FROM raw_offer "
                "WHERE fetched_at >= :since ORDER BY id"
            ),
            {"since": since_iso},
        ).mappings().all()
    return [dict(row) for row in rows]


def fetch_sources(session: Session) -> dict[int, tuple[str, str]]:
    """Returns {source_id: (name, kind)} for every registered source, used to dispatch the right normalizer."""
    rows = session.execute(text("SELECT id, name, kind FROM source")).all()
    return {int(row[0]): (str(row[1]), str(row[2])) for row in rows}


def parse_payload_json(raw: str) -> dict[str, Any]:
    """Parses a raw_offer.payload_json TEXT column value back into a dict."""
    value: dict[str, Any] = json.loads(raw)
    return value
