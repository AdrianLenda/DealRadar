"""Etap 5 (score): `deal_score` and `shop_credibility` -- pure statistics, no LLM anywhere. Owner: A4.

Implements ARCHITEKTURA.md section 8 literally: robust (median/MAD, never mean/stdev) baseline over the
last 90 days of matched, in-stock, `new`-condition, price-complete history; a weighted blend of four
[0,1]-clamped components; a coverage gate (G1) applied as a *ceiling*, never a multiplier; and a cross-shop
cold-start basis for products without enough history yet. All tunable constants live in config/scoring.yaml
(loaded via scoring.history.load_scoring_config), never hard-coded here.

Z3 is enforced structurally: `offer.list_price_claimed` is read in exactly one place in this module (the
shop_credibility block) and is never used to compute `depth`, `z`, or any deal_score component.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from dealradar.scoring.history import ScoringConfig, load_scoring_config

# --------------------------------------------------------------------------------------------------
# Row types for the two bulk queries this module issues (one per score_deals call, not one per product)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _TodayOffer:
    """One offer with a valid, filter-eligible price_point on the day being scored."""

    offer_id: int
    source_id: int
    price_total: int
    list_price_claimed: int | None


@dataclass(frozen=True)
class DealReport:
    """Counts describing one score_deals run, for the caller to fold into a RunReport (Z7)."""

    computed_at: datetime
    scoring_date: date
    deals_scored: int
    skipped_no_deal: int
    """Offers/products skipped per Z4 (e.g. a lone offer with no history) -- never scored as 0."""
    basis_counts: dict[str, int] = field(default_factory=lambda: {"history": 0, "cross_shop": 0})
    shop_credibility_rows_written: int = 0
    shop_credibility_rows_null: int = 0
    """Sources whose sample was below the minimum size -- written with bullshit_index=None, not omitted."""


def _clamp(value: float, low: float, high: float) -> float:
    """Returns value restricted to the closed interval [low, high]."""
    return max(low, min(high, value))


# --------------------------------------------------------------------------------------------------
# Bulk data fetch: one query for the 90-day history baseline, one for today's eligible offers
# --------------------------------------------------------------------------------------------------


def _filter_sql(cfg: ScoringConfig) -> tuple[str, dict[str, Any]]:
    """Builds the shared offer-level WHERE clause (condition/match_confidence/price_incomplete/in_stock)."""
    clauses = ["o.product_id IS NOT NULL"]
    params: dict[str, Any] = {}
    filters = cfg.history.filters
    if filters.condition:
        clauses.append("o.condition = :flt_condition")
        params["flt_condition"] = filters.condition
    clauses.append("o.match_confidence >= :flt_min_conf")
    params["flt_min_conf"] = filters.min_match_confidence
    if filters.exclude_price_incomplete:
        clauses.append("o.price_incomplete = 0")
    if filters.require_in_stock:
        clauses.append("pp.in_stock = 1")
    return " AND ".join(clauses), params


def _fetch_history_rows(
    session: Session, product_ids: list[int] | None, window_start: date, scoring_date: date, cfg: ScoringConfig
) -> dict[int, list[tuple[date, int]]]:
    """Returns {product_id: [(observed_date, price_total), ...]} for H, the 90-day robust-stats baseline."""
    filter_sql, params = _filter_sql(cfg)
    params["window_start"] = window_start.isoformat()
    params["scoring_date"] = scoring_date.isoformat()
    query = (
        "SELECT o.product_id AS product_id, date(pp.observed_at) AS obs_date, pp.price_total AS price_total "
        "FROM price_point pp JOIN offer o ON o.id = pp.offer_id "
        f"WHERE {filter_sql} AND date(pp.observed_at) BETWEEN :window_start AND :scoring_date"
    )
    if product_ids is not None:
        query += " AND o.product_id IN :product_ids"
        stmt = text(query).bindparams(bindparam("product_ids", expanding=True))
        params["product_ids"] = product_ids
    else:
        stmt = text(query)

    result: dict[int, list[tuple[date, int]]] = defaultdict(list)
    for row in session.execute(stmt, params).mappings().all():
        result[int(row["product_id"])].append((date.fromisoformat(row["obs_date"]), int(row["price_total"])))
    return result


def _fetch_today_rows(
    session: Session, product_ids: list[int] | None, scoring_date: date, cfg: ScoringConfig
) -> dict[int, list[_TodayOffer]]:
    """Returns {product_id: [_TodayOffer, ...]} for offers with an eligible price_point on scoring_date."""
    filter_sql, params = _filter_sql(cfg)
    params["scoring_date"] = scoring_date.isoformat()
    query = (
        "SELECT o.product_id AS product_id, o.id AS offer_id, o.source_id AS source_id, "
        "pp.price_total AS price_total, o.list_price_claimed AS list_price_claimed "
        "FROM price_point pp JOIN offer o ON o.id = pp.offer_id "
        f"WHERE {filter_sql} AND date(pp.observed_at) = :scoring_date"
    )
    if product_ids is not None:
        query += " AND o.product_id IN :product_ids"
        stmt = text(query).bindparams(bindparam("product_ids", expanding=True))
        params["product_ids"] = product_ids
    else:
        stmt = text(query)

    result: dict[int, list[_TodayOffer]] = defaultdict(list)
    for row in session.execute(stmt, params).mappings().all():
        result[int(row["product_id"])].append(
            _TodayOffer(
                offer_id=int(row["offer_id"]),
                source_id=int(row["source_id"]),
                price_total=int(row["price_total"]),
                list_price_claimed=None if row["list_price_claimed"] is None else int(row["list_price_claimed"]),
            )
        )
    return result


# --------------------------------------------------------------------------------------------------
# score_deals (Zadanie 2 + Zadanie 3)
# --------------------------------------------------------------------------------------------------


def score_deals(
    session: Session,
    product_ids: list[int] | None = None,
    *,
    at: date | None = None,
    config: ScoringConfig | None = None,
) -> DealReport:
    """Computes deal_score for every eligible offer (optionally restricted to product_ids) and writes
    `deal` + `shop_credibility` rows; returns counts of what happened.

    "Eligible" means: condition=new, match_confidence>=min_match_confidence, not price_incomplete, and
    in_stock, on the day being scored (`at`, default today) -- per F3, an incomplete price never enters
    ranking. A product with fewer than `min_points_for_history_basis` history points falls back to the
    cross-shop cold-start basis; if that product also has fewer than 2 eligible offers today, no deal row
    is written for it at all (Z4: absence, not a fabricated 0).
    """
    cfg = config or load_scoring_config()
    scoring_date = at or datetime.now(timezone.utc).date()
    computed_at = datetime.now(timezone.utc)
    window_start = scoring_date - timedelta(days=cfg.history.window_days - 1)

    history_rows = _fetch_history_rows(session, product_ids, window_start, scoring_date, cfg)
    today_rows = _fetch_today_rows(session, product_ids, scoring_date, cfg)

    deal_params: list[dict[str, Any]] = []
    basis_counts = {"history": 0, "cross_shop": 0}
    skipped_no_deal = 0
    credibility_samples: dict[int, list[tuple[float, float]]] = defaultdict(list)

    for product_id, today_offers in today_rows.items():
        h_points = history_rows.get(product_id, [])
        n_days_history = len({d for d, _ in h_points})
        h_prices = [p for _, p in h_points]
        hist_min = min(h_prices) if h_prices else None
        history_date_range = (
            [min(d for d, _ in h_points).isoformat(), max(d for d, _ in h_points).isoformat()] if h_points else [None, None]
        )

        if len(h_points) >= cfg.history.min_points_for_history_basis:
            basis = "history"
            med = float(statistics.median(h_prices))
            mad = float(statistics.median(abs(p - med) for p in h_prices))
            sigma = max(
                cfg.sigma.mad_constant * mad,
                cfg.sigma.floor_fraction_of_median * med,
                cfg.sigma.absolute_floor_grosze,
            )
        else:
            basis = "cross_shop"
            if len(today_offers) < 2:
                # Z4: a lone offer with no real history gets no deal record, not a fabricated deal_score=0.
                skipped_no_deal += 1
                continue
            med = float(statistics.median(o.price_total for o in today_offers))
            mad = None
            sigma = None

        sorted_prices = sorted(o.price_total for o in today_offers)
        n_competing = len(today_offers)
        n_sources = len({o.source_id for o in today_offers})
        competitor_offer_ids = [o.offer_id for o in today_offers]

        for offer in today_offers:
            rank = 1 + sum(1 for p in sorted_prices if p < offer.price_total)
            depth = 1.0 - offer.price_total / med if med > 0 else 0.0

            z: float | None
            if basis == "history":
                assert sigma is not None
                z_value = (med - offer.price_total) / sigma
                s_z = _clamp(z_value / cfg.clamps.z_divisor, 0.0, 1.0)
                z = z_value
            else:
                z = None
                s_z = 0.0

            s_depth = _clamp(depth / cfg.clamps.depth_divisor, 0.0, 1.0)
            if rank == 1:
                s_rank = cfg.rank_scores.cheapest_today
            elif rank <= 3:
                s_rank = cfg.rank_scores.top3_today
            else:
                s_rank = cfg.rank_scores.other
            is_atl = hist_min is not None and offer.price_total <= hist_min
            s_atl = 1.0 if is_atl else 0.0

            if basis == "history":
                raw = (
                    cfg.weights.z * s_z
                    + cfg.weights.depth * s_depth
                    + cfg.weights.rank * s_rank
                    + cfg.weights.atl * s_atl
                )
            else:
                remaining = cfg.weights.depth + cfg.weights.rank + cfg.weights.atl
                raw = (
                    (cfg.weights.depth / remaining) * s_depth
                    + (cfg.weights.rank / remaining) * s_rank
                    + (cfg.weights.atl / remaining) * s_atl
                )

            caps: list[tuple[float, str]] = []
            if basis == "history":
                if n_days_history < 14:
                    caps.append((cfg.coverage_gate.history_lt_14_days_cap, f"history {n_days_history}d < 14d"))
                if n_days_history < 30:
                    caps.append((cfg.coverage_gate.history_lt_30_days_cap, f"history {n_days_history}d < 30d"))
            else:
                caps.append((cfg.cold_start.cap, "cold start: fewer than min_points_for_history_basis history points"))
            if n_competing < cfg.coverage_gate.min_competing_offers:
                caps.append(
                    (cfg.coverage_gate.min_competing_offers_cap, f"only {n_competing} competing offers < {cfg.coverage_gate.min_competing_offers}")
                )
            if n_sources < cfg.coverage_gate.min_sources:
                caps.append((cfg.coverage_gate.min_sources_cap, f"only {n_sources} sources < {cfg.coverage_gate.min_sources}"))

            if caps:
                cap_value = min(c for c, _ in caps)
                cap_reasons = [reason for c, reason in caps if c == cap_value]
            else:
                cap_value = 1.0
                cap_reasons = []

            deal_score = 100.0 * min(raw, cap_value)

            evidence = {
                "basis": basis,
                "med": med,
                "mad": mad,
                "sigma": sigma,
                "n_history_points": len(h_points),
                "n_days_history": n_days_history,
                "history_date_range": history_date_range,
                "competitor_offer_ids": competitor_offer_ids,
                "n_competing_offers": n_competing,
                "n_sources": n_sources,
                "rank_today": rank,
                "s_z": s_z,
                "s_depth": s_depth,
                "s_rank": s_rank,
                "s_atl": s_atl,
                "raw_score": raw,
                "applied_cap": cap_value,
                "applied_cap_reasons": cap_reasons,
            }

            deal_params.append(
                {
                    "offer_id": offer.offer_id,
                    "computed_at": computed_at.isoformat(),
                    "basis": basis,
                    "deal_score": deal_score,
                    "depth_vs_median_90d": depth,
                    "z_robust": z,
                    "price_rank_today": rank,
                    "is_alltime_low": int(is_atl),
                    "n_competing_offers": n_competing,
                    "n_days_history": n_days_history,
                    "coverage_cap": cap_value,
                    "evidence_json": json.dumps(evidence, ensure_ascii=False, default=str),
                }
            )
            basis_counts[basis] += 1

            # Zadanie 3: wskaźnik ściemy. Z3 -- list_price_claimed is read here and ONLY here, and never
            # feeds deal_score/depth/z above. Requires real (basis="history") history of at least
            # shop_credibility.min_history_days, per the task spec ("inaczej porównujesz deklarację z niczym").
            if (
                offer.list_price_claimed is not None
                and offer.list_price_claimed > 0
                and basis == "history"
                and n_days_history >= cfg.shop_credibility.min_history_days
            ):
                claimed_discount = 1.0 - offer.price_total / offer.list_price_claimed
                credibility_samples[offer.source_id].append((claimed_discount, depth))

    if deal_params:
        session.execute(
            text(
                """
                INSERT INTO deal (
                    offer_id, computed_at, basis, deal_score, depth_vs_median_90d, z_robust,
                    price_rank_today, is_alltime_low, n_competing_offers, n_days_history,
                    coverage_cap, evidence_json
                ) VALUES (
                    :offer_id, :computed_at, :basis, :deal_score, :depth_vs_median_90d, :z_robust,
                    :price_rank_today, :is_alltime_low, :n_competing_offers, :n_days_history,
                    :coverage_cap, :evidence_json
                )
                """
            ),
            deal_params,
        )

    shop_credibility_written, shop_credibility_null = _write_shop_credibility(
        session, credibility_samples, period=scoring_date.strftime("%Y-%m"), cfg=cfg
    )

    return DealReport(
        computed_at=computed_at,
        scoring_date=scoring_date,
        deals_scored=len(deal_params),
        skipped_no_deal=skipped_no_deal,
        basis_counts=basis_counts,
        shop_credibility_rows_written=shop_credibility_written,
        shop_credibility_rows_null=shop_credibility_null,
    )


def _write_shop_credibility(
    session: Session, samples: dict[int, list[tuple[float, float]]], *, period: str, cfg: ScoringConfig
) -> tuple[int, int]:
    """Upserts one shop_credibility row per source_id in `samples`; returns (rows_written, rows_null)."""
    written = 0
    null_count = 0
    for source_id, pairs in samples.items():
        n_offers = len(pairs)
        claimed_avg = statistics.mean(c for c, _ in pairs)
        real_avg = statistics.mean(r for _, r in pairs)
        if n_offers < cfg.shop_credibility.min_sample_size:
            bullshit_index: float | None = None
            null_count += 1
        else:
            bullshit_index = statistics.mean(c - r for c, r in pairs)
        session.execute(
            text(
                """
                INSERT INTO shop_credibility (source_id, period, claimed_discount_avg, real_discount_avg, bullshit_index, n_offers)
                VALUES (:source_id, :period, :claimed_avg, :real_avg, :bullshit_index, :n_offers)
                ON CONFLICT(source_id, period) DO UPDATE SET
                    claimed_discount_avg = excluded.claimed_discount_avg,
                    real_discount_avg = excluded.real_discount_avg,
                    bullshit_index = excluded.bullshit_index,
                    n_offers = excluded.n_offers
                """
            ),
            {
                "source_id": source_id,
                "period": period,
                "claimed_avg": claimed_avg,
                "real_avg": real_avg,
                "bullshit_index": bullshit_index,
                "n_offers": n_offers,
            },
        )
        written += 1
    return written, null_count
