"""The mandatory regression suite: ~30 pairs that MUST stay separated (task spec 'Testy regresji').

Each pair is its own parametrized test case (rather than one aggregate assertion in test_service.py)
so a regression shows up as "test_trap_pair[t14_a-t14_b] FAILED", not a buried entry in a list --
exactly the kind of failure a real F2 (bad merge) would produce. The 30 pairs cover the five trap
categories the task spec names explicitly: capacity (512GB vs 1TB and friends), condition (new vs
refurb/used/damaged), bundle (single vs multipack), region/voltage (EU vs US), and the charger-for-
model-X-vs-model-X-itself classic.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text

from dealradar.matching.service import match_offers

TRAP_PAIRS: list[tuple[str, str]] = [
    ("t1_a", "t1_b"),  # Samsung 990 PRO 512GB vs 1TB
    ("t2_a", "t2_b"),  # Kingston NV2 500GB vs 1000GB
    ("t3_a", "t3_b"),  # WD Blue 1TB vs 2TB
    ("t4_a", "t4_b"),  # iPhone 15 128GB vs 256GB
    ("t5_a", "t5_b"),  # Redmi Note 13 128GB vs 256GB
    ("t6_a", "t6_b"),  # Lenovo IdeaPad Slim 5 256GB vs 512GB
    ("t7_a", "t7_b"),  # Dell Latitude 5420 new vs refurb (poleasingowy)
    ("t8_a", "t8_b"),  # Dell P2422H new vs used
    ("t9_a", "t9_b"),  # Asus Vivobook new vs damaged
    ("t10_a", "t10_b"),  # iPhone 13 new vs refurb
    ("t11_a", "t11_b"),  # MacBook Air M2 new vs refurb
    ("t12_a", "t12_b"),  # Galaxy S21 new vs damaged
    ("t13_a", "t13_b"),  # Duracell AA single vs 2-pak
    ("t14_a", "t14_b"),  # Baseus cable single vs 3-pak
    ("t15_a", "t15_b"),  # Xiaomi powerbank single vs 2-pak
    ("t16_a", "t16_b"),  # AirTag single vs 4-pak
    ("t17_a", "t17_b"),  # Duracell AAA single vs dwupak
    ("t18_a", "t18_b"),  # Anker cable 1szt vs 2szt
    ("t19_a", "t19_b"),  # Dell 65W charger EU 230V vs US 120V
    ("t20_a", "t20_b"),  # Anker powerbank EU 230V vs US 120V
    ("t21_a", "t21_b"),  # Philips hairdryer EU 230V vs US 120V
    ("t22_a", "t22_b"),  # Xiaomi charger EU 220V vs US 110V
    ("t23_a", "t23_b"),  # TP-Link PSU EU 230V vs US 120V
    ("t24_a", "t24_b"),  # Travel adapter EU 230V vs US 120V
    ("t25_a", "t25_b"),  # charger-for-Dell-Latitude-5420 vs the Latitude 5420 itself
    ("t26_a", "t26_b"),  # charger-for-Galaxy-S21 vs the S21 itself
    ("t27_a", "t27_b"),  # charger-for-iPhone-15 vs the iPhone 15 itself
    ("t28_a", "t28_b"),  # PSU-for-Dell-P2422H vs the monitor itself
    ("t29_a", "t29_b"),  # cable-for-Redmi-Note-13 vs the phone itself
    ("t30_a", "t30_b"),  # charger-for-MacBook-Air vs the MacBook itself
]


def test_trap_pairs_cover_at_least_30() -> None:
    assert len(TRAP_PAIRS) >= 30
    assert len(set(TRAP_PAIRS)) == len(TRAP_PAIRS)  # no accidental duplicates


@pytest.mark.parametrize("key_a,key_b", TRAP_PAIRS)
def test_trap_pair_stays_separated(matching_world: Any, key_a: str, key_b: str) -> None:
    match_offers(matching_world.session, full=True)
    matching_world.session.commit()

    offer_id_a = matching_world.offer_id[key_a]
    offer_id_b = matching_world.offer_id[key_b]
    row_a = matching_world.session.execute(text("SELECT product_id FROM offer WHERE id = :id"), {"id": offer_id_a}).first()
    row_b = matching_world.session.execute(text("SELECT product_id FROM offer WHERE id = :id"), {"id": offer_id_b}).first()
    assert row_a is not None and row_b is not None
    product_a, product_b = row_a[0], row_b[0]
    assert product_a is not None and product_b is not None, "every offer should still get a (singleton) product"
    assert product_a != product_b, f"{key_a} and {key_b} were incorrectly merged into product {product_a}"
