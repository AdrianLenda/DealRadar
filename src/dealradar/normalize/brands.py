"""Brand alias resolution against config/brands.yaml (A2 task spec, section 3).

Two jobs: (1) fold a source-declared brand string ("SAMSUNG", "Samsung Electronics") down to its canonical
lowercase form, and (2) when a source leaves brand empty, try to find a known brand name in the title --
but only on a word boundary, so "Xiaomi" inside some unrelated compound word never matches, and only against
brands we actually know, so an unrecognized brand is left None rather than guessed (Z4).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from dealradar.errors import ConfigError


class BrandIndex:
    """A loaded config/brands.yaml: alias (any case) -> canonical brand, plus word-boundary title matching."""

    def __init__(self, aliases: dict[str, list[str]]) -> None:
        """Builds the alias->canonical lookup and one compiled regex per canonical brand for title matching."""
        self._alias_to_canonical: dict[str, str] = {}
        self._canonical_display: dict[str, str] = {}
        self._title_patterns: list[tuple[str, re.Pattern[str]]] = []

        for canonical, alias_list in aliases.items():
            canonical_norm = canonical.strip().lower()
            all_forms = [canonical, *alias_list]
            self._canonical_display[canonical_norm] = canonical
            for form in all_forms:
                self._alias_to_canonical[form.strip().lower()] = canonical_norm
            # Longest alias first so e.g. "Samsung Electronics" is tried before the bare "Samsung" would
            # otherwise shadow it inside the same scan of a title.
            escaped = sorted({re.escape(f.strip()) for f in all_forms if f.strip()}, key=len, reverse=True)
            if escaped:
                pattern = re.compile(r"(?i)\b(" + "|".join(escaped) + r")\b")
                self._title_patterns.append((canonical_norm, pattern))

    def resolve(self, declared: str) -> tuple[str, str] | None:
        """Looks up a source-declared brand string; returns (canonical_display, canonical_norm) or None if unknown."""
        canonical_norm = self._alias_to_canonical.get(declared.strip().lower())
        if canonical_norm is None:
            return None
        return self._canonical_display[canonical_norm], canonical_norm

    def find_in_title(self, title: str) -> tuple[str, str] | None:
        """Scans `title` for a known brand on a word boundary; returns (canonical_display, canonical_norm) or None.

        Tries every known brand and requires an unambiguous single match: if two *different* canonical
        brands both appear in the title (e.g. a "compatible with X" accessory listing), this returns None
        rather than guess which one is the product's own brand -- guessing wrong here corrupts brand_norm,
        which the matcher (A3) blocks on.
        """
        found: set[str] = set()
        for canonical_norm, pattern in self._title_patterns:
            if pattern.search(title):
                found.add(canonical_norm)
        if len(found) != 1:
            return None
        canonical_norm = next(iter(found))
        return self._canonical_display[canonical_norm], canonical_norm


def load_brand_index(path: Path) -> BrandIndex:
    """Loads and parses config/brands.yaml into a BrandIndex; raises ConfigError if the file is missing/malformed."""
    if not path.exists():
        raise ConfigError(f"missing brand alias file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict) or not isinstance(data.get("aliases", {}), dict):
        raise ConfigError(f"expected a top-level 'aliases' mapping in {path}")
    aliases: dict[str, list[str]] = data.get("aliases", {})
    return BrandIndex(aliases)
