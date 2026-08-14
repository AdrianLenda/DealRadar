"""Tests for dealradar.pipeline -- orchestration, the run lock, and rebuild's Z1 guarantee. Owner: A6.

Two fixture styles, deliberately:
- The run-lock tests exercise `acquire_run_lock` directly (no DB needed) -- the file lock is what actually
  prevents two concurrent `dealradar run` OS processes from corrupting the same sqlite file.
- The orchestration/rebuild tests seed `raw_offer` with realistic payloads matching the real
  config/normalizers/*.yaml field maps (x-kom.yaml + the generic feed fallback), then drive the *real*
  normalize_batch -> match_offers -> score_deals -> score_quality chain through `pipeline.run_full` /
  `pipeline.rebuild` -- this is what makes the Z1 checksum test below a genuine raw_offer -> derived-state
  test, not a shortcut against directly-inserted `offer`/`product` rows.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar import pipeline
from dealradar.config import AppConfig, BrandAliasesConfig, SourceConfig, SourcesConfig
from dealradar.errors import RunLockError
from dealradar.models import Source

NOW = datetime.now(timezone.utc).replace(hour=6, minute=0, second=0, microsecond=0)
"""Today at 06:00 UTC -- deliberately relative to the real clock, NOT a hardcoded date.

`run_full` drives `snapshot_prices` and `score_deals`, and neither takes an `at`/`now` parameter the way
`snapshot_prices(at=...)` / `price_series(at=...)` do when called directly, so both read the real wall
clock. Seeded offers therefore have to be *seen today* to be scored: `snapshot_prices` writes a real
`in_stock` price point only when `last_seen` is today, and an offer 1-7 days stale instead gets an
`in_stock=False` point, which `score_deals` filters out -- yielding zero deals.

