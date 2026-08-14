"""Etap 4 (snapshot): daily price history capture, and offline compaction of old history. Owner: A4.

This is deliberately the first thing implemented in the `scoring` package (see prompty/A4-historia-i-deal-score.md):
price history is the one resource in DealRadar that cannot be bought or reconstructed after the fact, so
`snapshot_prices` must be correct and running before the rest of the pipeline (match, deal_score) is ready.

Design notes:
- `snapshot_prices` reads the *current* state of the `offer` table (owned by A1/A2/A3) and appends one
  `price_point` per active offer for the given calendar day, via `db.append_price_point`, which already
  guarantees at-most-one-row-per-offer-per-day (idempotent re-run, enforced by a DB unique index).
- "Active" is derived from `offer.last_seen` alone -- there is no separate tracking-state column (and this
  module must not add one; `models.py`/`migrations/` are A0-owned). An offer last seen today gets a real
  price point; one not seen today but within `disappeared_grace_days` gets an `in_stock=False` point at its
  last known price (disappearance is itself the signal, per the task spec); one stale beyond the grace
  period is silently skipped -- recomputing staleness from `last_seen` on every run reproduces "stopped
  tracking" without needing to persist that decision anywhere.
- Compaction is a separate, explicitly-invoked function (never called by `snapshot_prices`), matching the
  spec's "uruchamiane osobną komendą, nie automatycznie". It deletes rows, keeping only min/max/median
  *actual observed rows* per (offer, ISO week) among weeks older than the age threshold -- never fabricates
  a price a device never actually showed (Z1/Z2).
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.models import PricePoint

DEFAULT_SCORING_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "scoring.yaml"


# --------------------------------------------------------------------------------------------------
# config/scoring.yaml -> validated config (shared by history.py and deal.py)
# --------------------------------------------------------------------------------------------------


class HistoryFiltersConfig(BaseModel):
    """The offer-level filters applied when assembling H, the 90-day historical price baseline."""

    model_config = ConfigDict(extra="forbid")

    condition: str = "new"
    require_in_stock: bool = True
    min_match_confidence: float = 0.90
    exclude_price_incomplete: bool = True


class HistoryConfig(BaseModel):
    """Parameters for building H (ARCHITEKTURA.md section 8)."""

    model_config = ConfigDict(extra="forbid")

    window_days: int = 90
    min_points_for_history_basis: int = 3
    filters: HistoryFiltersConfig = Field(default_factory=HistoryFiltersConfig)


class SigmaConfig(BaseModel):
    """Parameters for sigma = max(mad_constant*mad, floor_fraction_of_median*med, absolute_floor_grosze)."""

    model_config = ConfigDict(extra="forbid")

    mad_constant: float = 1.4826
    floor_fraction_of_median: float = 0.01
    absolute_floor_grosze: float = 1.0


class WeightsConfig(BaseModel):
    """Component weights for `raw` (must sum to 1.0 for the history basis)."""

    model_config = ConfigDict(extra="forbid")

    z: float = 0.35
    depth: float = 0.30
    rank: float = 0.20
    atl: float = 0.15


class ClampsConfig(BaseModel):
    """Divisors used to clamp z and depth into [0, 1]."""

    model_config = ConfigDict(extra="forbid")

    z_divisor: float = 4.0
    depth_divisor: float = 0.35


class RankScoresConfig(BaseModel):
    """s_rank values for the three ranking buckets."""

    model_config = ConfigDict(extra="forbid")

    cheapest_today: float = 1.0
    top3_today: float = 0.5
    other: float = 0.0


class CoverageGateConfig(BaseModel):
    """G1: coverage ceilings (never a multiplier/penalty)."""

    model_config = ConfigDict(extra="forbid")

    history_lt_14_days_cap: float = 0.50
    history_lt_30_days_cap: float = 0.70
    min_competing_offers: int = 3
    min_competing_offers_cap: float = 0.60
    min_sources: int = 2
    min_sources_cap: float = 0.80


class ColdStartConfig(BaseModel):
    """Cross-shop cold-start ceiling."""

    model_config = ConfigDict(extra="forbid")

    cap: float = 0.70


class CompactionConfig(BaseModel):
    """Parameters for the offline compaction command."""

    model_config = ConfigDict(extra="forbid")

    age_threshold_days: int = 180
    keep_min: bool = True
    keep_max: bool = True
    keep_median: bool = True


class SnapshotConfig(BaseModel):
    """Parameters for snapshot_prices and compact_price_history."""

    model_config = ConfigDict(extra="forbid")

    disappeared_grace_days: int = 7
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)


class ShopCredibilityConfig(BaseModel):
    """Parameters for the bullshit_index computation (Zadanie 3)."""

    model_config = ConfigDict(extra="forbid")

    min_history_days: int = 30
    min_sample_size: int = 20


class ScoringConfig(BaseModel):
    """The fully parsed and validated contents of config/scoring.yaml."""

    model_config = ConfigDict(extra="forbid")

    history: HistoryConfig = Field(default_factory=HistoryConfig)
    sigma: SigmaConfig = Field(default_factory=SigmaConfig)
    weights: WeightsConfig = Field(default_factory=WeightsConfig)
    clamps: ClampsConfig = Field(default_factory=ClampsConfig)
    rank_scores: RankScoresConfig = Field(default_factory=RankScoresConfig)
    coverage_gate: CoverageGateConfig = Field(default_factory=CoverageGateConfig)
    cold_start: ColdStartConfig = Field(default_factory=ColdStartConfig)
    snapshot: SnapshotConfig = Field(default_factory=SnapshotConfig)
    shop_credibility: ShopCredibilityConfig = Field(default_factory=ShopCredibilityConfig)


def load_scoring_config(path: Path | None = None) -> ScoringConfig:
    """Reads and validates config/scoring.yaml (or `path`) into a ScoringConfig; raises ValueError if invalid."""
    target = path or DEFAULT_SCORING_CONFIG_PATH
    if not target.exists():
        raise ValueError(f"missing required config file: {target}")
    with target.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"expected a YAML mapping at the top level of {target}, got {type(data).__name__}")
    try:
        return ScoringConfig.model_validate(data)
    except Exception as exc:  # pydantic.ValidationError, normalized like config.py does for AppConfig
        raise ValueError(f"invalid configuration in {target}: {exc}") from exc


# --------------------------------------------------------------------------------------------------
# snapshot_prices (Zadanie 1)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotReport:
    """Counts describing one snapshot_prices run, for the caller to fold into a RunReport (Z7)."""

    snapshot_date: date
    offers_seen_today: int
    """Offers present in the offer table with last_seen == snapshot_date; got a real price point."""
    offers_marked_out_of_stock: int
    """Offers not seen today but within the disappearance grace window; got an in_stock=False point."""
    offers_stopped_tracking: int
    """Offers stale beyond the grace window this run; no price_point written, no longer queried."""

    @property
    def price_points_written(self) -> int:
        """Returns the total number of price_point rows written or refreshed by this run."""
        return self.offers_seen_today + self.offers_marked_out_of_stock


def snapshot_prices(session: Session, at: date | None = None, *, config: ScoringConfig | None = None) -> SnapshotReport:
    """Writes one price_point per active offer for `at` (default: today); returns counts of what happened.

    Idempotent: calling this twice for the same `at` does not create duplicate rows, because
    `db.append_price_point` upserts on (offer_id, date(observed_at)).
    """
    cfg = config or load_scoring_config()
    snapshot_date = at or datetime.now(timezone.utc).date()
    observed_at = _combine_utc(snapshot_date)
    grace_days = cfg.snapshot.disappeared_grace_days

    rows = session.execute(
        text(
            """
            SELECT id, price_gross, shipping_cost, in_stock, last_seen
            FROM offer
            """
        )
    ).mappings().all()

    seen_today = 0
    marked_out_of_stock = 0
    stopped_tracking = 0

    for row in rows:
        last_seen_date = _parse_iso_datetime(row["last_seen"]).date()
        days_stale = (snapshot_date - last_seen_date).days
        price_total = int(row["price_gross"]) + int(row["shipping_cost"] or 0)

        if days_stale <= 0:
            # Seen today (or, defensively, a future-dated last_seen): a real, current observation.
            in_stock = True if row["in_stock"] is None else bool(row["in_stock"])
            dealradar_db.append_price_point(
                session, offer_id=int(row["id"]), price_total=price_total, in_stock=in_stock, observed_at=observed_at
            )
            seen_today += 1
        elif days_stale <= grace_days:
            # Disappeared from the latest collect run, but still inside the grace window: the
            # disappearance itself is the signal (F5-adjacent) -- record it as out of stock at the
            # last known price, do not silently drop it and do not invent a new price.
            dealradar_db.append_price_point(
                session, offer_id=int(row["id"]), price_total=price_total, in_stock=False, observed_at=observed_at
            )
            marked_out_of_stock += 1
        else:
            # Stale beyond the grace window: stop querying/snapshotting it. Nothing is written; this
            # state is recomputed from last_seen every run, so nothing needs to be persisted for it.
            stopped_tracking += 1

    return SnapshotReport(
        snapshot_date=snapshot_date,
        offers_seen_today=seen_today,
        offers_marked_out_of_stock=marked_out_of_stock,
        offers_stopped_tracking=stopped_tracking,
    )


def _combine_utc(day: date) -> datetime:
    """Returns a UTC datetime at midnight on `day`, used as the observed_at for a snapshot run."""
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)


def _parse_iso_datetime(value: str) -> datetime:
    """Parses an ISO-8601 datetime string as stored by db.py (via datetime.isoformat()) back into a datetime."""
    return datetime.fromisoformat(value)


# --------------------------------------------------------------------------------------------------
# price_series -- generic, unfiltered read access to a product's price history
# --------------------------------------------------------------------------------------------------


def price_series(session: Session, product_id: int, days: int, *, at: date | None = None) -> list[PricePoint]:
    """Returns every price_point for product_id's offers observed in the last `days` days, oldest first.

    Unfiltered: unlike the H used internally by score_deals, this returns raw history across every offer
    of the product regardless of condition/match_confidence/price_incomplete -- callers needing the same
    quality filters as deal_score should use scoring.deal's internal history fetch instead. `at` (default:
    today) anchors the "last N days" window and exists mainly so tests can be deterministic.
    """
    if days < 0:
        raise ValueError(f"days must be >= 0, got {days}")
    reference = at or datetime.now(timezone.utc).date()
    cutoff = reference - timedelta(days=days)
    rows = (
        session.execute(
            text(
                """
                SELECT pp.id, pp.offer_id, pp.observed_at, pp.price_total, pp.in_stock
                FROM price_point pp
                JOIN offer o ON o.id = pp.offer_id
                WHERE o.product_id = :product_id
                  AND date(pp.observed_at) >= :cutoff
                ORDER BY pp.observed_at ASC
                """
            ),
            {"product_id": product_id, "cutoff": cutoff.isoformat()},
        )
        .mappings()
        .all()
    )
    return [
        PricePoint(
            id=row["id"],
            offer_id=row["offer_id"],
            observed_at=_parse_iso_datetime(row["observed_at"]),
            price_total=row["price_total"],
            in_stock=bool(row["in_stock"]),
        )
        for row in rows
    ]


# --------------------------------------------------------------------------------------------------
# compact_price_history (Zadanie 1 -- separate command, never run automatically)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CompactionReport:
    """Counts describing one compact_price_history run."""

    before: date
    weeks_examined: int
    rows_deleted: int
    offers_affected: int


def compact_price_history(
    session: Session, *, before: date | None = None, config: ScoringConfig | None = None
) -> CompactionReport:
    """Collapses price_point rows older than the compaction threshold to <=3 rows/week (min, max, median).

    Never invoked by snapshot_prices; must be called explicitly (its own CLI command, or by a maintenance
    job). Operates per (offer_id, ISO year+week): for weeks with more than 3 rows, keeps only the row with
    the minimum price, the row with the maximum price, and the row closest to the week's true median price
    (an actual observed row -- never a synthesized value, per Z1/Z2), and deletes the rest. Weeks with 3 or
    fewer rows are left untouched (nothing to compact).
    """
    cfg = config or load_scoring_config()
    cutoff_date = (before or datetime.now(timezone.utc).date()) - timedelta(days=cfg.snapshot.compaction.age_threshold_days)

    rows = (
        session.execute(
            text(
                """
                SELECT id, offer_id, observed_at, price_total
                FROM price_point
                WHERE date(observed_at) < :cutoff
                ORDER BY offer_id, observed_at ASC
                """
            ),
            {"cutoff": cutoff_date.isoformat()},
        )
        .mappings()
        .all()
    )

    weeks: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        observed = _parse_iso_datetime(row["observed_at"])
        iso_year, iso_week, _ = observed.isocalendar()
        weeks[(row["offer_id"], iso_year, iso_week)].append(dict(row))

    ids_to_delete: list[int] = []
    offers_affected: set[int] = set()

    for (offer_id, _iso_year, _iso_week), week_rows in weeks.items():
        if len(week_rows) <= 3:
            continue
        keep_ids: set[int] = set()
        if cfg.snapshot.compaction.keep_min:
            keep_ids.add(min(week_rows, key=lambda r: r["price_total"])["id"])
        if cfg.snapshot.compaction.keep_max:
            keep_ids.add(max(week_rows, key=lambda r: r["price_total"])["id"])
        if cfg.snapshot.compaction.keep_median:
            median_price = statistics.median(r["price_total"] for r in week_rows)
            closest = min(week_rows, key=lambda r: abs(r["price_total"] - median_price))
            keep_ids.add(closest["id"])
        for r in week_rows:
            if r["id"] not in keep_ids:
                ids_to_delete.append(r["id"])
                offers_affected.add(offer_id)

    if ids_to_delete:
        # Chunk the DELETE to stay well under SQLite's default bound-parameter limit.
        chunk_size = 500
        delete_stmt = text("DELETE FROM price_point WHERE id IN :ids").bindparams(bindparam("ids", expanding=True))
        for i in range(0, len(ids_to_delete), chunk_size):
            chunk = ids_to_delete[i : i + chunk_size]
            session.execute(delete_stmt, {"ids": chunk})

    return CompactionReport(
        before=before or datetime.now(timezone.utc).date(),
        weeks_examined=len(weeks),
        rows_deleted=len(ids_to_delete),
        offers_affected=len(offers_affected),
    )
