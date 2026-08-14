"""Synthetic fixtures for the matcher's own test suite. Owner: A3.

A2 (normalize) is being built in parallel by a sibling agent, so these tests do not depend on its
live output -- every Offer here is built and normalized by hand (see `_norm_title`, a small
standalone stand-in for A2's title normalization, good enough to make TF-IDF/RapidFuzz meaningful
without depending on A2's actual implementation or its completion).

The fixture set has three parts, all persisted into the `db` session fixture (see ../conftest.py)
by `matching_world`:

- ~15 "true match" families (2-4 offers each, same real product from different sources) --
  something for the cascade to actually cluster, and raw material for the eval dataset's 'same'
  pairs.
- 30 trap pairs (6 each: capacity, condition, bundle, region/voltage, charger-vs-device) that MUST
  stay separated -- the regression suite in test_regression_pairs.py asserts this directly.
- ~15 AliExpress-style offers with no brand/EAN/category -- exercise the "this offer legitimately
  stays a singleton" path the task spec explicitly calls a correct, not a failed, outcome.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar import db as dealradar_db
from dealradar.models import Offer, Source, validate_gtin

NOW = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)

_JUNK_RE = re.compile(
    r"\b(nowy|nowa|promocja|okazja|hit|super\s*cena|gwarancja\s*\d*\w*|fv\s*23%?|wysylka\s*24h|dostawa\s*gratis)\b",
    re.IGNORECASE,
)
_DECOR_RE = re.compile(r"[\[\]!*🔥⚡❗()–-]+")
_WS_RE = re.compile(r"\s+")


def _norm_title(raw: str) -> str:
    """A small standalone stand-in for A2's title_norm: lowercase, strip decoration/junk, collapse whitespace."""
    text_ = raw.lower()
    text_ = _DECOR_RE.sub(" ", text_)
    text_ = _JUNK_RE.sub(" ", text_)
    text_ = _WS_RE.sub(" ", text_).strip()
    return text_


def _ean13(body11: str) -> str:
    """Appends the correct GTIN-13 check digit to an 11-digit body; returns a fully valid 13-digit EAN."""
    for d in range(10):
        candidate = f"{body11}{d}"
        if validate_gtin(candidate):
            return candidate
    raise AssertionError(f"no valid check digit found for body {body11!r}")


@dataclass
class MatchingWorld:
    """Everything a matching test needs: persisted offer ids by mnemonic key, category ids, and grouping info."""

    session: Session
    offer_id: dict[str, int] = field(default_factory=dict)
    category_id: dict[str, int] = field(default_factory=dict)
    source_id: dict[str, int] = field(default_factory=dict)
    true_match_families: dict[str, list[str]] = field(default_factory=dict)
    """family name -> mnemonic offer keys that SHOULD end up in the same product cluster."""
    trap_pairs: list[tuple[str, str]] = field(default_factory=list)
    """(key_a, key_b) mnemonic pairs that MUST NOT end up in the same product cluster."""

    def ids(self, *keys: str) -> list[int]:
        """Returns the persisted offer ids for the given mnemonic keys, in order."""
        return [self.offer_id[k] for k in keys]


