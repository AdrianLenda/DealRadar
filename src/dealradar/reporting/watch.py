"""Watchlist CRUD, hit evaluation, and notification delivery (Zadanie 4). Owner: A6.

Three pieces:

- CRUD over the `watch` table (already in migrations/001_init.sql, owner A0) via plain SQL, matching the
  rest of the codebase's no-ORM-mapped-classes convention (see db.py's module docstring).
- `evaluate_watches`: for every active watch, checks its condition (`max_price`, `min_deal_score`,
  `any_alltime_low`) against the *latest* deal for the product(s) it targets, using only numbers already
  computed by A4's `score --deals` -- this module never recomputes a deal_score or a price threshold itself.
- `Notifier` plugins (`console`, `file`, `smtp`, `telegram`) behind one interface, each disabling itself
  gracefully (never raising) when its required configuration/secret is absent -- a run must never fail
  because a channel wasn't configured. Antispam is enforced by `notification_log`, created defensively here
  (`CREATE TABLE IF NOT EXISTS`, migrations/ is off-limits) following the exact pattern A2 already used for
  `rejected_offer` (see normalize/store.py).
"""

from __future__ import annotations

import json
import logging
import smtplib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.config import get_secret
from dealradar.models import NotifyChannel, Watch

logger = logging.getLogger("dealradar.reporting.watch")

# --------------------------------------------------------------------------------------------------
# watch_flags -- ARCHITEKTURA.md's `watch` table (migrations/001_init.sql, owner A0) has no column for the
# third condition the task spec requires ("any_alltime_low"); models.py/migrations/ are off-limits to A6.
# Following the exact precedent A2 set for rejected_offer (normalize/store.py): a small companion table,
# created defensively with CREATE TABLE IF NOT EXISTS, keyed 1:1 on watch.id.
# --------------------------------------------------------------------------------------------------

_WATCH_FLAGS_DDL = """
CREATE TABLE IF NOT EXISTS watch_flags (
    watch_id          INTEGER PRIMARY KEY,
    any_alltime_low   INTEGER NOT NULL DEFAULT 0
)
"""


def ensure_watch_flags_table(session: Session) -> None:
    """Creates watch_flags if it does not already exist; idempotent."""
    session.execute(text(_WATCH_FLAGS_DDL))


def set_watch_any_alltime_low(session: Session, watch_id: int, value: bool) -> None:
    """Upserts watch_id's any_alltime_low flag (Zadanie 4's third watch condition)."""
    ensure_watch_flags_table(session)
    session.execute(
        text(
            "INSERT INTO watch_flags (watch_id, any_alltime_low) VALUES (:id, :v) "
            "ON CONFLICT(watch_id) DO UPDATE SET any_alltime_low = excluded.any_alltime_low"
        ),
        {"id": watch_id, "v": int(value)},
    )


def get_watch_any_alltime_low(session: Session, watch_id: int) -> bool:
    """Returns watch_id's any_alltime_low flag; False if never set (the column's own DEFAULT 0)."""
    ensure_watch_flags_table(session)
    row = session.execute(
        text("SELECT any_alltime_low FROM watch_flags WHERE watch_id = :id"), {"id": watch_id}
    ).scalar_one_or_none()
    return bool(row) if row is not None else False


# --------------------------------------------------------------------------------------------------
# notification_log (antyspam) -- created defensively, same pattern as normalize/store.py's rejected_offer
# --------------------------------------------------------------------------------------------------

