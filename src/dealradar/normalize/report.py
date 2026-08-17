"""Report and rejection contracts for etap 2 (normalize). Owner: A2.

`models.py` (A0) does not define `RejectedOffer` or a normalize-specific report shape, so both live here.
`RejectedOffer` is deliberately a first-class model, not an exception: a raw_offer that could not become a
canonical `Offer` is written to a real table (see `store.py`) so the question "why did 40k records become
31k" has a queryable answer, per the A2 task spec ("ladduje w tabeli, nie w koszu").
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from dealradar.models import DealRadarModel

RejectReason = Literal[
    "missing_title",
    "missing_url",
    "missing_or_unparseable_price",
    "fx_rate_unavailable",
    "unknown_source",
    "offer_validation_failed",
    "not_a_product_offer",
]
"""Every reason normalize refuses to turn a raw_offer into an Offer. Keep this list exhaustive — the whole
point of RejectedOffer is that every drop is explained, never silently absorbed into a generic 'other'."""


class RejectedOffer(DealRadarModel):
    """A raw_offer that normalize could not turn into a canonical Offer, kept for auditability (never discarded)."""

    id: int | None = None
    raw_offer_id: int
    source_id: int
    external_id: str
    reason: RejectReason
    detail: str
    """Human-readable specifics, e.g. the unparseable price string or the validation error text."""
    rejected_at: datetime


class SourceCoverage(DealRadarModel):
    """Per-source counters for one normalize run — the raw material behind the four decisive coverage percentages."""

    source_name: str
    accepted: int = 0
    rejected: int = 0
    with_ean: int = 0
    with_brand: int = 0
    with_category: int = 0
    with_any_attr: int = 0

    def pct(self, field: str) -> float | None:
        """Returns 100 * getattr(self, field) / accepted, or None when no offers were accepted for this source."""
        if self.accepted == 0:
            return None
        count = int(getattr(self, field))
        return 100.0 * count / self.accepted


class NormalizeReport(DealRadarModel):
    """Outcome of one normalize_batch run: counts, rejection breakdown by reason, and coverage percentages (Z7).

    The four pct_with_* fields are computed over *accepted* offers only (the ones that reached the `offer`
    table) — they answer "of what the matcher (A3) will actually see, how much has an EAN/brand/category/
    attribute", which is the question ARCHITEKTURA.md's `Raport końcowy` section asks for.
    """

    processed: int = 0
    """Number of raw_offer rows examined this run (accepted + rejected)."""
    accepted: int = 0
    rejected: int = 0
    rejected_by_reason: dict[str, int] = Field(default_factory=dict)
    pct_with_ean: float | None = None
    pct_with_brand: float | None = None
    pct_with_category: float | None = None
    pct_with_any_attr: float | None = None
    by_source: dict[str, SourceCoverage] = Field(default_factory=dict)
    unmapped_categories: list[list[object]] = Field(default_factory=list)
    """[source_name, source_category_raw, offer_count] triples, sorted by count desc — next week's mapping
    backlog for config/category_map.yaml (A2 task spec, section 5)."""

    def summary_line(self) -> str:
        """Returns one human-readable line summarizing this normalize run, suitable for terminal output."""

        def _fmt(pct: float | None) -> str:
            return f"{pct:.1f}%" if pct is not None else "n/a"

        reasons = ", ".join(f"{k}={v}" for k, v in sorted(self.rejected_by_reason.items())) or "none"
        return (
            f"[normalize] processed={self.processed} accepted={self.accepted} rejected={self.rejected} "
            f"({reasons}) pct_with_ean={_fmt(self.pct_with_ean)} pct_with_brand={_fmt(self.pct_with_brand)} "
            f"pct_with_category={_fmt(self.pct_with_category)} pct_with_any_attr={_fmt(self.pct_with_any_attr)}"
        )
