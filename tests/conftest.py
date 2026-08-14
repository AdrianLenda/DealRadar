"""Shared pytest fixtures for DealRadar.

`db` gives a Session on a fresh in-memory SQLite database with the full schema already migrated.
`sample_offers` gives 20 realistic, *not yet persisted* Offer instances spanning five fictional sources,
so the rest of the team can build their own tests on top of this without depending on A0's test internals.
It deliberately includes: several offers without an EAN, one offer with a checksum-invalid EAN (to exercise
the auto-null-and-record-reason path), several without shipping_cost (price_incomplete=True), one bundle
(dwupak) and one refurb/poleasing unit, per the A0 task spec.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.models import Offer, Source, validate_gtin

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)


def _ean13(body11: str) -> str:
    """Appends the correct GTIN-13 check digit to an 11-digit body; returns a fully valid 13-digit EAN."""
    for d in range(10):
        candidate = f"{body11}{d}"
        if validate_gtin(candidate):
            return candidate
    raise AssertionError(f"no valid check digit found for body {body11!r}")


@pytest.fixture()
def db() -> Iterator[Session]:
    """Yields a Session bound to a fresh in-memory SQLite database with the full schema already migrated."""
    engine = dealradar_db.build_engine("sqlite:///:memory:")
    dealradar_db.run_migrations(engine)
    session = dealradar_db.make_session_factory(engine)()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def sample_offers(db: Session) -> list[Offer]:
    """Returns 20 realistic, unpersisted Offer instances across 5 sources; also registers those 5 sources in `db`."""
    source_ids = {
        name: dealradar_db.upsert_source(
            db, Source(name=name, kind=kind, base_url=f"https://{name}.example", enabled=True, risk_prior=risk)
        )
        for name, kind, risk in [
            ("x-kom", "feed", 0.0),
            ("mediaexpert", "feed", 0.05),
            ("allegro", "api", 0.1),
            ("ibood", "feed", 0.1),
            ("pepper", "rss", 0.05),
        ]
    }
    db.commit()

    # Confirmed-valid, independently-sourced real EAN-13 / EAN-8 codes (see A0 final report for provenance);
    # the rest are synthesized with a correct check digit purely for fixture variety.
    ean_ssd_2tb = "4006381333931"
    ean_ssd_1tb = "5901234123457"
    ean_monitor = "3017620422003"
    ean_powerbank_anker = "40170725"
    ean_laptop_lenovo = "73513537"
    ean_phone_xiaomi = "96385074"
    ean_earbuds = _ean13("59012345678")
    ean_ssd_kingston = _ean13("59098765432")
    ean_powerbank_baseus = _ean13("59011112222")
    ean_bundle_batteries = _ean13("59033334444")
    ean_phone_bad_checksum = ean_ssd_2tb[:-1] + str((int(ean_ssd_2tb[-1]) + 1) % 10)  # deliberately invalid

    def offer(**kwargs: object) -> Offer:
        base: dict[str, object] = dict(
            first_seen=NOW,
            last_seen=NOW,
            currency="PLN",
            condition="new",
        )
        base.update(kwargs)
        return Offer(**base)  # type: ignore[arg-type]

    return [
        offer(
            source_id=source_ids["x-kom"], external_id="xk-ssd-990pro-2tb",
            title="Dysk SSD Samsung 990 PRO 2TB M.2 NVMe", ean=ean_ssd_2tb,
            brand="Samsung", brand_norm="samsung", mpn="MZ-V9P2T0BW",
            url="https://x-kom.example/ssd-990pro-2tb", price_gross=89900, shipping_cost=0,
            rating_avg=4.8, rating_count=1240, warranty_months=60, in_stock=True,
        ),
        offer(
            source_id=source_ids["mediaexpert"], external_id="me-ssd-990pro-2tb",
            title="SSD Samsung 990 PRO 2TB M.2 PCIe 4.0", ean=ean_ssd_2tb,
            brand="Samsung", brand_norm="samsung", mpn="MZ-V9P2T0BW",
            url="https://mediaexpert.example/ssd-990pro-2tb", price_gross=87900, shipping_cost=None,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["allegro"], external_id="al-ssd-990pro-1tb",
            title="Dysk SSD Samsung 990 PRO 1TB M.2 NVMe", ean=ean_ssd_1tb,
            brand="Samsung", brand_norm="samsung", mpn="MZ-V9P1T0BW",
            url="https://allegro.example/ssd-990pro-1tb", price_gross=49900, shipping_cost=0,
            seller="ssd-store", seller_rating=4.9, in_stock=True,
        ),
        offer(
            source_id=source_ids["ibood"], external_id="ib-ssd-990pro-1tb",
            title="Samsung 990 PRO 1TB - dagdeal!", ean=ean_ssd_1tb,
            brand="Samsung", brand_norm="samsung",
            url="https://ibood.example/ssd-990pro-1tb", price_gross=45900, shipping_cost=1500,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["pepper"], external_id="pp-lg-27gp850",
            title="[Deal] LG UltraGear 27GP850 27\" 165Hz -30%", ean=ean_monitor,
            brand="LG", brand_norm="lg",
            url="https://pepper.example/lg-27gp850", price_gross=179900, shipping_cost=0,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["x-kom"], external_id="xk-lg-27gp850",
            title="Monitor LG UltraGear 27GP850 27 cali 165Hz", ean=ean_monitor,
            brand="LG", brand_norm="lg",
            url="https://x-kom.example/lg-27gp850", price_gross=189900, shipping_cost=None,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["mediaexpert"], external_id="me-anker-20000",
            title="Powerbank Anker PowerCore 20000mAh", ean=ean_powerbank_anker,
            brand="Anker", brand_norm="anker",
            url="https://mediaexpert.example/anker-20000", price_gross=15900, shipping_cost=999,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["allegro"], external_id="al-anker-20000",
            title="Powerbank Anker PowerCore 20000 mAh NOWY FV23", ean=None,
            brand="Anker", brand_norm="anker",
            url="https://allegro.example/anker-20000", price_gross=14900, shipping_cost=0,
            seller="powerbank-shop", seller_rating=4.6, in_stock=True,
        ),
        offer(
            source_id=source_ids["ibood"], external_id="ib-lenovo-slim5",
            title="Lenovo IdeaPad Slim 5 14ITL05", ean=ean_laptop_lenovo,
            brand="Lenovo", brand_norm="lenovo",
            url="https://ibood.example/lenovo-slim5", price_gross=289900, shipping_cost=0,
            warranty_months=24, in_stock=True,
        ),
        offer(
            source_id=source_ids["pepper"], external_id="pp-lenovo-slim5",
            title="[Gorące] Lenovo IdeaPad Slim 5 -15%", ean=None,
            brand="Lenovo", brand_norm="lenovo",
            url="https://pepper.example/lenovo-slim5", price_gross=259900, shipping_cost=None,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["x-kom"], external_id="xk-xiaomi-note13",
            title="Xiaomi Redmi Note 13 8/256GB", ean=ean_phone_xiaomi,
            brand="Xiaomi", brand_norm="xiaomi",
            url="https://x-kom.example/redmi-note13", price_gross=99900, shipping_cost=0,
            rating_avg=4.5, rating_count=340, in_stock=True,
        ),
        offer(
            source_id=source_ids["mediaexpert"], external_id="me-xiaomi-note13",
            title="Smartfon Xiaomi Redmi Note 13 8/256GB", ean=ean_phone_bad_checksum,
            brand="Xiaomi", brand_norm="xiaomi",
            url="https://mediaexpert.example/redmi-note13", price_gross=104900, shipping_cost=1200,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["allegro"], external_id="al-dell-5420-poleasing",
            title="Laptop Dell Latitude 5420 poleasingowy i5/16GB", ean=None,
            brand="Dell", brand_norm="dell", condition="refurb",
            url="https://allegro.example/dell-5420-poleasing", price_gross=189900, shipping_cost=0,
            seller="it-resell", seller_rating=4.7, warranty_months=12, in_stock=True,
        ),
        offer(
            source_id=source_ids["x-kom"], external_id="xk-duracell-aa-2pak",
            title="Baterie Duracell AA 2-pak", ean=ean_bundle_batteries,
            brand="Duracell", brand_norm="duracell", is_bundle=True, bundle_size=2,
            url="https://x-kom.example/duracell-aa-2pak", price_gross=1999, shipping_cost=0,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["mediaexpert"], external_id="me-samsung-buds",
            title="Samsung Galaxy Buds FE", ean=ean_earbuds,
            brand="Samsung", brand_norm="samsung",
            url="https://mediaexpert.example/galaxy-buds-fe", price_gross=59900, shipping_cost=0,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["ibood"], external_id="ib-xiaomi-powerbank",
            title="Xiaomi Powerbank 20000mAh 22.5W", ean=None,
            brand="Xiaomi", brand_norm="xiaomi",
            url="https://ibood.example/xiaomi-powerbank", price_gross=8900, shipping_cost=1500,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["pepper"], external_id="pp-kingston-nv2-1tb",
            title="[Okazja] Kingston NV2 1TB SSD M.2", ean=ean_ssd_kingston,
            brand="Kingston", brand_norm="kingston",
            url="https://pepper.example/kingston-nv2-1tb", price_gross=24900, shipping_cost=None,
            in_stock=True,
        ),
        offer(
            source_id=source_ids["allegro"], external_id="al-dell-p2422h-used",
            title="Monitor Dell P2422H używany", ean=None,
            brand="Dell", brand_norm="dell", condition="used",
            url="https://allegro.example/dell-p2422h-used", price_gross=59900, shipping_cost=0,
            seller="uzywane-monitory", seller_rating=4.2, in_stock=True,
        ),
        offer(
            source_id=source_ids["x-kom"], external_id="xk-asus-vivobook-damaged",
            title="Laptop Asus Vivobook (opakowanie uszkodzone)", ean=None,
            brand="Asus", brand_norm="asus", condition="damaged",
            url="https://x-kom.example/asus-vivobook-outlet", price_gross=129900, shipping_cost=0,
            warranty_months=24, in_stock=True,
        ),
        offer(
            source_id=source_ids["mediaexpert"], external_id="me-baseus-10000",
            title="Powerbank Baseus 10000mAh 20W", ean=ean_powerbank_baseus,
            brand="Baseus", brand_norm="baseus",
            url="https://mediaexpert.example/baseus-10000", price_gross=6900, shipping_cost=0,
            in_stock=True,
        ),
    ]
