"""The matcher's internal view of one `offer` row, plus its extracted numeric attributes. Owner: A3.

Deliberately not `dealradar.models.Offer`: the matcher only needs a small, flat subset of offer
columns (task spec: "masz tam ean, brand_norm, mpn_norm, title_norm, category_id, condition,
is_bundle, price_total"), plus the numeric attributes gate G2 needs -- sourced from A2's
`offer.evidence["attrs"]` where present, filled in by this package's own title_norm regex
extraction otherwise (see attrs.py's module docstring for the full A2/A3 integration note). Keeping
this as a light dataclass -- rather than reusing/extending Offer -- avoids coupling A3's internals
to A0's shared contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from dealradar.matching.attrs import attrs_for_offer


@dataclass(frozen=True)
class OfferRecord:
    """The fields of one `offer` row the matcher reads, plus attrs extracted from title_norm for gate G2."""

    id: int
    source_id: int
    title: str
    title_norm: str | None
    brand: str | None
    brand_norm: str | None
    mpn: str | None
    mpn_norm: str | None
    ean: str | None
    category_id: int | None
    condition: str | None
    is_bundle: bool
    bundle_size: int | None
    price_total: int
    product_id: int | None
    match_confidence: float | None
    match_method: str | None
    attrs: dict[str, float] = field(default_factory=dict)

    @staticmethod
    def from_row(row: dict[str, Any]) -> "OfferRecord":
        """Builds an OfferRecord from a raw `offer` table row mapping, resolving attrs from evidence + title_norm.

        `row` comes from `sqlalchemy.engine.Row.mappings()`, which mypy --strict cannot type more
        precisely than `Any` per column (see dealradar.db._row_to_offer for the same pattern) --
        every value is explicitly coerced to its real type here rather than trusted. `evidence` is
        stored as a JSON text column (see dealradar.db.upsert_offer); a missing/unparseable value
        is treated as "no evidence" rather than raising, since evidence is optional per Offer.
        """
        title_norm_val = row["title_norm"]
        title_norm = str(title_norm_val) if title_norm_val is not None else None
        evidence_raw = row.get("evidence")
        evidence: dict[str, Any] = {}
        if isinstance(evidence_raw, str) and evidence_raw:
            try:
                parsed = json.loads(evidence_raw)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                evidence = parsed
        elif isinstance(evidence_raw, dict):
            evidence = evidence_raw
        return OfferRecord(
            id=int(row["id"]),
            source_id=int(row["source_id"]),
            title=str(row["title"]),
            title_norm=title_norm,
            brand=None if row["brand"] is None else str(row["brand"]),
            brand_norm=None if row["brand_norm"] is None else str(row["brand_norm"]),
            mpn=None if row["mpn"] is None else str(row["mpn"]),
            mpn_norm=None if row["mpn_norm"] is None else str(row["mpn_norm"]),
            ean=None if row["ean"] is None else str(row["ean"]),
            category_id=None if row["category_id"] is None else int(row["category_id"]),
            condition=None if row["condition"] is None else str(row["condition"]),
            is_bundle=bool(row["is_bundle"]),
            bundle_size=None if row["bundle_size"] is None else int(row["bundle_size"]),
            price_total=int(row["price_total"]),
            product_id=None if row["product_id"] is None else int(row["product_id"]),
            match_confidence=None if row["match_confidence"] is None else float(row["match_confidence"]),
            match_method=None if row["match_method"] is None else str(row["match_method"]),
            attrs=attrs_for_offer(title_norm, evidence),
        )
