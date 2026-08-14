"""Canonical pydantic v2 data contracts for DealRadar (ARCHITEKTURA.md section 5).

Owner: A0. These models are the stable interface the rest of the team builds on — do not change field
names or types without updating every downstream agent. Hard rules enforced throughout this module:

- All money is an `int` number of grosze (Polish cents). Floats are rejected, never silently coerced.
- Missing data is `None` with a reason recorded somewhere nearby (`data_issues`, `missing_reason`), never
  a fabricated zero or default that looks like a real value (Z4).
- Every computed model (`Deal`, `QualityScore`) carries an `evidence: dict[str, Any]` field (Z6).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, computed_field, model_validator

# --------------------------------------------------------------------------------------------------
# Shared scalar types
# --------------------------------------------------------------------------------------------------


def _reject_float_amount(value: Any) -> Any:
    """Raises ValueError when a monetary field is given a float; passes any other value through unchanged."""
    if isinstance(value, float):
        raise ValueError(
            "monetary amounts must be an int number of grosze, not a float "
            f"(got {value!r}); convert to grosze explicitly before constructing the model"
        )
    return value


Grosze = Annotated[int, BeforeValidator(_reject_float_amount), Field(ge=0)]
"""An amount of money in whole grosze (1/100 PLN). Never a float — Z2."""

Condition = Literal["new", "refurb", "used", "damaged"]
MatchMethod = Literal["ean", "brand_mpn", "fuzzy", "manual", "none"]
DealBasis = Literal["history", "cross_shop"]
MatchLabel = Literal["same", "different", "conflict", "unlabeled"]
SourceKind = Literal["api", "feed", "rss"]
NotifyChannel = Literal["console", "file", "smtp", "telegram"]
ConfidenceLevel = Literal["high", "medium", "low"]
ExtractedFrom = Literal["title", "feed_field", "description"]


def validate_gtin(value: str) -> bool:
    """Returns True iff `value` is exactly 8 or 13 digits with a correct GTIN/EAN check digit (mod-10, GS1)."""
    if not isinstance(value, str) or not value.isdigit() or len(value) not in (8, 13):
        return False
    digits = [int(c) for c in value]
    body, check_digit = digits[:-1], digits[-1]
    total = 0
    for i, d in enumerate(reversed(body)):
        total += d * (3 if i % 2 == 0 else 1)
    computed_check = (10 - (total % 10)) % 10
    return computed_check == check_digit


def compute_content_hash(payload: dict[str, Any]) -> str:
    """Returns a stable sha256 hex digest of a JSON-serializable payload, used to dedupe unchanged raw offers."""
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class DealRadarModel(BaseModel):
    """Shared base for every canonical model: unknown fields are a hard error, not a silently dropped typo."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------------------------------
# source
# --------------------------------------------------------------------------------------------------


class Source(DealRadarModel):
    """A registered origin of offers — an API, a product feed, or an RSS channel (ARCHITEKTURA.md §5)."""

    id: int | None = None
    name: str
    kind: SourceKind
    base_url: str
    enabled: bool = True
    risk_prior: Annotated[float, Field(ge=0.0, le=1.0)]


# --------------------------------------------------------------------------------------------------
# raw_offer
# --------------------------------------------------------------------------------------------------


class RawOffer(DealRadarModel):
    """The untouched payload exactly as it arrived from a source, before any parsing (Z1: raw data is untouchable)."""

    id: int | None = None
    source_id: int
    external_id: str
    fetched_at: datetime
    payload_json: dict[str, Any]
    content_hash: str

    @model_validator(mode="after")
    def _check_content_hash_present(self) -> "RawOffer":
        """Rejects an empty content_hash; returns self unchanged when a hash is present."""
        if not self.content_hash:
            raise ValueError("content_hash must not be empty; use compute_content_hash(payload_json)")
        return self


# --------------------------------------------------------------------------------------------------
# offer
# --------------------------------------------------------------------------------------------------


