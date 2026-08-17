"""Declarative per-source field mapping: config/normalizers/<source>.yaml -> canonical raw-field extraction.

This is what makes "add a new affiliate-feed shop" a config change instead of a code change (A2 task spec,
section "Kontrakt"): each source config lists, per canonical field, an ordered list of candidate JSON keys
to look for in `RawOffer.payload_json` (because every affiliate network names its EAN field something
different: `ean`/`gtin`/`barcode`). The first candidate key present with a non-empty value wins. A source
with no dedicated config file falls back to `_default_<kind>.yaml` (one generic mapping per collector kind:
api/feed/rss) so an unmapped new source still gets best-effort field extraction instead of nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from dealradar.errors import ConfigError

CANONICAL_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "url",
    "ean",
    "price_gross",
    "shipping_cost",
    "currency",
    "list_price_claimed",
    "omnibus_min_30d",
    "brand",
    "mpn",
    "category_raw",
    "seller",
    "seller_rating",
    "condition_text",
    "rating_avg",
    "rating_count",
    "warranty_months",
    "in_stock",
)


class NormalizerConfig:
    """One parsed config/normalizers/*.yaml: candidate source-field names per canonical field, plus source policy."""

    def __init__(
        self,
        *,
        fields: dict[str, list[str]],
        assume_new_if_undeclared: bool = True,
        currency_default: str = "PLN",
    ) -> None:
        """Stores the field-candidate map and the two per-source policy flags declared alongside it."""
        self.fields = fields
        self.assume_new_if_undeclared = assume_new_if_undeclared
        self.currency_default = currency_default

    def extract_raw_fields(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Returns {canonical_field: value} for every canonical field found in `payload` via this config's candidates.

        A field is considered "found" when the first matching candidate key is present with a value that
        is not None and not an empty/whitespace-only string. Later candidates for the same field are only
        tried when earlier ones are absent -- this is the whole "every network names it differently" fix.
        """
        result: dict[str, Any] = {}
        for canonical, candidates in self.fields.items():
            for candidate in candidates:
                if candidate not in payload:
                    continue
                value = payload[candidate]
                if value is None:
                    continue
                if isinstance(value, str) and not value.strip():
                    continue
                result[canonical] = value
                break
        return result


def _parse_config(data: dict[str, Any], *, source_path: Path) -> NormalizerConfig:
    """Validates and builds one NormalizerConfig from a parsed YAML document; raises ConfigError on a bad shape."""
    fields_raw = data.get("fields", {})
    if not isinstance(fields_raw, dict):
        raise ConfigError(f"{source_path}: 'fields' must be a mapping of canonical field -> list of source keys")
    fields: dict[str, list[str]] = {}
    for canonical, candidates in fields_raw.items():
        if canonical not in CANONICAL_FIELDS:
            raise ConfigError(f"{source_path}: unknown canonical field {canonical!r}")
        if not isinstance(candidates, list) or not all(isinstance(c, str) for c in candidates):
            raise ConfigError(f"{source_path}: candidates for {canonical!r} must be a list of strings")
        fields[canonical] = candidates
    return NormalizerConfig(
        fields=fields,
        assume_new_if_undeclared=bool(data.get("assume_new_if_undeclared", True)),
        currency_default=str(data.get("currency_default", "PLN")),
    )


class NormalizerConfigRegistry:
    """Every loaded config/normalizers/*.yaml, resolvable by exact source name or by collector kind fallback."""

    def __init__(self, by_source: dict[str, NormalizerConfig], by_kind_default: dict[str, NormalizerConfig]) -> None:
        """Stores the exact-source-name configs and the per-kind (`api`/`feed`/`rss`) fallback configs."""
        self._by_source = by_source
        self._by_kind_default = by_kind_default

    def resolve(self, source_name: str, kind: str) -> NormalizerConfig:
        """Returns the best-matching NormalizerConfig for `source_name`: exact match, else its `family_`
        prefix, else the kind's default.

        The family step exists because one site can publish several channels that are separate sources but
        share a payload shape -- `pepper`, `pepper_wszystkie` and `pepper_elektronika` are all Pepper RSS,
        so `pepper_elektronika` inherits `pepper.yaml` instead of needing a duplicate file (and, worse,
        silently falling through to the generic RSS default, which does not know where Pepper hides its
        price).

        Falls back to a minimal built-in config (best-effort, guesses the handful of most common field
        names) when nothing else matches, so a source A1 adds before A2 writes its YAML still gets
        *something* rather than a crash (Z7: one missing config must not stop the run).
        """
        if source_name in self._by_source:
            return self._by_source[source_name]
        family = source_family(source_name)
        if family != source_name and family in self._by_source:
            return self._by_source[family]
        if kind in self._by_kind_default:
            return self._by_kind_default[kind]
        return _BUILTIN_FALLBACK


def source_family(source_name: str) -> str:
    """Returns the part of a source name before its first underscore -- the site a channel belongs to.

    `pepper_elektronika` -> `pepper`; `allegro` -> `allegro`. Shared by the normalizer-config and hook
    lookups so both inherit per-site behaviour identically.
    """
    return source_name.split("_", 1)[0]


_BUILTIN_FALLBACK = NormalizerConfig(
    fields={
        "title": ["title", "name"],
        "url": ["url", "link"],
        "ean": ["ean", "gtin"],
        "price_gross": ["price", "price_gross"],
        "currency": ["currency"],
        "brand": ["brand"],
        "category_raw": ["category"],
    },
    assume_new_if_undeclared=True,
)


def load_normalizer_registry(config_dir: Path) -> NormalizerConfigRegistry:
    """Loads every YAML file under config_dir into a NormalizerConfigRegistry; missing dir yields an empty registry.

    Files named `_default_<kind>.yaml` register as the fallback for that collector kind; every other file's
    stem is treated as an exact source name (matching `source.name` in config/sources.yaml).
    """
    by_source: dict[str, NormalizerConfig] = {}
    by_kind_default: dict[str, NormalizerConfig] = {}
    if not config_dir.exists():
        return NormalizerConfigRegistry(by_source, by_kind_default)

    for path in sorted(config_dir.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"expected a YAML mapping at the top level of {path}")
        cfg = _parse_config(data, source_path=path)
        stem = path.stem
        if stem.startswith("_default_"):
            by_kind_default[stem[len("_default_") :]] = cfg
        else:
            by_source[stem] = cfg
    return NormalizerConfigRegistry(by_source, by_kind_default)
