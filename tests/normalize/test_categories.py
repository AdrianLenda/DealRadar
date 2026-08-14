"""Tests for dealradar.normalize.categories -- category tree seeding, source mapping, and the unmapped report.

Uses the real config/category_map.yaml (A2-owned) against a fresh in-memory schema, so this also acts as a
validation test for that config file's own shape.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.normalize.categories import load_category_map

CATEGORY_MAP_PATH = Path(__file__).resolve().parents[2] / "config" / "category_map.yaml"


def test_ensure_categories_creates_tree_and_is_idempotent(normalize_db: Session) -> None:
    category_map = load_category_map(CATEGORY_MAP_PATH)
    first = category_map.ensure_categories(normalize_db)
    assert "elektronika" in first
    assert "elektronika/dyski_ssd" in first
    assert isinstance(first["elektronika/dyski_ssd"], int)

    # Idempotent: running it again on the same session must not create duplicate rows.
    second = category_map.ensure_categories(normalize_db)
    assert first == second
    count = normalize_db.execute(text("SELECT COUNT(*) FROM category")).scalar_one()
    assert count == len(first)


def test_child_category_has_correct_parent(normalize_db: Session) -> None:
    category_map = load_category_map(CATEGORY_MAP_PATH)
    ids = category_map.ensure_categories(normalize_db)
    parent_id = normalize_db.execute(
        text("SELECT parent_id FROM category WHERE id = :id"), {"id": ids["elektronika/dyski_ssd"]}
    ).scalar_one()
    assert parent_id == ids["elektronika"]


def test_resolve_source_specific_mapping(normalize_db: Session) -> None:
    category_map = load_category_map(CATEGORY_MAP_PATH)
    ids = category_map.ensure_categories(normalize_db)
    resolved = category_map.resolve("x-kom", "Dyski SSD")
    assert resolved == ids["elektronika/dyski_ssd"]


def test_resolve_is_case_and_whitespace_insensitive(normalize_db: Session) -> None:
    category_map = load_category_map(CATEGORY_MAP_PATH)
    ids = category_map.ensure_categories(normalize_db)
    assert category_map.resolve("x-kom", "  DYSKI ssd  ") == ids["elektronika/dyski_ssd"]


def test_resolve_falls_back_to_default_bucket(normalize_db: Session) -> None:
    category_map = load_category_map(CATEGORY_MAP_PATH)
    ids = category_map.ensure_categories(normalize_db)
    # "ssd" (generic) is only in the "default" bucket, not under a source that doesn't declare it explicitly.
    assert category_map.resolve("some-new-shop-not-in-config", "SSD") == ids["elektronika/dyski_ssd"]


def test_resolve_unmapped_category_returns_none_and_is_recorded(normalize_db: Session) -> None:
    category_map = load_category_map(CATEGORY_MAP_PATH)
    category_map.ensure_categories(normalize_db)
    assert category_map.resolve("x-kom", "Ogród i taras") is None
    assert category_map.resolve("x-kom", "Ogród i taras") is None  # seen twice -> count 2

    report = category_map.unmapped_report()
    assert ("x-kom", "Ogród i taras", 2) in report


def test_resolve_none_or_empty_category_raw_returns_none_without_recording(normalize_db: Session) -> None:
    category_map = load_category_map(CATEGORY_MAP_PATH)
    category_map.ensure_categories(normalize_db)
    assert category_map.resolve("x-kom", None) is None
    assert category_map.resolve("x-kom", "") is None
    assert category_map.unmapped_report() == []


def test_unmapped_report_sorted_by_count_descending(normalize_db: Session) -> None:
    category_map = load_category_map(CATEGORY_MAP_PATH)
    category_map.ensure_categories(normalize_db)
    for _ in range(3):
        category_map.resolve("x-kom", "Ogród")
    for _ in range(1):
        category_map.resolve("x-kom", "Zabawki")
    report = category_map.unmapped_report()
    counts = [count for _, _, count in report]
    assert counts == sorted(counts, reverse=True)