class Offer(DealRadarModel):
    """A normalized, single-source listing of a product at a point in time (output of etap 2: normalize)."""

    id: int | None = None
    source_id: int
    external_id: str
    first_seen: datetime
    last_seen: datetime

    title: str
    title_norm: str | None = None
    brand: str | None = None
    brand_norm: str | None = None
    mpn: str | None = None
    mpn_norm: str | None = None
    ean: str | None = None

    category_raw: str | None = None
    category_id: int | None = None

    url: str
    seller: str | None = None
    seller_rating: Annotated[float, Field(ge=0.0, le=5.0)] | None = None
    condition: Condition | None = None
    is_bundle: bool = False
    bundle_size: Annotated[int, Field(ge=1)] | None = None

    price_gross: Grosze
    shipping_cost: Grosze | None = None
    currency: str = "PLN"
    list_price_claimed: Grosze | None = None
    """Z3: the shop's own 'before' price. Never used to compute a discount — kept only to measure the
    claimed-vs-real gap (shop_credibility / bullshit_index)."""
    omnibus_min_30d: Grosze | None = None

    rating_avg: Annotated[float, Field(ge=0.0, le=5.0)] | None = None
    rating_count: Annotated[int, Field(ge=0)] | None = None
    warranty_months: Annotated[int, Field(ge=0)] | None = None
    in_stock: bool | None = None

    product_id: int | None = None
    match_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    match_method: MatchMethod | None = None

    data_issues: list[str] = Field(default_factory=list)
    """Reasons data was dropped or looked suspicious during normalization (e.g. a rejected EAN)."""
    evidence: dict[str, Any] = Field(default_factory=dict)
    """Not in ARCHITEKTURA.md §5's offer columns verbatim — added by A0 so normalize-time facts that need
    provenance (e.g. an FX rate used to convert a foreign price) have somewhere to live, per the Z6 spirit
    already mandated for Deal/QualityScore. See the A0 final report for the full rationale."""

    @model_validator(mode="after")
    def _validate_ean(self) -> "Offer":
        """Nulls an EAN that fails length/checksum validation and records why in data_issues; returns self."""
        if self.ean is not None and not validate_gtin(self.ean):
            self.data_issues = [*self.data_issues, f"invalid EAN dropped: {self.ean!r}"]
            self.ean = None
        return self

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_total(self) -> int:
        """Returns price_gross plus shipping_cost (treated as 0 when unknown) — the only price used for ranking."""
        return self.price_gross + (self.shipping_cost or 0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def price_incomplete(self) -> bool:
        """Returns True when shipping_cost is unknown, meaning this offer must be excluded from ranking (F3)."""
        return self.shipping_cost is None


# --------------------------------------------------------------------------------------------------
# price_point
# --------------------------------------------------------------------------------------------------


class PricePoint(DealRadarModel):
    """One append-only daily observation of an offer's total price and stock status — the system's core evidence."""

    id: int | None = None
    offer_id: int
    observed_at: datetime
    price_total: Grosze
    in_stock: bool


# --------------------------------------------------------------------------------------------------
# product / product_attr / category
# --------------------------------------------------------------------------------------------------


class Product(DealRadarModel):
    """A canonical cluster of offers believed to be the same real-world item (output of etap 3: match)."""

    id: int | None = None
    canonical_title: str
    brand: str | None = None
    mpn: str | None = None
    ean: str | None = None
    category_id: int | None = None
    created_at: datetime


class ProductAttr(DealRadarModel):
    """One extracted numeric or textual attribute of a product (e.g. capacity_gb=512), with its extraction source."""

    id: int | None = None
    product_id: int
    key: str
    value_num: float | None = None
    value_text: str | None = None
    unit: str | None = None
    extracted_from: ExtractedFrom | None = None


class Category(DealRadarModel):
    """A node in DealRadar's own category tree, independent of any single source's taxonomy."""

    id: int | None = None
    parent_id: int | None = None
    name: str
    path: str


# --------------------------------------------------------------------------------------------------
# deal / quality_score / shop_credibility / match_review
# --------------------------------------------------------------------------------------------------


class Deal(DealRadarModel):
    """A computed deal-strength assessment for one offer at one point in time (etap 5: score). Carries evidence (Z6)."""

    id: int | None = None
    offer_id: int
    computed_at: datetime
    basis: DealBasis
    deal_score: Annotated[float, Field(ge=0.0, le=100.0)]
    depth_vs_median_90d: float | None = None
    z_robust: float | None = None
    price_rank_today: Annotated[int, Field(ge=1)] | None = None
    is_alltime_low: bool = False
    n_competing_offers: Annotated[int, Field(ge=0)] = 0
    n_days_history: Annotated[int, Field(ge=0)] = 0
    coverage_cap: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    """med, mad, sigma, point count, date range, competitor offer_ids, applied ceiling and why (Z6)."""


class QualityScore(DealRadarModel):
    """A computed 1-10 value-for-money score for a product, or None with a reason when it cannot be computed (Z4)."""

    id: int | None = None
    product_id: int
    computed_at: datetime
    peer_group_id: str | None = None
    value_score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    reliability_score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    deal_strength: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    risk_score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    total_1_10: Annotated[float, Field(ge=1.0, le=10.0)] | None = None
    confidence: ConfidenceLevel | None = None
    missing_reason: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _require_reason_when_missing(self) -> "QualityScore":
        """Enforces Z4: a null total_1_10 must always carry a missing_reason; returns self unchanged."""
        if self.total_1_10 is None and not self.missing_reason:
            raise ValueError("total_1_10 is None but missing_reason was not set (Z4: absence needs a reason)")
        return self


class ShopCredibility(DealRadarModel):
    """Per-source, per-period comparison of claimed vs. real discounts — the 'bullshit index' (Z3, failure mode F1)."""

    id: int | None = None
    source_id: int
    period: str
    """Free-form period label chosen by the caller, e.g. an ISO month '2026-08'. A0 does not fix the format;
    A4 (owner of shop_credibility computation) may document a stricter convention."""
    claimed_discount_avg: float | None = None
    real_discount_avg: float | None = None
    bullshit_index: float | None = None
    n_offers: Annotated[int, Field(ge=0)] = 0


class MatchReview(DealRadarModel):
    """A pair of offers flagged for human review, or already manually labeled — the matcher's evaluation set (Z5)."""

    id: int | None = None
    offer_a: int
    offer_b: int
    combined_score: Annotated[float, Field(ge=0.0, le=1.0)] | None = None
    method: MatchMethod | None = None
    label: MatchLabel | None = None
    labeled_at: datetime | None = None


# --------------------------------------------------------------------------------------------------
# watch
# --------------------------------------------------------------------------------------------------


class Watch(DealRadarModel):
    """A standing alert on a specific product or a free-text query, evaluated on every run."""

    id: int | None = None
    product_id: int | None = None
    query: str | None = None
    max_price: Grosze | None = None
    min_deal_score: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    channel: NotifyChannel = "console"
    active: bool = True

    @model_validator(mode="after")
    def _require_target(self) -> "Watch":
        """Ensures the watch targets either a product_id or a free-text query; returns self unchanged."""
        if self.product_id is None and not self.query:
            raise ValueError("Watch needs either product_id or query, got neither")
        return self


# --------------------------------------------------------------------------------------------------
# run_log / RunReport (Z7)
# --------------------------------------------------------------------------------------------------


class RunReport(DealRadarModel):
    """Per-stage run summary (Z7): what happened, in numbers. Written to run_log at the end of every CLI command."""

    id: int | None = None
    stage: str
    started_at: datetime
    finished_at: datetime | None = None
    status: Literal["ok", "partial", "failed"] = "ok"
    offers_fetched: Annotated[int, Field(ge=0)] = 0
    offers_new: Annotated[int, Field(ge=0)] = 0
    pct_with_ean: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    pct_matched: Annotated[float, Field(ge=0.0, le=100.0)] | None = None
    products_new: Annotated[int, Field(ge=0)] = 0
    deals_scored: Annotated[int, Field(ge=0)] = 0
    quality_scored: Annotated[int, Field(ge=0)] = 0
    sources_failed: list[str] = Field(default_factory=list)
    """Names of sources that failed or suspiciously returned 0 records this run (F5), not just a bare count —
    a count with no names is not evidence (Z6)."""
    duration_s: Annotated[float, Field(ge=0.0)] | None = None
    notes: list[str] = Field(default_factory=list)

    def summary_line(self) -> str:
        """Returns one human-readable line summarizing this run, suitable for terminal output."""
        pct_ean = f"{self.pct_with_ean:.1f}%" if self.pct_with_ean is not None else "n/a"
        pct_matched = f"{self.pct_matched:.1f}%" if self.pct_matched is not None else "n/a"
        dur = f"{self.duration_s:.2f}s" if self.duration_s is not None else "n/a"
        failed = ", ".join(self.sources_failed) if self.sources_failed else "none"
        return (
            f"[{self.stage}] status={self.status} offers_fetched={self.offers_fetched} "
            f"offers_new={self.offers_new} pct_with_ean={pct_ean} pct_matched={pct_matched} "
            f"products_new={self.products_new} deals_scored={self.deals_scored} "
            f"quality_scored={self.quality_scored} sources_failed=[{failed}] duration={dur}"
        )
