"""End-to-end proof that match_offers() actually reads A2's `offer.evidence["attrs"]` from the DB,
not just that the attrs.py helpers work in isolation -- this is the cross-agent contract point A2
flagged directly (see the A3 final report's reconciliation note).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from dealradar import db as dealradar_db
from dealradar.matching.service import match_offers
from dealradar.models import Offer, Source

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def test_match_offers_reads_evidence_attrs_from_the_database(db: Any) -> None:
    source_id = dealradar_db.upsert_source(
        db, Source(name="x-kom", kind="feed", base_url="https://x-kom.example", enabled=True, risk_prior=0.0)
    )
    db.commit()
    category_id = db.execute(
        text("INSERT INTO category (parent_id, name, path) VALUES (NULL, 'dyski_ssd', 'dyski_ssd') RETURNING id")
    ).scalar_one()
    db.commit()

    # Both offers' title_norm says "512gb" (so this module's own regex extractor alone would see
    # capacity_gb=512 on both sides and find no conflict). offer B carries A2-style evidence saying
    # the real capacity is 1024GB (e.g. A2 trusted a feed_field over the title text) -- if
    # match_offers is reading evidence.attrs as designed, the two must NOT merge.
    offer_a = Offer(
        source_id=source_id, external_id="ev-a", first_seen=NOW, last_seen=NOW,
        title="Dysk SSD Kryptonite 512GB", title_norm="dysk ssd kryptonite 512gb",
        brand="Kryptonite", brand_norm="kryptonite", mpn="KX512", mpn_norm="KX512",
        category_id=category_id, url="https://x-kom.example/ev-a", condition="new",
        price_gross=39900, shipping_cost=0, currency="PLN",
    )
    offer_b = Offer(
        source_id=source_id, external_id="ev-b", first_seen=NOW, last_seen=NOW,
        title="Dysk SSD Kryptonite 512GB", title_norm="dysk ssd kryptonite 512gb",
        brand="Kryptonite", brand_norm="kryptonite", mpn="KX512", mpn_norm="KX512",
        category_id=category_id, url="https://x-kom.example/ev-b", condition="new",
        price_gross=69900, shipping_cost=0, currency="PLN",
        evidence={"attrs": [{"key": "capacity_gb", "value_num": 1024.0, "unit": "GB", "extracted_from": "feed_field"}]},
    )
    id_a = dealradar_db.upsert_offer(db, offer_a)
    id_b = dealradar_db.upsert_offer(db, offer_b)
    db.commit()

    # Sanity check: evidence really did round-trip through the DB as JSON text.
    raw_evidence = db.execute(text("SELECT evidence FROM offer WHERE id = :id"), {"id": id_b}).scalar_one()
    assert json.loads(raw_evidence)["attrs"][0]["value_num"] == 1024.0

    match_offers(db, full=True)
    db.commit()

    product_a = db.execute(text("SELECT product_id FROM offer WHERE id = :id"), {"id": id_a}).scalar_one()
    product_b = db.execute(text("SELECT product_id FROM offer WHERE id = :id"), {"id": id_b}).scalar_one()
    assert product_a != product_b, (
        "offer B's evidence.attrs said capacity_gb=1024 (vs offer A's title-derived 512) -- "
        "match_offers must have read the DB's evidence column to catch this, and it didn't"
    )
