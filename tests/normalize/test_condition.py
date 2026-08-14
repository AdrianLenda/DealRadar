"""Tests for dealradar.normalize.condition -- condition and bundle detection on >= 15 real Allegro-style titles.

This is the direct defense against failure mode F2 (ARCHITEKTURA.md section 2): comparing a poleasing unit
to a new one, or a single item to a multi-pack, silently poisons the median every downstream deal_score
depends on.
"""

from __future__ import annotations

import pytest

from dealradar.normalize.condition import detect_bundle, resolve_condition


# --------------------------------------------------------------------------------------------------
# Condition -- >= 15 real Allegro-style titles.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Laptop Dell Latitude 5420 poleasingowy i5/16GB/512GB", "refurb"),
        ("Monitor Dell P2422H POWYSTAWOWY, pełna sprawność", "refurb"),
        ("iPhone 12 128GB Refurbished by Apple", "refurb"),
        ("Aparat Canon EOS 90D odnowiony, gwarancja 12m", "refurb"),
        ("Laptop Lenovo ThinkPad T14 OUTLET, drobne rysy na obudowie", "refurb"),
        ("Monitor Dell P2422H używany, ślady eksploatacji", "used"),
        ("iPhone 11 64GB uzywany stan bardzo dobry", "used"),
        ("Słuchawki Sony WH-1000XM4 open box, pełny zestaw", "used"),
        ("Laptop Asus Vivobook (opakowanie uszkodzone)", "damaged"),
        ("Monitor LG 27\" uszkodzona podstawa, ekran sprawny", "damaged"),
        ("Telefon Samsung Galaxy S21 uszkodzony wyświetlacz", "damaged"),
        ("Dysk SSD Kingston NV2 1TB M.2 NVMe", None),  # no phrase; caller default decides
        ("Powerbank Anker PowerCore 20000 mAh NOWY FV23", None),  # "NOWY" alone is not treated as a condition phrase
        ("Smartfon Xiaomi Redmi Note 13 8/256GB", None),
        ("Laptop HP Pavilion 15 i5/16GB/512GB SSD", None),
    ],
)
def test_detect_condition_phrase_on_allegro_titles(title: str, expected: str | None) -> None:
    from dealradar.normalize.condition import detect_condition_phrase

    assert detect_condition_phrase(title) == expected


def test_resolve_condition_defaults_to_new_for_first_party_feed_when_undeclared() -> None:
    """A retail feed (assume_new_if_undeclared=True) with no condition phrase means new stock."""
    assert resolve_condition("Dysk SSD Kingston NV2 1TB M.2 NVMe", assume_new_if_undeclared=True) == "new"


def test_resolve_condition_stays_none_on_marketplace_when_undeclared() -> None:
    """Allegro (assume_new_if_undeclared=False): an unstated condition is genuinely unknown, never 'new' (A2 spec)."""
    assert resolve_condition("Smartfon Xiaomi Redmi Note 13 8/256GB", assume_new_if_undeclared=False) is None


def test_resolve_condition_explicit_phrase_wins_regardless_of_source_policy() -> None:
    assert resolve_condition("Laptop Dell Latitude 5420 poleasingowy", assume_new_if_undeclared=True) == "refurb"
    assert resolve_condition("Laptop Dell Latitude 5420 poleasingowy", assume_new_if_undeclared=False) == "refurb"


# --------------------------------------------------------------------------------------------------
# Bundles.
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected_is_bundle,expected_size",
    [
        ("Baterie Duracell AA 2-pak", True, 2),
        ("Baterie Duracell AA, dwupak", True, 2),
        ("Powerbank Anker x2, zestaw promocyjny", True, 2),
        ("Słuchawki douszne, 3 szt. w zestawie", True, 3),
        ("Zestaw słuchawkowy bezprzewodowy XYZ", True, None),  # "zestaw" alone, no parseable count
        ("Kabel USB-C, komplet akcesoriów w zestawie", True, None),
        ("Dysk SSD Samsung 990 PRO 2TB M.2 NVMe", False, None),
        ("Monitor LG UltraGear 27GP850 27\" 165Hz", False, None),
        ("Laptop Dell Latitude 5420 poleasingowy i5/16GB", False, None),
    ],
)
def test_detect_bundle_on_allegro_titles(title: str, expected_is_bundle: bool, expected_size: int | None) -> None:
    is_bundle, bundle_size = detect_bundle(title)
    assert is_bundle == expected_is_bundle
    assert bundle_size == expected_size