A literal date here (this was `datetime(2026, 8, 11, ...)`) therefore makes these tests pass on exactly one
calendar day and fail silently ever after. If you need a fixed date, pass it explicitly through the
pipeline rather than freezing it here."""


def _empty_config(database_url: str) -> AppConfig:
    """An AppConfig with no configured sources -- collect becomes a no-op, letting a test's manually
    seeded raw_offer stand in for "already collected" data without any network/collector involved."""
    return AppConfig(database_url=database_url, sources=SourcesConfig(sources=[]), brands=BrandAliasesConfig())


# --------------------------------------------------------------------------------------------------
# acquire_run_lock (Zadanie 1: concurrency)
# --------------------------------------------------------------------------------------------------


def test_second_concurrent_acquire_raises_run_lock_error(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'lock_test.db'}"
    with pipeline.acquire_run_lock(db_url):
        with pytest.raises(RunLockError):
            with pipeline.acquire_run_lock(db_url):
                pass  # pragma: no cover -- must never be reached


def test_lock_is_released_on_exit_and_can_be_reacquired(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'lock_test2.db'}"
    with pipeline.acquire_run_lock(db_url):
        pass
    with pipeline.acquire_run_lock(db_url):  # must not raise -- the first lock was released
        pass


def test_lock_is_released_even_if_the_protected_work_raises(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'lock_test3.db'}"
    with pytest.raises(ValueError):
        with pipeline.acquire_run_lock(db_url):
            raise ValueError("boom")
    with pipeline.acquire_run_lock(db_url):  # must not raise
        pass


def test_stale_lock_past_ttl_is_taken_over_automatically(tmp_path: Path) -> None:
    db_url = f"sqlite:///{tmp_path / 'lock_test4.db'}"
    lock_path = pipeline._lock_path_for(db_url)
    lock_path.write_text(json.dumps({"pid": 999999, "acquired_at": "long ago"}), encoding="utf-8")
    old_time = time.time() - 3600  # 1 hour in the past
    os.utime(lock_path, (old_time, old_time))

    with pipeline.acquire_run_lock(db_url, ttl_seconds=1.0):  # ttl=1s, lock is 3600s old -> stale, taken over
        pass


def test_two_different_database_urls_do_not_share_a_lock(tmp_path: Path) -> None:
    url_a = f"sqlite:///{tmp_path / 'a.db'}"
    url_b = f"sqlite:///{tmp_path / 'b.db'}"
    with pipeline.acquire_run_lock(url_a):
        with pipeline.acquire_run_lock(url_b):  # different DB -- must not collide with url_a's lock
            pass


# --------------------------------------------------------------------------------------------------
# Fixture: realistic raw_offer payloads that GenericNormalizer can actually turn into matched offers
# --------------------------------------------------------------------------------------------------

_SSD_2TB_EAN = "4006381333931"  # confirmed-valid EAN-13, reused from tests/conftest.py's sample_offers


def _seed_sources(session: Session) -> dict[str, int]:
    ids = {
        name: dealradar_db.upsert_source(
            session, Source(name=name, kind="feed", base_url=f"https://{name}.example", risk_prior=0.0)
        )
        for name in ("x-kom", "mediaexpert")
    }
    session.commit()
    return ids


def _insert_raw(session: Session, source_id: int, external_id: str, payload: dict[str, Any]) -> None:
    from dealradar.models import RawOffer, compute_content_hash

    raw = RawOffer(
        source_id=source_id, external_id=external_id, fetched_at=NOW,
        payload_json=payload, content_hash=compute_content_hash(payload),
    )
    dealradar_db.insert_raw_offer(session, raw)


def _seed_raw_offers(session: Session) -> dict[str, int]:
    """Seeds raw_offer with one matched SSD pair (x-kom + mediaexpert, same EAN) plus 3 peer SSDs, all in
    category "Dyski SSD" (maps to elektronika/dyski_ssd per config/category_map.yaml). Returns source ids.
    """
    source_ids = _seed_sources(session)

    _insert_raw(session, source_ids["x-kom"], "xk-ssd-990pro-2tb", {
        "title": "Dysk SSD Samsung 990 PRO 2TB M.2 NVMe", "ean": _SSD_2TB_EAN,
        "brand": "Samsung", "category": "Dyski SSD", "price": 899.00, "shipping_cost": 0.0,
        "price_before": 1180.00, "warranty_months": 60, "url": "https://x-kom.example/990pro-2tb",
        "rating": 4.8, "rating_count": 1200, "in_stock": True,
    })
    _insert_raw(session, source_ids["mediaexpert"], "me-ssd-990pro-2tb", {
        "title": "SSD Samsung 990 PRO 2TB M.2 PCIe 4.0", "ean": _SSD_2TB_EAN,
        "brand": "Samsung", "category_raw": "dyski ssd", "price_gross": 649.00, "shipping_cost": 0.0,
        "url": "https://mediaexpert.example/990pro-2tb", "in_stock": True,
    })
    for i, (ean, price) in enumerate(
        [("5901234123457", 550.00), ("3017620422003", 700.00), ("40170725", 600.00)]
    ):
        _insert_raw(session, source_ids["x-kom"], f"xk-peer-ssd-{i}", {
            "title": f"Dysk SSD Inny Producent Model {i} 1TB M.2", "ean": ean,
            "brand": f"Brand{i}", "category": "Dyski SSD", "price": price, "shipping_cost": 0.0,
            "url": f"https://x-kom.example/peer-ssd-{i}", "in_stock": True,
        })
    session.commit()
    return source_ids


# --------------------------------------------------------------------------------------------------
# run_full: end-to-end on fixtures (Definicja ukonczenia #1)
# --------------------------------------------------------------------------------------------------


def test_run_full_end_to_end_produces_matched_offers_and_deal_scores(db: Session) -> None:
    _seed_raw_offers(db)
    cfg = _empty_config("sqlite:///:memory:")

    report = pipeline.run_full(db, cfg, since=None, offline=True)

    assert report.stage == "run"
    assert "skipped score" not in " ".join(report.notes)
    offers = db.execute(text("SELECT id, product_id, match_method FROM offer")).mappings().all()
    assert len(offers) == 5
    matched_pair = [o for o in offers if o["match_method"] == "ean"]
    assert len(matched_pair) == 2  # the x-kom/mediaexpert 990 PRO 2TB pair, matched by shared EAN
    assert matched_pair[0]["product_id"] == matched_pair[1]["product_id"]

    products = db.execute(text("SELECT COUNT(*) FROM product")).scalar_one()
    assert products == 4  # the matched pair's 1 product + 3 standalone peers

    deals = db.execute(text("SELECT COUNT(*) FROM deal")).scalar_one()
    # Only the matched pair gets a deal row on day 1: cross_shop basis needs >=2 today-offers of the same
    # product (Z4 -- a lone peer offer with no history gets no fabricated deal_score, see scoring/deal.py).
    assert deals == 2


def test_run_full_is_safe_to_run_twice_in_a_row(db: Session) -> None:
    """Idempotence (ARCHITEKTURA.md sec. 11): a second run on unchanged raw_offer must not corrupt state or
    orphan the first run's product ids (see pipeline.py module docstring on the incremental-since default)."""
    _seed_raw_offers(db)
    cfg = _empty_config("sqlite:///:memory:")

    pipeline.run_full(db, cfg, since=None, offline=True)
    products_after_first = {
        r["id"]: r["canonical_title"] for r in db.execute(text("SELECT id, canonical_title FROM product")).mappings().all()
    }

    pipeline.run_full(db, cfg, offline=True)  # second call: since defaults to "last successful run"
    products_after_second = {
        r["id"]: r["canonical_title"] for r in db.execute(text("SELECT id, canonical_title FROM product")).mappings().all()
    }
    assert products_after_first == products_after_second  # same ids, same titles -- nothing orphaned/duplicated


