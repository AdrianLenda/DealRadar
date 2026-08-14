"""Category tree + source -> category mapping, backed by config/category_map.yaml (A2 task spec, section 5).

Two deliberately separate things live in one YAML file: (1) DealRadar's own small category tree (inserted
into the `category` table A0 already defined), and (2) a manual, per-source lookup from whatever string a
source calls its category to one of our own leaf categories. Only electronics are mapped at launch, per the
task spec -- everything else gets `category_id = None` and keeps flowing through the pipeline; that is
correct behavior (Z4), not a gap. Unmapped source categories are counted so the next mapping session has a
ready-made, frequency-sorted to-do list instead of a blind guess.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.errors import ConfigError


def _norm_key(value: str) -> str:
    """Returns value lowercased, stripped, and whitespace-collapsed, for stable use as a category lookup key."""
    return " ".join(value.strip().lower().split())


class CategoryMap:
    """Loaded config/category_map.yaml: DealRadar's own category tree plus the (source, raw) -> path mapping."""

    def __init__(self, tree: list[dict[str, Any]], mappings: dict[str, dict[str, str]]) -> None:
        """Stores the raw tree/mapping config; call `ensure_categories` once per session before `resolve`."""
        self._tree = tree
        # mappings: {source_name_or_"default": {normalized_raw_category: category_path}}
        self._mappings = {
            source: {_norm_key(raw): path for raw, path in per_source.items()} for source, per_source in mappings.items()
        }
        self._path_to_id: dict[str, int] = {}
        self._unmapped: Counter[tuple[str, str]] = Counter()

    def ensure_categories(self, session: Session) -> dict[str, int]:
        """Idempotently upserts every category in the configured tree (by unique `path`); returns path -> id.

        The YAML tree must list parents before their children (enforced by raising if a parent path is
        referenced before it has been inserted) -- this keeps the insert order a single deterministic pass
        with no recursion.
        """
        for node in self._tree:
            path, name = node["path"], node["name"]
            parent_path = node.get("parent")
            parent_id = None
            if parent_path is not None:
                parent_id = self._path_to_id.get(parent_path)
                if parent_id is None:
                    raise ConfigError(
                        f"config/category_map.yaml: category {path!r} references parent {parent_path!r} "
                        "before it is defined -- list parents before their children"
                    )
            existing = session.execute(text("SELECT id FROM category WHERE path = :path"), {"path": path}).scalar_one_or_none()
            if existing is not None:
                self._path_to_id[path] = int(existing)
                continue
            result = session.execute(
                text("INSERT INTO category (parent_id, name, path) VALUES (:parent_id, :name, :path) RETURNING id"),
                {"parent_id": parent_id, "name": name, "path": path},
            )
            self._path_to_id[path] = int(result.scalar_one())
        return dict(self._path_to_id)

    def resolve(self, source_name: str, category_raw: str | None) -> int | None:
        """Returns the category_id mapped for (source_name, category_raw), or None when nothing matches (Z4).

        Falls back to the "default" mapping bucket (generic labels shared across sources) when no
        source-specific entry exists. Records every miss so `unmapped_report` can list them by frequency.
        """
        if not category_raw or not category_raw.strip():
            return None
        key = _norm_key(category_raw)
        for bucket in (source_name, "default"):
            path = self._mappings.get(bucket, {}).get(key)
            if path is not None:
                category_id = self._path_to_id.get(path)
                if category_id is not None:
                    return category_id
        self._unmapped[(source_name, category_raw)] += 1
        return None

    def unmapped_report(self, limit: int = 50) -> list[tuple[str, str, int]]:
        """Returns (source_name, category_raw, count) triples for every unmapped category seen, sorted by count desc."""
        return [(source, raw, count) for (source, raw), count in self._unmapped.most_common(limit)]


def load_category_map(path: Path) -> CategoryMap:
    """Loads and parses config/category_map.yaml into a CategoryMap; raises ConfigError if missing/malformed."""
    if not path.exists():
        raise ConfigError(f"missing category map file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"expected a YAML mapping at the top level of {path}")
    tree = data.get("categories", [])
    mappings = data.get("mappings", {})
    if not isinstance(tree, list) or not isinstance(mappings, dict):
        raise ConfigError(f"{path}: expected 'categories' to be a list and 'mappings' to be a mapping")
    return CategoryMap(tree, mappings)