_NOTIFICATION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS notification_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    watch_id      INTEGER NOT NULL,
    product_id    INTEGER NOT NULL,
    price_bucket  INTEGER NOT NULL,
    price_total   INTEGER NOT NULL,
    reason        TEXT NOT NULL,
    channel       TEXT NOT NULL,
    sent_at       TEXT NOT NULL
)
"""
_NOTIFICATION_LOG_INDEX_DDL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_notification_log_dedup "
    "ON notification_log (watch_id, product_id, price_bucket)"
)
RENOTIFY_DROP_PCT = 3
"""ARCHITEKTURA.md Zadanie 4: re-notify only once price has fallen >= 3% below the last sent price."""


def ensure_notification_log_table(session: Session) -> None:
    """Creates notification_log and its dedup unique index if they do not already exist; idempotent."""
    session.execute(text(_NOTIFICATION_LOG_DDL))
    session.execute(text(_NOTIFICATION_LOG_INDEX_DDL))


def _price_bucket(price_total: int) -> int:
    """Returns the dedup bucket for price_total: the price itself, in grosze.

    Design choice (ARCHITEKTURA.md doesn't specify the bucketing function, only the column name and the
    3%-drop re-notify rule): the *exact* price is the finest possible bucket, so the DB-level unique index
    on (watch_id, product_id, price_bucket) gives a hard guarantee that literally the same price is never
    notified twice, even under a concurrent/duplicate call. The coarser "must be >=3% lower to re-notify"
    rule is enforced explicitly in `_should_notify` by comparing against the *last sent* price -- doing that
    check in application code (rather than encoding a 3%-log-scale tier into price_bucket itself) is far more
    transparent to test and to reason about, at the cost of the unique index alone not being a *sufficient*
    antispam guarantee on its own (it only blocks exact-price repeats; `_should_notify` blocks the rest).
    """
    return int(price_total)


def _last_notification(session: Session, watch_id: int, product_id: int) -> dict[str, Any] | None:
    """Returns the most recently sent notification_log row for (watch_id, product_id), or None if never sent."""
    row = session.execute(
        text(
            "SELECT price_total, sent_at FROM notification_log WHERE watch_id = :w AND product_id = :p "
            "ORDER BY sent_at DESC LIMIT 1"
        ),
        {"w": watch_id, "p": product_id},
    ).mappings().first()
    return dict(row) if row is not None else None


def should_notify(session: Session, watch_id: int, product_id: int, price_total: int) -> bool:
    """Returns True iff a hit on (watch_id, product_id) at price_total should actually be sent (antispam).

    True on the first-ever hit for this (watch_id, product_id). On a repeat hit, True only when price_total
    is at least RENOTIFY_DROP_PCT% below the price of the last notification actually sent -- a hit at the
    same or a higher price than what was already reported is exactly the "watchlist gets ignored after three
    days" failure mode the task spec calls out, so it is suppressed.
    """
    ensure_notification_log_table(session)
    last = _last_notification(session, watch_id, product_id)
    if last is None:
        return True
    last_price = int(last["price_total"])
    return price_total * 100 <= last_price * (100 - RENOTIFY_DROP_PCT)


def record_notification(
    session: Session, *, watch_id: int, product_id: int, price_total: int, reason: str, channel: NotifyChannel
) -> bool:
    """Records that a notification for (watch_id, product_id) at price_total was sent; returns True if the
    row was actually written, or False if a duplicate (watch_id, product_id, price_bucket) already exists
    (the unique index's own dedup guarantee winning a race against `should_notify`'s own check).
    """
    ensure_notification_log_table(session)
    bucket = _price_bucket(price_total)
    existing = session.execute(
        text(
            "SELECT 1 FROM notification_log WHERE watch_id = :w AND product_id = :p AND price_bucket = :b"
        ),
        {"w": watch_id, "p": product_id, "b": bucket},
    ).first()
    if existing is not None:
        return False
    session.execute(
        text(
            "INSERT INTO notification_log (watch_id, product_id, price_bucket, price_total, reason, channel, sent_at) "
            "VALUES (:w, :p, :b, :price, :reason, :channel, :sent_at)"
        ),
        {
            "w": watch_id,
            "p": product_id,
            "b": bucket,
            "price": price_total,
            "reason": reason,
            "channel": channel,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return True


# --------------------------------------------------------------------------------------------------
# watch CRUD
# --------------------------------------------------------------------------------------------------


def add_watch(session: Session, watch: Watch, *, any_alltime_low: bool = False) -> int:
    """Inserts a new watch row (Watch's own validator already enforces product_id-or-query) plus its
    watch_flags row for the third condition (any_alltime_low, see module docstring); returns the new id."""
    result = session.execute(
        text(
            """
            INSERT INTO watch (product_id, query, max_price, min_deal_score, channel, active)
            VALUES (:product_id, :query, :max_price, :min_deal_score, :channel, :active)
            RETURNING id
            """
        ),
        {
            "product_id": watch.product_id,
            "query": watch.query,
            "max_price": watch.max_price,
            "min_deal_score": watch.min_deal_score,
            "channel": watch.channel,
            "active": int(watch.active),
        },
    )
    new_id = int(result.scalar_one())
    if any_alltime_low:
        set_watch_any_alltime_low(session, new_id, True)
    return new_id


def list_watches(session: Session, *, active_only: bool = False) -> list[Watch]:
    """Returns every watch row, most recently added first; `active_only=True` filters to active=1."""
    query = "SELECT * FROM watch"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY id DESC"
    rows = session.execute(text(query)).mappings().all()
    return [_row_to_watch(row) for row in rows]


def get_watch(session: Session, watch_id: int) -> Watch | None:
    """Returns the watch with id=watch_id, or None if it does not exist."""
    row = session.execute(text("SELECT * FROM watch WHERE id = :id"), {"id": watch_id}).mappings().first()
    return _row_to_watch(row) if row is not None else None


def remove_watch(session: Session, watch_id: int) -> bool:
    """Deletes the watch with id=watch_id (and its watch_flags row, if any); returns True iff a watch row
    was actually deleted."""
    ensure_watch_flags_table(session)
    session.execute(text("DELETE FROM watch_flags WHERE watch_id = :id"), {"id": watch_id})
    result = session.execute(text("DELETE FROM watch WHERE id = :id"), {"id": watch_id})
    return bool(result.rowcount)  # type: ignore[attr-defined]


def set_watch_active(session: Session, watch_id: int, active: bool) -> bool:
    """Sets active on the watch with id=watch_id; returns True iff a row was actually updated."""
    result = session.execute(
        text("UPDATE watch SET active = :active WHERE id = :id"), {"active": int(active), "id": watch_id}
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


_UNSET = object()
"""Sentinel distinguishing "leave this field unchanged" from an explicit None (a meaningful value for
max_price/min_deal_score: it means "no threshold"), for update_watch's optional keyword arguments."""


def update_watch(
    session: Session,
    watch_id: int,
    *,
    max_price: int | None | object = _UNSET,
    min_deal_score: float | None | object = _UNSET,
    channel: NotifyChannel | None = None,
    any_alltime_low: bool | None = None,
) -> bool:
    """Updates the given fields of the watch with id=watch_id (a field left at its _UNSET default is not
    touched; `any_alltime_low` writes to the watch_flags companion table, see this module's docstring);
    returns True iff a watch row was updated (a lone any_alltime_low change still returns True)."""
    sets: list[str] = []
    params: dict[str, object] = {"id": watch_id}
    if max_price is not _UNSET:
        sets.append("max_price = :max_price")
        params["max_price"] = max_price
    if min_deal_score is not _UNSET:
        sets.append("min_deal_score = :min_deal_score")
        params["min_deal_score"] = min_deal_score
    if channel is not None:
        sets.append("channel = :channel")
        params["channel"] = channel

    updated = False
    if sets:
        result = session.execute(text(f"UPDATE watch SET {', '.join(sets)} WHERE id = :id"), params)
        updated = bool(result.rowcount)  # type: ignore[attr-defined]
    if any_alltime_low is not None:
        set_watch_any_alltime_low(session, watch_id, any_alltime_low)
        updated = True
    return updated


def _row_to_watch(row: Any) -> Watch:
    """Converts a `watch` table row mapping into a Watch model."""
    data = dict(row)
    return Watch(
        id=data["id"],
        product_id=data["product_id"],
        query=data["query"],
        max_price=data["max_price"],
        min_deal_score=data["min_deal_score"],
        channel=data["channel"],
        active=bool(data["active"]),
    )


def resolve_query_watch_product_id(session: Session, watch: Watch) -> int | None:
    """Resolves a query-based watch (no product_id yet) to a product_id by matching `product.canonical_title`
    (case-insensitive substring), returning the most recently created match, or None if nothing matches yet.

    This is intentionally the simplest possible resolution -- a real free-text search (fuzzy, multi-field)
    belongs to the matcher's own vocabulary (A3), not to the watchlist. A query watch that never resolves
    just never fires, which is correct (Z4): it is not silently promoted to a fabricated match.
    """
    if watch.product_id is not None or not watch.query:
        return watch.product_id
    row = session.execute(
        text("SELECT id FROM product WHERE canonical_title LIKE :pattern ORDER BY created_at DESC LIMIT 1"),
        {"pattern": f"%{watch.query}%"},
    ).scalar_one_or_none()
    return int(row) if row is not None else None


# --------------------------------------------------------------------------------------------------
# Hit evaluation -- reads only, never recomputes deal_score/quality itself (task spec: "nie liczysz tego
# u siebie po swojemu")
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchHit:
    """One watch condition met by one product's current best offer -- consumed by the digest and by notify."""

    watch_id: int
    product_id: int
    channel: NotifyChannel
    reason: Literal["max_price", "min_deal_score", "any_alltime_low"]
    price_total: int
    deal_score: float | None
    is_alltime_low: bool
    offer_id: int


def _latest_deal_rows_for_product(session: Session, product_id: int) -> list[dict[str, Any]]:
    """Returns the most recent deal row per offer of product_id, as plain dicts, cheapest offer first."""
    rows = session.execute(
        text(
            """
            SELECT d.offer_id, d.deal_score, d.price_rank_today, d.is_alltime_low, d.computed_at, o.price_total
            FROM deal d
            JOIN offer o ON o.id = d.offer_id
            WHERE o.product_id = :product_id
              AND d.id = (SELECT MAX(d2.id) FROM deal d2 WHERE d2.offer_id = d.offer_id)
            ORDER BY o.price_total ASC
            """
        ),
        {"product_id": product_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def evaluate_watches(session: Session) -> list[WatchHit]:
    """Evaluates every active watch against the latest `deal` rows of the product(s) it targets; returns
    every condition actually met, independent of any deal_score threshold used elsewhere in the digest
    (task spec Zadanie 3: watchlist hits are shown "niezaleznie od progu deal_score").

    A watch with no condition set (max_price, min_deal_score, any_alltime_low all None) never fires -- an
    empty watch is not an implicit "notify on anything" wildcard. A watch fires at most once per evaluation,
    on its cheapest currently-eligible offer.
    """
    hits: list[WatchHit] = []
    for watch in list_watches(session, active_only=True):
        product_id = resolve_query_watch_product_id(session, watch)
        if product_id is None:
            continue
        deal_rows = _latest_deal_rows_for_product(session, product_id)
        if not deal_rows:
            continue
        assert watch.id is not None  # every row from list_watches came from the DB and has an id
        best = deal_rows[0]  # cheapest offer, per the ORDER BY above
        price_total = int(best["price_total"])
        deal_score = float(best["deal_score"]) if best["deal_score"] is not None else None
        is_alltime_low = bool(best["is_alltime_low"])
        offer_id = int(best["offer_id"])

        reason: Literal["max_price", "min_deal_score", "any_alltime_low"] | None = None
        if watch.max_price is not None and price_total <= watch.max_price:
            reason = "max_price"
        elif watch.min_deal_score is not None and deal_score is not None and deal_score >= watch.min_deal_score:
            reason = "min_deal_score"
        elif is_alltime_low and get_watch_any_alltime_low(session, watch.id):
            reason = "any_alltime_low"

        if reason is not None:
            hits.append(
                WatchHit(
                    watch_id=watch.id,
                    product_id=product_id,
                    channel=watch.channel,
                    reason=reason,
                    price_total=price_total,
                    deal_score=deal_score,
                    is_alltime_low=is_alltime_low,
                    offer_id=offer_id,
                )
            )
    return hits


# --------------------------------------------------------------------------------------------------
# Notifier plugins (Zadanie 4: "kanaly jako wtyczki za jednym interfejsem")
# --------------------------------------------------------------------------------------------------


class Notifier(Protocol):
    """One notification channel. `enabled` reflects whether required configuration is present; `send` never
    raises -- a delivery failure is caught and logged, not propagated into the pipeline run."""

    channel: NotifyChannel
    enabled: bool

    def send(self, subject: str, body: str) -> bool:
        """Attempts delivery; returns True iff the message was actually sent."""
        ...


class ConsoleNotifier:
    """Default channel (Watch.channel default, task spec: "Domyslny kanal to console"). Always enabled."""

    channel: NotifyChannel = "console"
    enabled = True

    def send(self, subject: str, body: str) -> bool:
        """Prints subject and body to stdout via the standard logger; always succeeds."""
        logger.info("[watch] %s\n%s", subject, body)
        print(f"[watch] {subject}\n{body}")  # noqa: T201 -- this *is* the console channel's product
        return True


class FileNotifier:
    """Appends each notification as one line of JSON to a file. Disables itself if the path can't be written."""

    channel: NotifyChannel = "file"

    def __init__(self, path: Path) -> None:
        """Binds this notifier to `path`; `enabled` is True as long as the parent directory is creatable."""
        self.path = path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.enabled = True
        except OSError as exc:
            logger.warning("file notifier disabled: cannot create %s: %s", path.parent, exc)
            self.enabled = False

    def send(self, subject: str, body: str) -> bool:
        """Appends {sent_at, subject, body} as one JSON line to self.path; returns False on any I/O error."""
        if not self.enabled:
            return False
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {"sent_at": datetime.now(timezone.utc).isoformat(), "subject": subject, "body": body},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            return True
        except OSError as exc:
            logger.warning("file notifier failed to write %s: %s", self.path, exc)
            return False


class SmtpNotifier:
    """Sends one plain-text email per notification via stdlib smtplib. Disables itself if SMTP_* or a
    recipient address is not configured -- never raises, never blocks a run on missing secrets."""

    channel: NotifyChannel = "smtp"

    def __init__(self, *, to_addr: str | None = None) -> None:
        """Reads SMTP_HOST/PORT/USER/PASSWORD from the environment (config.get_secret) and a recipient
        (explicit `to_addr`, or the DEALRADAR_NOTIFY_EMAIL_TO env var); enabled iff all are present."""
        self.host = get_secret("SMTP_HOST")
        self.port = get_secret("SMTP_PORT")
        self.user = get_secret("SMTP_USER")
        self.password = get_secret("SMTP_PASSWORD")
        self.to_addr = to_addr or get_secret("DEALRADAR_NOTIFY_EMAIL_TO")
        self.enabled = all([self.host, self.port, self.user, self.password, self.to_addr])
        if not self.enabled:
            logger.info("smtp notifier disabled: SMTP_HOST/PORT/USER/PASSWORD or a recipient address is not set")

    def send(self, subject: str, body: str) -> bool:
        """Sends one email via SMTP+STARTTLS; returns False (logged, never raised) on any delivery error."""
        if not self.enabled:
            return False
        assert self.host and self.port and self.user and self.password and self.to_addr
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.user
        message["To"] = self.to_addr
        message.set_content(body)
        try:
            with smtplib.SMTP(self.host, int(self.port), timeout=15) as smtp:
                smtp.starttls()
                smtp.login(self.user, self.password)
                smtp.send_message(message)
            return True
        except (OSError, smtplib.SMTPException) as exc:
            logger.warning("smtp notifier failed to send: %s", exc)
            return False


class TelegramNotifier:
    """Sends one message via the Telegram Bot API (plain httpx POST, already a project dependency -- no LLM
    call). Disables itself if TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not configured."""

    channel: NotifyChannel = "telegram"

    def __init__(self) -> None:
        """Reads TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID from the environment; enabled iff both are present."""
        self.token = get_secret("TELEGRAM_BOT_TOKEN")
        self.chat_id = get_secret("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            logger.info("telegram notifier disabled: TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set")

    def send(self, subject: str, body: str) -> bool:
        """POSTs {subject}\\n{body} to the bot API's sendMessage endpoint; returns False on any HTTP error."""
        if not self.enabled:
            return False
        assert self.token and self.chat_id
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        try:
            response = httpx.post(url, json={"chat_id": self.chat_id, "text": f"{subject}\n{body}"}, timeout=15)
            response.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            logger.warning("telegram notifier failed to send: %s", exc)
            return False


def build_notifier(channel: NotifyChannel, *, notify_file_path: Path | None = None) -> Notifier:
    """Constructs the Notifier for `channel`; unconfigured channels come back with enabled=False, not an error."""
    if channel == "console":
        return ConsoleNotifier()
    if channel == "file":
        return FileNotifier(notify_file_path or Path("notifications.log"))
    if channel == "smtp":
        return SmtpNotifier()
    if channel == "telegram":
        return TelegramNotifier()
    raise ValueError(f"unknown notify channel {channel!r}")  # unreachable given NotifyChannel's Literal


# --------------------------------------------------------------------------------------------------
# End-to-end: evaluate watches, apply antispam, deliver, record
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class NotifyReport:
    """Counts describing one notify_watch_hits run, for folding into a RunReport note."""

    hits_found: int
    sent: int
    suppressed_antispam: int
    channel_disabled: int


def notify_watch_hits(
    session: Session, hits: list[WatchHit], *, notify_file_path: Path | None = None
) -> NotifyReport:
    """Applies antispam (should_notify) to each hit, delivers the survivors via their watch's channel, and
    records every actually-sent notification to notification_log; returns counts of what happened.

    Ensures notification_log exists first (defensive CREATE TABLE IF NOT EXISTS, see module docstring).
    Never raises: a disabled or failing channel is counted, not propagated.
    """
    ensure_notification_log_table(session)
    notifiers: dict[NotifyChannel, Notifier] = {}
    sent = 0
    suppressed = 0
    disabled = 0

    for hit in hits:
        if not should_notify(session, hit.watch_id, hit.product_id, hit.price_total):
            suppressed += 1
            continue
        notifier = notifiers.setdefault(hit.channel, build_notifier(hit.channel, notify_file_path=notify_file_path))
        if not notifier.enabled:
            disabled += 1
            continue
        subject = f"DealRadar: watch #{hit.watch_id} trafienie ({hit.reason})"
        body = (
            f"product_id={hit.product_id} offer_id={hit.offer_id} price_total={hit.price_total / 100:.2f} zl "
            f"deal_score={hit.deal_score if hit.deal_score is not None else 'n/a'} "
            f"is_alltime_low={hit.is_alltime_low} reason={hit.reason}"
        )
        delivered = notifier.send(subject, body)
        if delivered:
            record_notification(
                session,
                watch_id=hit.watch_id,
                product_id=hit.product_id,
                price_total=hit.price_total,
                reason=hit.reason,
                channel=hit.channel,
            )
            sent += 1
        else:
            disabled += 1

    return NotifyReport(hits_found=len(hits), sent=sent, suppressed_antispam=suppressed, channel_disabled=disabled)