# --------------------------------------------------------------------------------------------------
# Failure isolation (Definicja ukonczenia #2, Zadanie 1: "awaria etapu nie kasuje pracy poprzednich")
# --------------------------------------------------------------------------------------------------


def test_a_broken_source_does_not_abort_the_run_and_is_visible_afterwards(db: Session) -> None:
    """A source misconfigured to always fail must not stop normalize/match/snapshot from running, and must
    show up in the aggregate report's sources_failed / notes (feeding the digest's UWAGI section, see
    tests/reporting/test_health.py's source_zero coverage)."""
    _seed_raw_offers(db)
    cfg = AppConfig(
        database_url="sqlite:///:memory:",
        sources=SourcesConfig(sources=[SourceConfig(name="dummy", kind="api", base_url="https://x.invalid", risk_prior=0.0)]),
        brands=BrandAliasesConfig(),
    )
    report = pipeline.run_full(db, cfg, since=None, offline=True)  # "dummy" has no registered collector -> fails
    assert "dummy" in report.sources_failed
    # The rest of the pipeline still ran on the seeded raw_offer despite collect's failure:
    offers = db.execute(text("SELECT COUNT(*) FROM offer")).scalar_one()
    assert offers == 5
    deals = db.execute(text("SELECT COUNT(*) FROM deal")).scalar_one()
    assert deals == 2  # see test_run_full_end_to_end_... for why only the matched pair scores on day 1


def test_match_stage_failure_skips_scoring_but_keeps_normalize_work(db: Session, monkeypatch: Any) -> None:
    _seed_raw_offers(db)
    cfg = _empty_config("sqlite:///:memory:")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated matcher crash")

    monkeypatch.setattr(pipeline, "match_offers", _boom)
    report = pipeline.run_full(db, cfg, since=None, offline=True)

    assert report.status == "partial"
    assert any("match' FAILED" in n for n in report.notes)
    assert any("skipped score" in n for n in report.notes)
    # normalize's work (offer rows) is still there -- one stage's failure did not roll back an earlier one:
    offers = db.execute(text("SELECT COUNT(*) FROM offer")).scalar_one()
    assert offers == 5
    deals = db.execute(text("SELECT COUNT(*) FROM deal")).scalar_one()
    assert deals == 0


# --------------------------------------------------------------------------------------------------
# rebuild --from raw (Definicja ukonczenia #3, Z1)
# --------------------------------------------------------------------------------------------------


