"""End-to-end tests for match_offers(): assignment, clustering quality, and product.id stability."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.matching.service import match_offers

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _product_id_of(session: Session, offer_id: int) -> int | None:
    row = session.execute(text("SELECT product_id FROM offer WHERE id = :id"), {"id": offer_id}).first()
    assert row is not None
    return None if row[0] is None else int(row[0])


def test_every_offer_gets_a_product_id(matching_world: Any) -> None:
    report = match_offers(matching_world.session, full=True)
    matching_world.session.commit()

    assert report.offers_total == len(matching_world.offer_id)
    rows = matching_world.session.execute(text("SELECT id, product_id FROM offer")).all()
    assert len(rows) == report.offers_total
    for offer_id, product_id in rows:
        assert product_id is not None, f"offer {offer_id} has no product_id after match_offers"


def test_true_match_families_cluster_together(matching_world: Any) -> None:
    match_offers(matching_world.session, full=True)
    matching_world.session.commit()

    failures = []
    for family, keys in matching_world.true_match_families.items():
        product_ids = {_product_id_of(matching_world.session, matching_world.offer_id[k]) for k in keys}
        if len(product_ids) != 1:
            failures.append((family, keys, product_ids))
    assert not failures, f"these true-match families did not fully cluster: {failures}"


def test_trap_pairs_never_share_a_product(matching_world: Any) -> None:
    match_offers(matching_world.session, full=True)
    matching_world.session.commit()

    assert len(matching_world.trap_pairs) >= 30
    merged = []
    for key_a, key_b in matching_world.trap_pairs:
        pid_a = _product_id_of(matching_world.session, matching_world.offer_id[key_a])
        pid_b = _product_id_of(matching_world.session, matching_world.offer_id[key_b])
        if pid_a is not None and pid_a == pid_b:
            merged.append((key_a, key_b))
    assert not merged, f"these trap pairs were incorrectly merged into the same product: {merged}"


def test_aliexpress_style_offers_stay_singletons(matching_world: Any) -> None:
    report = match_offers(matching_world.session, full=True)
    matching_world.session.commit()

    ali_keys = [k for k in matching_world.offer_id if k.startswith("ali_")]
    assert len(ali_keys) == 15
    for key in ali_keys:
        row = matching_world.session.execute(
            text("SELECT product_id, match_method FROM offer WHERE id = :id"),
            {"id": matching_world.offer_id[key]},
        ).first()
        assert row is not None
        product_id, method = row
        assert product_id is not None  # still gets a solo product (own price history is still useful)
        assert method == "none"

    assert report.offers_singleton >= 15


def test_report_shape(matching_world: Any) -> None:
    report = match_offers(matching_world.session, full=True)
    assert report.pct_matched is not None
    assert 0.0 <= report.pct_matched <= 100.0
    assert report.n_clusters > 0
    assert report.largest_cluster_size <= report.offers_total
    assert set(report.by_method.keys()) <= {"ean", "brand_mpn", "fuzzy", "manual", "none"}
    assert sum(report.by_method.values()) == report.offers_total
    assert report.ean_conflicts >= 0
    assert report.products_created > 0


def test_rerun_does_not_renumber_existing_products(matching_world: Any) -> None:
    match_offers(matching_world.session, full=True)
    matching_world.session.commit()

    before = {
        key: _product_id_of(matching_world.session, offer_id)
        for key, offer_id in matching_world.offer_id.items()
    }

    match_offers(matching_world.session, full=True)
    matching_world.session.commit()
    match_offers(matching_world.session, full=False)
    matching_world.session.commit()

    after = {
        key: _product_id_of(matching_world.session, offer_id)
        for key, offer_id in matching_world.offer_id.items()
    }
    assert before == after, "re-running match_offers changed at least one offer's product_id"


def test_oversized_cluster_is_flagged_in_the_report(tmp_path: Any) -> None:
    """A cluster bigger than cluster.max_size_warn must show up in report.large_clusters (task spec: '>50 offers is almost certainly a bug')."""
    import yaml

    from dealradar import db as dealradar_db
    from dealradar.matching.config import DEFAULT_MATCHING_CONFIG_PATH
    from dealradar.models import Offer, Source

    base_cfg = yaml.safe_load(DEFAULT_MATCHING_CONFIG_PATH.read_text(encoding="utf-8"))
    base_cfg["cluster"]["max_size_warn"] = 3
    small_cfg_path = tmp_path / "matching.yaml"
    small_cfg_path.write_text(yaml.safe_dump(base_cfg), encoding="utf-8")

    engine = dealradar_db.build_engine("sqlite:///:memory:")
    dealradar_db.run_migrations(engine)
    session = dealradar_db.make_session_factory(engine)()
    try:
        source_id = dealradar_db.upsert_source(
            session, Source(name="x-kom", kind="feed", base_url="https://x-kom.example", enabled=True, risk_prior=0.0)
        )
        session.commit()
        ean = "4006381333931"
        for i in range(5):  # 5 offers sharing one EAN -> one cluster of size 5 > max_size_warn=3
            offer = Offer(
                source_id=source_id, external_id=f"big-{i}", first_seen=NOW, last_seen=NOW,
                title=f"Dysk SSD Test 1TB wariant {i}", title_norm=f"dysk ssd test 1tb wariant {i}",
                brand="Test", brand_norm="test", ean=ean, url=f"https://x-kom.example/big-{i}",
                condition="new", price_gross=10000 + i, shipping_cost=0, currency="PLN",
            )
            dealradar_db.upsert_offer(session, offer)
        session.commit()

        report = match_offers(session, full=True, config_path=small_cfg_path)
        assert report.largest_cluster_size == 5
        assert len(report.large_clusters) == 1
        assert "5" in report.large_clusters[0]
    finally:
        session.close()


def test_incremental_run_does_not_touch_already_matched_offers(matching_world: Any) -> None:
    match_offers(matching_world.session, full=True)
    matching_world.session.commit()

    before_row = matching_world.session.execute(
        text("SELECT product_id, match_confidence, match_method FROM offer WHERE id = :id"),
        {"id": matching_world.offer_id["f1_xkom"]},
    ).first()
    assert before_row is not None

    # A brand-new offer arrives (simulating a fresh collect+normalize run) that should join an
    # existing cluster; incremental match must not disturb the already-matched f1_* offers.
    from dealradar import db as dealradar_db
    from dealradar.models import Offer
    new_offer = Offer(
        source_id=matching_world.source_id["pepper"],
        external_id="pepper-f1-new",
        first_seen=NOW, last_seen=NOW,
        title="[Deal] Samsung 990 PRO 2TB SSD -20%",
        title_norm="samsung 990 pro 2tb ssd 20%".replace(" 20%", ""),
        brand="Samsung", brand_norm="samsung", mpn="MZ-V9P2T0BW", mpn_norm="MZV9P2T0BW",
        ean=None, category_id=matching_world.category_id["dyski_ssd"],
        url="https://pepper.example/f1-new", condition="new", price_gross=86900, shipping_cost=0,
        currency="PLN",
    )
    new_id = dealradar_db.upsert_offer(matching_world.session, new_offer)
    matching_world.session.commit()

    match_offers(matching_world.session, full=False)
    matching_world.session.commit()

    after_row = matching_world.session.execute(
        text("SELECT product_id, match_confidence, match_method FROM offer WHERE id = :id"),
        {"id": matching_world.offer_id["f1_xkom"]},
    ).first()
    assert after_row is not None
    assert before_row == after_row

    new_product_id = _product_id_of(matching_world.session, new_id)
    f1_product_id = _product_id_of(matching_world.session, matching_world.offer_id["f1_xkom"])
    assert new_product_id == f1_product_id, "new offer should have joined the existing Samsung 990 PRO 2TB cluster"
