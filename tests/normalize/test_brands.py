"""Tests for dealradar.normalize.brands -- alias resolution and word-boundary title matching."""

from __future__ import annotations

from pathlib import Path

import pytest

from dealradar.normalize.brands import load_brand_index

CONFIG_BRANDS_PATH = Path(__file__).resolve().parents[2] / "config" / "brands.yaml"


@pytest.fixture()
def brand_index():  # type: ignore[no-untyped-def]
    return load_brand_index(CONFIG_BRANDS_PATH)


@pytest.mark.parametrize(
    "declared,expected_norm",
    [
        ("SAMSUNG", "samsung"),
        ("Samsung Electronics", "samsung"),
        ("samsung", "samsung"),
        ("Xiaomi", "xiaomi"),
        ("Mi", "xiaomi"),
        ("LG Electronics", "lg"),
        ("Hewlett-Packard", "hp"),
    ],
)
def test_resolve_known_alias(brand_index, declared: str, expected_norm: str) -> None:  # type: ignore[no-untyped-def]
    resolved = brand_index.resolve(declared)
    assert resolved is not None
    _, norm = resolved
    assert norm == expected_norm


def test_resolve_unknown_brand_returns_none(brand_index) -> None:  # type: ignore[no-untyped-def]
    assert brand_index.resolve("TotallyUnknownBrandXYZ") is None


@pytest.mark.parametrize(
    "title,expected_norm",
    [
        ("Dysk SSD Samsung 990 PRO 2TB M.2 NVMe", "samsung"),
        ("Powerbank Anker PowerCore 20000mAh", "anker"),
        ("Smartfon Xiaomi Redmi Note 13 8/256GB", "xiaomi"),
    ],
)
def test_find_in_title_matches_known_brand(brand_index, title: str, expected_norm: str) -> None:  # type: ignore[no-untyped-def]
    found = brand_index.find_in_title(title)
    assert found is not None
    _, norm = found
    assert norm == expected_norm


def test_find_in_title_respects_word_boundary() -> None:
    """"Xiaomi" must not match inside an unrelated longer word (A2 task spec, section 3)."""
    index = load_brand_index(CONFIG_BRANDS_PATH)
    # A fabricated word that *contains* "Xiaomi" as a substring but is not the brand name on its own.
    assert index.find_in_title("Ładowarka kompatybilna zPseudoXiaomiAccessoryXYZ") is None


def test_find_in_title_with_no_known_brand_returns_none(brand_index) -> None:  # type: ignore[no-untyped-def]
    assert brand_index.find_in_title("Uniwersalny kabel USB-C do wszystkich telefonów") is None


def test_find_in_title_two_different_brands_is_ambiguous_returns_none(brand_index) -> None:  # type: ignore[no-untyped-def]
    """Two different known brands both appearing (e.g. a 'works with X' accessory) must not guess either one."""
    assert brand_index.find_in_title("Etui Samsung kompatybilne z Apple AirPods") is None