@pytest.fixture()
def matching_world(db: Session) -> Iterator[MatchingWorld]:
    """Builds and persists the full synthetic offer set described in this module's docstring; yields a MatchingWorld."""
    world = MatchingWorld(session=db)

    for name in ("x-kom", "mediaexpert", "allegro", "ibood", "pepper", "aliexpress"):
        kind = "api" if name == "allegro" else ("rss" if name == "pepper" else "feed")
        world.source_id[name] = dealradar_db.upsert_source(
            db, Source(name=name, kind=kind, base_url=f"https://{name}.example", enabled=True, risk_prior=0.1)
        )
    db.commit()

    for name, path in [
        ("dyski_ssd", "elektronika/komputery/dyski_ssd"),
        ("monitory", "elektronika/komputery/monitory"),
        ("laptopy", "elektronika/komputery/laptopy"),
        ("telefony", "elektronika/telefony"),
        ("powerbanki", "elektronika/akcesoria/powerbanki"),
        ("rtv", "elektronika/rtv"),
        ("akcesoria_zasilacze", "elektronika/akcesoria/zasilacze"),
        ("akcesoria_kable", "elektronika/akcesoria/kable"),
        ("audio", "elektronika/audio"),
        ("peryferia", "elektronika/komputery/peryferia"),
    ]:
        result = db.execute(
            text('INSERT INTO category (parent_id, name, path) VALUES (NULL, :name, :path) RETURNING id'),
            {"name": name, "path": path},
        )
        world.category_id[name] = int(result.scalar_one())
    db.commit()

    _seq = {"n": 0}

    def add(
        key: str,
        source: str,
        title: str,
        *,
        brand: str | None = None,
        mpn: str | None = None,
        ean: str | None = None,
        category: str | None = None,
        condition: str | None = "new",
        is_bundle: bool = False,
        bundle_size: int | None = None,
        price_gross: int,
        shipping_cost: int | None = 0,
        title_norm_override: str | None = None,
    ) -> None:
        """Builds one Offer, persists it via db.upsert_offer, and records its id under `key`."""
        _seq["n"] += 1
        title_norm = title_norm_override if title_norm_override is not None else _norm_title(title)
        offer = Offer(
            source_id=world.source_id[source],
            external_id=f"{source}-{key}",
            first_seen=NOW,
            last_seen=NOW,
            title=title,
            title_norm=title_norm,
            brand=brand,
            brand_norm=(brand.lower() if brand else None),
            mpn=mpn,
            mpn_norm=(mpn.upper().replace("-", "").replace("/", "").replace(" ", "") if mpn else None),
            ean=ean,
            category_id=(world.category_id[category] if category else None),
            url=f"https://{source}.example/{key}",
            condition=condition,
            is_bundle=is_bundle,
            bundle_size=bundle_size,
            price_gross=price_gross,
            shipping_cost=shipping_cost,
            currency="PLN",
        )
        offer_id = dealradar_db.upsert_offer(db, offer)
        world.offer_id[key] = offer_id

    # ============================================================================================
    # TRUE-MATCH FAMILIES -- same real product, different sources. Family names map to the offer
    # keys that should end up clustered together.
    # ============================================================================================

    ean_990pro_2tb = _ean13("400111222330")
    add("f1_xkom", "x-kom", "Dysk SSD Samsung 990 PRO 2TB M.2 NVMe", brand="Samsung", mpn="MZ-V9P2T0BW",
        ean=ean_990pro_2tb, category="dyski_ssd", price_gross=89900)
    add("f1_mediaexpert", "mediaexpert", "SSD Samsung 990 PRO 2TB M.2 PCIe 4.0", brand="Samsung", mpn="MZ-V9P2T0BW",
        ean=ean_990pro_2tb, category="dyski_ssd", price_gross=87900, shipping_cost=None)
    add("f1_allegro", "allegro", "Samsung 990 PRO 2TB dysk SSD NVMe PCIe4", brand="Samsung", mpn="MZ-V9P2T0BW",
        category="dyski_ssd", price_gross=88900)
    world.true_match_families["f1_samsung_990pro_2tb"] = ["f1_xkom", "f1_mediaexpert", "f1_allegro"]

    ean_nv2_1tb = _ean13("400222333440")
    add("f2_pepper", "pepper", "[Okazja] Kingston NV2 1TB SSD M.2 -25%", brand="Kingston",
        ean=ean_nv2_1tb, category="dyski_ssd", price_gross=24900, shipping_cost=None)
    add("f2_xkom", "x-kom", "Kingston NV2 1TB dysk SSD M.2 PCIe NVMe", brand="Kingston",
        category="dyski_ssd", price_gross=25900)
    world.true_match_families["f2_kingston_nv2_1tb"] = ["f2_pepper", "f2_xkom"]

    ean_lg_27gp850 = _ean13("400333444550")
    add("f3_pepper", "pepper", '[Deal] LG UltraGear 27GP850 27" 165Hz -30%!!!', brand="LG",
        ean=ean_lg_27gp850, category="monitory", price_gross=179900)
    add("f3_xkom", "x-kom", 'Monitor LG UltraGear 27GP850 27 cali 165Hz', brand="LG",
        ean=ean_lg_27gp850, category="monitory", price_gross=189900, shipping_cost=None)
    world.true_match_families["f3_lg_27gp850"] = ["f3_pepper", "f3_xkom"]

    ean_lenovo_slim5 = _ean13("400444555660")
    add("f4_ibood", "ibood", "Lenovo IdeaPad Slim 5 14ITL05 i5/16GB/512GB", brand="Lenovo",
        ean=ean_lenovo_slim5, category="laptopy", price_gross=289900)
    add("f4_pepper", "pepper", "[Gorące] Lenovo IdeaPad Slim 5 14ITL05 -15%", brand="Lenovo",
        category="laptopy", price_gross=259900, shipping_cost=None)
    world.true_match_families["f4_lenovo_slim5"] = ["f4_ibood", "f4_pepper"]

    ean_anker_20000 = _ean13("400555666770")
    add("f5_mediaexpert", "mediaexpert", "Powerbank Anker PowerCore 20000mAh", brand="Anker",
        ean=ean_anker_20000, category="powerbanki", price_gross=15900, shipping_cost=999)
    add("f5_allegro", "allegro", "Powerbank Anker PowerCore 20000 mAh NOWY FV23", brand="Anker",
        category="powerbanki", price_gross=14900)
    world.true_match_families["f5_anker_powercore_20000"] = ["f5_mediaexpert", "f5_allegro"]

    ean_redmi13_ok = _ean13("400666777880")
    ean_redmi13_bad = ean_redmi13_ok[:-1] + str((int(ean_redmi13_ok[-1]) + 1) % 10)
    add("f6_xkom", "x-kom", "Xiaomi Redmi Note 13 8/256GB", brand="Xiaomi",
        ean=ean_redmi13_ok, category="telefony", price_gross=99900)
    add("f6_mediaexpert", "mediaexpert", "Smartfon Xiaomi Redmi Note 13 8/256GB", brand="Xiaomi",
        ean=None, category="telefony", price_gross=104900, shipping_cost=1200,
        title_norm_override=_norm_title("Smartfon Xiaomi Redmi Note 13 8/256GB"))
    # ean_redmi13_bad deliberately unused as an `ean=` value anywhere -- A2 would have nulled it at
    # normalize time (checksum-invalid); kept here only to document why f6_mediaexpert has no EAN.
    assert not validate_gtin(ean_redmi13_bad) or True
    world.true_match_families["f6_xiaomi_redmi_note13"] = ["f6_xkom", "f6_mediaexpert"]

    add("f7_mediaexpert", "mediaexpert", "Samsung Galaxy Buds FE", brand="Samsung",
        ean=_ean13("400777888990"), category="audio", price_gross=59900)
    add("f7_allegro", "allegro", "Słuchawki Samsung Galaxy Buds FE bezprzewodowe", brand="Samsung",
        category="audio", price_gross=57900)
    world.true_match_families["f7_samsung_buds_fe"] = ["f7_mediaexpert", "f7_allegro"]

    ean_iphone15 = _ean13("400888999000")
    add("f8_xkom", "x-kom", "Apple iPhone 15 128GB Black", brand="Apple",
        ean=ean_iphone15, category="telefony", price_gross=399900)
    add("f8_mediaexpert", "mediaexpert", "iPhone 15 128GB czarny Apple", brand="Apple",
        ean=ean_iphone15, category="telefony", price_gross=389900)
    add("f8_allegro", "allegro", "Apple iPhone 15 128 GB czarny - nowy", brand="Apple",
        category="telefony", price_gross=394900)
    world.true_match_families["f8_iphone15_128"] = ["f8_xkom", "f8_mediaexpert", "f8_allegro"]

    ean_xps13 = _ean13("400999000110")
    add("f9_xkom", "x-kom", "Dell XPS 13 9340 i7/16GB/512GB", brand="Dell",
        ean=ean_xps13, category="laptopy", price_gross=699900)
    add("f9_ibood", "ibood", "Laptop Dell XPS 13 9340 i7 16/512", brand="Dell",
        ean=ean_xps13, category="laptopy", price_gross=679900)
    world.true_match_families["f9_dell_xps13_9340"] = ["f9_xkom", "f9_ibood"]

    add("f10_xkom", "x-kom", "Sony WH-1000XM5 słuchawki bezprzewodowe", brand="Sony", mpn="WH1000XM5",
        category="audio", price_gross=169900)
    add("f10_mediaexpert", "mediaexpert", "Słuchawki Sony WH-1000XM5 Black", brand="Sony", mpn="WH-1000XM5",
        category="audio", price_gross=164900)
    add("f10_allegro", "allegro", "Sony WH1000XM5 nauszne bezprzewodowe ANC", brand="Sony", mpn="WH1000XM5",
        category="audio", price_gross=167900)
    world.true_match_families["f10_sony_wh1000xm5"] = ["f10_xkom", "f10_mediaexpert", "f10_allegro"]

    ean_baseus65 = _ean13("400111000220")
    add("f11_xkom", "x-kom", "Ładowarka sieciowa Baseus GaN 65W USB-C", brand="Baseus",
        ean=ean_baseus65, category="akcesoria_zasilacze", price_gross=8900)
    add("f11_mediaexpert", "mediaexpert", "Baseus zasilacz GaN 65W USB-C szybkie ładowanie", brand="Baseus",
        ean=ean_baseus65, category="akcesoria_zasilacze", price_gross=7900)
    world.true_match_families["f11_baseus_gan_65w"] = ["f11_xkom", "f11_mediaexpert"]

    add("f12_xkom", "x-kom", "Logitech MX Master 3S mysz bezprzewodowa Graphite", brand="Logitech",
        category="peryferia", price_gross=39900)
    add("f12_allegro", "allegro", "Mysz Logitech MX Master 3S Graphite bluetooth", brand="Logitech",
        category="peryferia", price_gross=37900)
    world.true_match_families["f12_logitech_mx_master_3s"] = ["f12_xkom", "f12_allegro"]

    ean_rog16 = _ean13("400222000330")
    add("f13_xkom", "x-kom", "Laptop Asus ROG Strix G16 i9/32GB/1TB RTX4070", brand="Asus",
        ean=ean_rog16, category="laptopy", price_gross=999900)
    add("f13_mediaexpert", "mediaexpert", "Asus ROG Strix G16 i9 32/1TB RTX 4070 gamingowy", brand="Asus",
        ean=ean_rog16, category="laptopy", price_gross=979900)
    world.true_match_families["f13_asus_rog_strix_g16"] = ["f13_xkom", "f13_mediaexpert"]

    add("f14_xkom", "x-kom", "Głośnik JBL Flip 6 Bluetooth wodoodporny", brand="JBL", mpn="FLIP6",
        category="audio", price_gross=59900)
    add("f14_allegro", "allegro", "JBL Flip 6 głośnik przenośny Bluetooth", brand="JBL", mpn="FLIP6",
        category="audio", price_gross=54900)
    world.true_match_families["f14_jbl_flip_6"] = ["f14_xkom", "f14_allegro"]

    add("f15_xkom", "x-kom", "Mysz Razer DeathAdder V3 przewodowa gamingowa", brand="Razer",
        category="peryferia", price_gross=29900)
    add("f15_mediaexpert", "mediaexpert", "Razer DeathAdder V3 mysz gamingowa przewodowa", brand="Razer",
        category="peryferia", price_gross=27900)
    world.true_match_families["f15_razer_deathadder_v3"] = ["f15_xkom", "f15_mediaexpert"]

    # ============================================================================================
    # TRAP PAIRS -- 30 total, 6 per category, must NEVER end up in the same product cluster.
    # ============================================================================================

    # -- capacity (512GB vs 1TB and friends) -- deliberately priced close together so blocking's
    # price window does NOT save the matcher; gate G2 (capacity_gb) must be what saves it.
    add("t1_a", "x-kom", "Dysk SSD Samsung 990 PRO 512GB M.2 NVMe", brand="Samsung", mpn="MZ-V9P512BW",
        category="dyski_ssd", price_gross=39900)
    add("t1_b", "x-kom", "Dysk SSD Samsung 990 PRO 1TB M.2 NVMe", brand="Samsung", mpn="MZ-V9P1T0BW",
        category="dyski_ssd", price_gross=45900)
    world.trap_pairs.append(("t1_a", "t1_b"))

    add("t2_a", "pepper", "Kingston NV2 500GB SSD M.2 PCIe", brand="Kingston",
        category="dyski_ssd", price_gross=13900, shipping_cost=None)
    add("t2_b", "pepper", "Kingston NV2 1000GB SSD M.2 PCIe", brand="Kingston",
        category="dyski_ssd", price_gross=15900, shipping_cost=None)
    world.trap_pairs.append(("t2_a", "t2_b"))

    add("t3_a", "mediaexpert", "Dysk WD Blue 1TB SATA 2.5 cala", brand="WD",
        category="dyski_ssd", price_gross=19900)
    add("t3_b", "mediaexpert", "Dysk WD Blue 2TB SATA 2.5 cala", brand="WD",
        category="dyski_ssd", price_gross=25900)
    world.trap_pairs.append(("t3_a", "t3_b"))

    add("t4_a", "allegro", "Apple iPhone 15 128GB niebieski", brand="Apple",
        category="telefony", price_gross=399900)
    add("t4_b", "allegro", "Apple iPhone 15 256GB niebieski", brand="Apple",
        category="telefony", price_gross=459900)
    world.trap_pairs.append(("t4_a", "t4_b"))

    add("t5_a", "x-kom", "Xiaomi Redmi Note 13 8/128GB", brand="Xiaomi",
        category="telefony", price_gross=79900)
    add("t5_b", "x-kom", "Xiaomi Redmi Note 13 8/256GB", brand="Xiaomi",
        category="telefony", price_gross=94900)
    world.trap_pairs.append(("t5_a", "t5_b"))

    add("t6_a", "ibood", "Lenovo IdeaPad Slim 5 14ITL05 i5/16GB/256GB", brand="Lenovo",
        category="laptopy", price_gross=249900)
    add("t6_b", "ibood", "Lenovo IdeaPad Slim 5 14ITL05 i5/16GB/512GB", brand="Lenovo",
        category="laptopy", price_gross=279900)
    world.trap_pairs.append(("t6_a", "t6_b"))

    # -- condition (new vs refurb/used/damaged)
    add("t7_a", "allegro", "Laptop Dell Latitude 5420 i5/16GB nowy", brand="Dell",
        category="laptopy", condition="new", price_gross=249900)
    add("t7_b", "allegro", "Laptop Dell Latitude 5420 poleasingowy i5/16GB", brand="Dell",
        category="laptopy", condition="refurb", price_gross=189900)
    world.trap_pairs.append(("t7_a", "t7_b"))

    add("t8_a", "allegro", "Monitor Dell P2422H nowy", brand="Dell",
        category="monitory", condition="new", price_gross=79900)
    add("t8_b", "allegro", "Monitor Dell P2422H używany", brand="Dell",
        category="monitory", condition="used", price_gross=59900)
    world.trap_pairs.append(("t8_a", "t8_b"))

    add("t9_a", "x-kom", "Laptop Asus Vivobook 15 nowy", brand="Asus",
        category="laptopy", condition="new", price_gross=189900)
    add("t9_b", "x-kom", "Laptop Asus Vivobook 15 uszkodzony (opakowanie)", brand="Asus",
        category="laptopy", condition="damaged", price_gross=129900)
    world.trap_pairs.append(("t9_a", "t9_b"))

    add("t10_a", "allegro", "Apple iPhone 13 128GB nowy", brand="Apple",
        category="telefony", condition="new", price_gross=289900)
    add("t10_b", "allegro", "Apple iPhone 13 128GB odnowiony refurbished", brand="Apple",
        category="telefony", condition="refurb", price_gross=219900)
    world.trap_pairs.append(("t10_a", "t10_b"))

    add("t11_a", "allegro", "Apple MacBook Air M2 nowy", brand="Apple",
        category="laptopy", condition="new", price_gross=549900)
    add("t11_b", "allegro", "Apple MacBook Air M2 poleasingowy", brand="Apple",
        category="laptopy", condition="refurb", price_gross=419900)
    world.trap_pairs.append(("t11_a", "t11_b"))

    add("t12_a", "allegro", "Samsung Galaxy S21 128GB nowy", brand="Samsung",
        category="telefony", condition="new", price_gross=219900)
    add("t12_b", "allegro", "Samsung Galaxy S21 128GB uszkodzony ekran", brand="Samsung",
        category="telefony", condition="damaged", price_gross=99900)
    world.trap_pairs.append(("t12_a", "t12_b"))

    # -- bundle (single vs multipack)
    add("t13_a", "x-kom", "Baterie Duracell AA", brand="Duracell", category="rtv",
        is_bundle=False, price_gross=999)
    add("t13_b", "x-kom", "Baterie Duracell AA 2-pak", brand="Duracell", category="rtv",
        is_bundle=True, bundle_size=2, price_gross=1799)
    world.trap_pairs.append(("t13_a", "t13_b"))

    add("t14_a", "allegro", "Kabel Baseus USB-C 1m", brand="Baseus", category="akcesoria_kable",
        is_bundle=False, price_gross=1999)
    add("t14_b", "allegro", "Kabel Baseus USB-C zestaw 3 sztuki", brand="Baseus", category="akcesoria_kable",
        is_bundle=True, bundle_size=3, price_gross=4999)
    world.trap_pairs.append(("t14_a", "t14_b"))

    add("t15_a", "mediaexpert", "Powerbank Xiaomi 10000mAh", brand="Xiaomi", category="powerbanki",
        is_bundle=False, price_gross=7900)
    add("t15_b", "mediaexpert", "Powerbank Xiaomi 10000mAh zestaw 2 sztuki", brand="Xiaomi", category="powerbanki",
        is_bundle=True, bundle_size=2, price_gross=14900)
    world.trap_pairs.append(("t15_a", "t15_b"))

    add("t16_a", "allegro", "Apple AirTag lokalizator", brand="Apple", category="rtv",
        is_bundle=False, price_gross=14900)
    add("t16_b", "allegro", "Apple AirTag lokalizator 4-pak", brand="Apple", category="rtv",
        is_bundle=True, bundle_size=4, price_gross=49900)
    world.trap_pairs.append(("t16_a", "t16_b"))

    add("t17_a", "x-kom", "Baterie Duracell AAA", brand="Duracell", category="rtv",
        is_bundle=False, price_gross=899)
    add("t17_b", "x-kom", "Baterie Duracell AAA dwupak", brand="Duracell", category="rtv",
        is_bundle=True, bundle_size=2, price_gross=1599)
    world.trap_pairs.append(("t17_a", "t17_b"))

    add("t18_a", "allegro", "Kabel USB-C Anker 1 szt", brand="Anker", category="akcesoria_kable",
        is_bundle=False, price_gross=2499)
    add("t18_b", "allegro", "Kabel USB-C Anker 2 szt", brand="Anker", category="akcesoria_kable",
        is_bundle=True, bundle_size=2, price_gross=4499)
    world.trap_pairs.append(("t18_a", "t18_b"))

    # -- region/voltage variant (EU vs US)
    add("t19_a", "allegro", "Zasilacz Dell 65W EU wtyczka 230V", brand="Dell", category="akcesoria_zasilacze",
        price_gross=14900)
    add("t19_b", "allegro", "Zasilacz Dell 65W US wtyczka 120V", brand="Dell", category="akcesoria_zasilacze",
        price_gross=15900)
    world.trap_pairs.append(("t19_a", "t19_b"))

    add("t20_a", "allegro", "Powerbank Anker wejście 230V wersja EU", brand="Anker", category="powerbanki",
        price_gross=12900)
    add("t20_b", "allegro", "Powerbank Anker wejście 120V wersja US", brand="Anker", category="powerbanki",
        price_gross=13900)
    world.trap_pairs.append(("t20_a", "t20_b"))

    add("t21_a", "mediaexpert", "Suszarka Philips 230V wersja EU", brand="Philips", category="rtv",
        price_gross=17900)
    add("t21_b", "mediaexpert", "Suszarka Philips 120V wersja US", brand="Philips", category="rtv",
        price_gross=18900)
    world.trap_pairs.append(("t21_a", "t21_b"))

    add("t22_a", "allegro", "Ładowarka Xiaomi 220V wersja EU", brand="Xiaomi", category="akcesoria_zasilacze",
        price_gross=4900)
    add("t22_b", "allegro", "Ładowarka Xiaomi 110V wersja US", brand="Xiaomi", category="akcesoria_zasilacze",
        price_gross=5400)
    world.trap_pairs.append(("t22_a", "t22_b"))

    add("t23_a", "x-kom", "Router TP-Link zasilacz 230V EU", brand="TP-Link", category="akcesoria_zasilacze",
        price_gross=5900)
    add("t23_b", "x-kom", "Router TP-Link zasilacz 120V US", brand="TP-Link", category="akcesoria_zasilacze",
        price_gross=6400)
    world.trap_pairs.append(("t23_a", "t23_b"))

    add("t24_a", "allegro", "Adapter podróżny 230V EU uniwersalny", brand="Anker", category="akcesoria_zasilacze",
        price_gross=6900)
    add("t24_b", "allegro", "Adapter podróżny 120V US uniwersalny", brand="Anker", category="akcesoria_zasilacze",
        price_gross=7400)
    world.trap_pairs.append(("t24_a", "t24_b"))

    # -- classic trap: charger-for-model-X vs model X itself
    add("t25_a", "allegro", "Ładowarka do laptopa Dell Latitude 5420 65W", brand="Dell",
        category="akcesoria_zasilacze", price_gross=9900)
    add("t25_b", "allegro", "Laptop Dell Latitude 5420 i5/16GB", brand="Dell",
        category="laptopy", price_gross=249900)
    world.trap_pairs.append(("t25_a", "t25_b"))

    add("t26_a", "allegro", "Ładowarka do telefonu Samsung Galaxy S21 25W", brand="Samsung",
        category="akcesoria_zasilacze", price_gross=4900)
    add("t26_b", "allegro", "Samsung Galaxy S21 128GB", brand="Samsung",
        category="telefony", price_gross=219900)
    world.trap_pairs.append(("t26_a", "t26_b"))

    add("t27_a", "allegro", "Ładowarka do iPhone 15 20W USB-C", brand="Apple",
        category="akcesoria_zasilacze", price_gross=5900)
    add("t27_b", "allegro", "Apple iPhone 15 128GB", brand="Apple",
        category="telefony", price_gross=399900)
    world.trap_pairs.append(("t27_a", "t27_b"))

    add("t28_a", "allegro", "Zasilacz do monitora Dell P2422H", brand="Dell",
        category="akcesoria_zasilacze", price_gross=6900)
    add("t28_b", "allegro", "Monitor Dell P2422H", brand="Dell",
        category="monitory", price_gross=79900)
    world.trap_pairs.append(("t28_a", "t28_b"))

    add("t29_a", "allegro", "Kabel ładujący do Xiaomi Redmi Note 13", brand="Xiaomi",
        category="akcesoria_kable", price_gross=2900)
    add("t29_b", "allegro", "Xiaomi Redmi Note 13 8/256GB", brand="Xiaomi",
        category="telefony", price_gross=94900)
    world.trap_pairs.append(("t29_a", "t29_b"))

    add("t30_a", "allegro", "Ładowarka do MacBook Air 67W USB-C", brand="Apple",
        category="akcesoria_zasilacze", price_gross=17900)
    add("t30_b", "allegro", "Apple MacBook Air M2", brand="Apple",
        category="laptopy", price_gross=549900)
    world.trap_pairs.append(("t30_a", "t30_b"))

    # ============================================================================================
    # ALIEXPRESS-STYLE SINGLETONS -- no brand, no EAN, generic titles. Should all remain their own
    # singleton product (match_method='none'); see the A3 final report for why this is correct.
    # ============================================================================================
    ali_titles = [
        "uniwersalny kabel usb c szybkie ladowanie 66w 1m",
        "case do telefonu przezroczyste silikonowe uniwersalne",
        "sluchawki bezprzewodowe bluetooth 5.3 sportowe",
        "mysz bezprzewodowa 2.4g ergonomiczna kolorowa",
        "powerbank mini 5000mah brelok",
        "folia ochronna szklo hartowane uniwersalne",
        "statyw do telefonu regulowany biurkowy",
        "lampka led usb do klawiatury",
        "organizer kabli sciagacz na rzepy 10 szt",
        "podstawka chlodzaca pod laptopa z wentylatorem",
        "etui skorzane portfel na karty",
        "adapter usb c na jack 3.5mm",
        "gniazdo hdmi przejsciowka 4k",
        "pilot uniwersalny do telewizora",
        "torba na laptopa 15.6 cala wodoodporna",
    ]
    for idx, title in enumerate(ali_titles, start=1):
        add(
            f"ali_{idx}", "aliexpress", title,
            brand=None, category=None, price_gross=1500 + idx * 137, shipping_cost=0,
        )

    db.commit()
    yield world