def _derived_state_fingerprint(session: Session) -> str:
    """A sha256 checksum of offer/product/deal/quality_score, keyed by *stable* identity -- (source_name,
    external_id) for offers, and the frozenset of an offer-cluster's (source_name, external_id) pairs for
    product/deal/quality_score -- rather than by raw autoincrement id.

    Autoincrement ids are NOT expected to match between two `rebuild --from raw` calls: rebuild clears and
    recreates `product`/`deal`/`quality_score` from scratch each time (see pipeline.py's module docstring),
    and SQLite's AUTOINCREMENT never reuses a value even after DELETE. Z1's actual guarantee is that the same
    raw_offer input produces the same *derived values* -- this fingerprint is built to test exactly that,
    not row-id stability, which was never promised.
    """
    offer_rows = session.execute(
        text(
            "SELECT o.id, s.name AS source_name, o.external_id, o.price_total, o.ean, o.match_method, o.product_id "
            "FROM offer o JOIN source s ON s.id = o.source_id"
        )
    ).mappings().all()
    offer_key: dict[int, list[str]] = {int(r["id"]): [r["source_name"], r["external_id"]] for r in offer_rows}

    cluster_by_product: dict[int, list[list[str]]] = {}
    for r in offer_rows:
        if r["product_id"] is not None:
            cluster_by_product.setdefault(int(r["product_id"]), []).append([r["source_name"], r["external_id"]])
    for members in cluster_by_product.values():
        members.sort()

    offers_struct = sorted(
        (
            {
                "key": offer_key[int(r["id"])],
                "price_total": r["price_total"],
                "ean": r["ean"],
                "match_method": r["match_method"],
                "cluster": cluster_by_product.get(int(r["product_id"])) if r["product_id"] is not None else None,
            }
            for r in offer_rows
        ),
        key=lambda d: d["key"],
    )

    product_rows = session.execute(text("SELECT id, canonical_title, brand, mpn, ean, category_id FROM product")).mappings().all()
    products_struct = sorted(
        (
            {
                "cluster": cluster_by_product.get(int(r["id"]), []),
                "canonical_title": r["canonical_title"], "brand": r["brand"], "mpn": r["mpn"],
                "ean": r["ean"], "category_id": r["category_id"],
            }
            for r in product_rows
        ),
        key=lambda d: json.dumps(d["cluster"], sort_keys=True),
    )

    deal_rows = session.execute(
        text(
            "SELECT d.offer_id, d.basis, d.deal_score, d.depth_vs_median_90d, d.z_robust, d.price_rank_today, "
            "d.is_alltime_low, d.n_competing_offers, d.n_days_history, d.coverage_cap FROM deal d "
            "WHERE d.id = (SELECT MAX(d2.id) FROM deal d2 WHERE d2.offer_id = d.offer_id)"
        )
    ).mappings().all()
    deals_struct = sorted(
        (
            {**{k: v for k, v in dict(r).items() if k != "offer_id"}, "offer_key": offer_key[int(r["offer_id"])]}
            for r in deal_rows
        ),
        key=lambda d: d["offer_key"],
    )

    quality_rows = session.execute(
        text(
            "SELECT q.product_id, q.value_score, q.reliability_score, q.deal_strength, q.risk_score, "
            "q.total_1_10, q.confidence, q.missing_reason FROM quality_score q "
            "WHERE q.id = (SELECT MAX(q2.id) FROM quality_score q2 WHERE q2.product_id = q.product_id)"
        )
    ).mappings().all()
    quality_struct = sorted(
        (
            {**{k: v for k, v in dict(r).items() if k != "product_id"}, "cluster": cluster_by_product.get(int(r["product_id"]), [])}
            for r in quality_rows
        ),
        key=lambda d: json.dumps(d["cluster"], sort_keys=True),
    )

    blob = json.dumps(
        {"offers": offers_struct, "products": products_struct, "deals": deals_struct, "quality": quality_struct},
        sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def test_rebuild_from_raw_twice_produces_identical_derived_state(db: Session) -> None:
    """The Z1 test the task spec explicitly asks for: two `rebuild --from raw` passes over the same
    raw_offer/price_point must yield byte-identical derived state (offer/product/deal/quality_score),
    proving the scoring algorithm can be safely recomputed from scratch at any time."""
    _seed_raw_offers(db)
    cfg = _empty_config("sqlite:///:memory:")
    pipeline.run_full(db, cfg, since=None, offline=True)  # establishes an initial offer/product/deal state

    first_report = pipeline.rebuild(db, "raw", since=None)
    assert first_report.status != "failed"
    fingerprint_1 = _derived_state_fingerprint(db)

    second_report = pipeline.rebuild(db, "raw", since=None)
    assert second_report.status != "failed"
    fingerprint_2 = _derived_state_fingerprint(db)

    assert fingerprint_1 == fingerprint_2


def test_rebuild_from_raw_does_not_touch_price_point(db: Session) -> None:
    """rebuild --from raw is documented (module docstring) to leave price_point untouched -- it is collected
    history, not a pure function of raw_offer, and re-running snapshot would make two rebuilds diverge."""
    _seed_raw_offers(db)
    cfg = _empty_config("sqlite:///:memory:")
    pipeline.run_full(db, cfg, since=None, offline=True)
    before = {
        (r["offer_id"], r["observed_at"]): r["price_total"]
        for r in db.execute(text("SELECT offer_id, observed_at, price_total FROM price_point")).mappings().all()
    }
    pipeline.rebuild(db, "raw", since=None)
    after = {
        (r["offer_id"], r["observed_at"]): r["price_total"]
        for r in db.execute(text("SELECT offer_id, observed_at, price_total FROM price_point")).mappings().all()
    }
    assert before == after


def test_rebuild_unsupported_from_stage_raises_value_error(db: Session) -> None:
    with pytest.raises(ValueError):
        pipeline.rebuild(db, "nonsense", since=None)


def test_rebuild_from_score_only_recomputes_deal_and_quality(db: Session) -> None:
    _seed_raw_offers(db)
    cfg = _empty_config("sqlite:///:memory:")
    pipeline.run_full(db, cfg, since=None, offline=True)
    products_before = db.execute(text("SELECT id FROM product ORDER BY id")).scalars().all()

    report = pipeline.rebuild(db, "score", since=None)
    assert report.status != "failed"
    products_after = db.execute(text("SELECT id FROM product ORDER BY id")).scalars().all()
    assert products_before == products_after  # --from score never touches product/offer, only deal/quality
