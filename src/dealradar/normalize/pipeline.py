"""Etap 2 driver: raw_offer -> Offer | RejectedOffer, and the normalize_batch orchestration (A2 task spec).

`GenericNormalizer` is the one concrete `SourceNormalizer` implementation this package ships: it is driven
entirely by a declarative field-mapping config (`mapping.NormalizerConfig`) plus an optional Python hook
(`hooks.HOOKS`), so a brand-new affiliate-feed source becomes a `config/normalizers/<source>.yaml` file, not
a new Python class (A2 task spec: "Dodanie nowego sklepu z feedu afiliacyjnego ma być zmianą w YAML-u, nie
w kodzie"). `normalize_batch` is the exact contract function named in the task spec; `run_normalize` and
`fill_run_report` are thin CLI-facing wrappers around it.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.orm import Session

from dealradar import db
from dealradar.models import Offer, RawOffer, RunReport
from dealradar.normalize import attrs as attrs_mod
from dealradar.normalize import condition as condition_mod
from dealradar.normalize import fx, identifiers, money, store
from dealradar.normalize import title as title_mod
from dealradar.normalize.brands import BrandIndex, load_brand_index
from dealradar.normalize.categories import CategoryMap, load_category_map
from dealradar.normalize.hooks import HOOKS, Hook
from dealradar.normalize.mapping import NormalizerConfig, NormalizerConfigRegistry, load_normalizer_registry
from dealradar.normalize.report import NormalizeReport, RejectedOffer, RejectReason, SourceCoverage

CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
NORMALIZERS_DIR = CONFIG_DIR / "normalizers"
BRANDS_PATH = CONFIG_DIR / "brands.yaml"
CATEGORY_MAP_PATH = CONFIG_DIR / "category_map.yaml"


@runtime_checkable
class SourceNormalizer(Protocol):
    """The A2 task spec's contract: source_name plus a raw_offer -> Offer | RejectedOffer transform."""

    source_name: str

    def to_offer(self, raw: RawOffer) -> "Offer | RejectedOffer":
        """Converts one raw_offer into a canonical Offer, or a RejectedOffer explaining why it could not be."""
        ...


# --------------------------------------------------------------------------------------------------
# Small, dependency-free coercion helpers for the loosely-typed feed fields that aren't prices.
# --------------------------------------------------------------------------------------------------


def _to_float(value: Any) -> float | None:
    """Coerces a feed value (str/int/float) to float; returns None when it is missing or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _to_int(value: Any) -> int | None:
    """Coerces a feed value to int via `_to_float`; returns None when it is missing or not numeric."""
    parsed = _to_float(value)
    return int(parsed) if parsed is not None else None


_TRUE_WORDS = {"true", "1", "tak", "yes", "dostepny", "dostępny", "in_stock", "instock"}
_FALSE_WORDS = {"false", "0", "nie", "no", "niedostepny", "niedostępny", "out_of_stock", "outofstock"}


def _to_bool(value: Any) -> bool | None:
    """Coerces a feed value (bool/int/PL-or-EN word) to bool; returns None when it cannot be read confidently."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def _round_amount(amount: int, rate: float) -> int:
    """Multiplies an integer amount (minor units) by an FX rate and rounds half-up to the nearest whole unit."""
    return int((Decimal(amount) * Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------------------------------
# GenericNormalizer
# --------------------------------------------------------------------------------------------------


class GenericNormalizer:
    """Declarative-mapping-driven SourceNormalizer: YAML field map + optional Python hook, one instance per source."""

    def __init__(
        self,
        source_name: str,
        *,
        config: NormalizerConfig,
        brand_index: BrandIndex,
        category_map: CategoryMap,
        hook: Hook | None = None,
    ) -> None:
        """Binds this normalizer to one source's field-mapping config plus the shared brand/category lookups."""
        self.source_name = source_name
        self._config = config
        self._brand_index = brand_index
        self._category_map = category_map
        self._hook = hook

    def _reject(self, raw: RawOffer, reason: RejectReason, detail: str) -> RejectedOffer:
        """Builds a RejectedOffer for `raw` with the given reason/detail, timestamped now."""
        return RejectedOffer(
            raw_offer_id=raw.id if raw.id is not None else -1,
            source_id=raw.source_id,
            external_id=raw.external_id,
            reason=reason,
            detail=detail,
            rejected_at=datetime.now(timezone.utc),
        )

    def to_offer(self, raw: RawOffer) -> "Offer | RejectedOffer":
        """Converts one raw_offer into a canonical Offer, or a RejectedOffer explaining why it could not be."""
        fields = self._config.extract_raw_fields(raw.payload_json)
        if self._hook is not None:
            fields = self._hook.apply(raw.payload_json, fields)

        title_raw = fields.get("title")
        if not title_raw or not str(title_raw).strip():
            return self._reject(raw, "missing_title", "no usable title field found in payload_json")
        title = str(title_raw).strip()

        url_raw = fields.get("url")
        if not url_raw or not str(url_raw).strip():
            return self._reject(raw, "missing_url", "no usable url field found in payload_json")
        url = str(url_raw).strip()

        description_raw = fields.get("description")
        description = str(description_raw).strip() if description_raw else None

        currency = str(fields["currency"]).strip().upper() if fields.get("currency") else money.detect_currency(
            fields.get("price_gross"), default=self._config.currency_default
        )

        price_amount = money.parse_amount_to_grosze(fields.get("price_gross"))
        if price_amount is None:
            return self._reject(
                raw, "missing_or_unparseable_price", f"price_gross raw value: {fields.get('price_gross')!r}"
            )

        evidence: dict[str, Any] = {}
        rate = 1.0
        as_of: date = raw.fetched_at.date()
        if currency != "PLN":
            rate_result = fx.get_fx_rate_to_pln(currency, as_of)
            if rate_result is None:
                return self._reject(raw, "fx_rate_unavailable", f"no NBP rate for {currency} as of {as_of}")
            rate, table_no = rate_result
            evidence.update(
                {
                    "fx_rate": rate,
                    "fx_currency": currency,
                    "fx_nbp_table_no": table_no,
                    "fx_as_of": as_of.isoformat(),
                    "fx_source": "NBP table A",
                }
            )

        price_gross = _round_amount(price_amount, rate) if currency != "PLN" else price_amount

        shipping_amount = money.parse_amount_to_grosze(fields.get("shipping_cost"))
        shipping_cost = (
            _round_amount(shipping_amount, rate) if shipping_amount is not None and currency != "PLN" else shipping_amount
        )

        list_price_amount = money.parse_amount_to_grosze(fields.get("list_price_claimed"))
        list_price_claimed = (
            _round_amount(list_price_amount, rate)
            if list_price_amount is not None and currency != "PLN"
            else list_price_amount
        )

        omnibus_amount = money.parse_amount_to_grosze(fields.get("omnibus_min_30d"))
        omnibus_min_30d = (
            _round_amount(omnibus_amount, rate) if omnibus_amount is not None and currency != "PLN" else omnibus_amount
        )

        data_issues: list[str] = []

        ean = identifiers.sanitize_ean(fields.get("ean"))

        mpn_raw = fields.get("mpn")
        mpn = str(mpn_raw).strip() if mpn_raw else identifiers.extract_mpn_from_title(title)
        mpn_norm = identifiers.normalize_mpn(mpn) if mpn else None

        brand_declared = fields.get("brand")
        brand: str | None
        brand_norm: str | None
        if brand_declared:
            resolved = self._brand_index.resolve(str(brand_declared))
            if resolved is not None:
                brand, brand_norm = resolved
            else:
                brand, brand_norm = str(brand_declared).strip(), None
                data_issues.append(f"unrecognized brand declared by source: {brand_declared!r}")
        else:
            found = self._brand_index.find_in_title(title)
            brand, brand_norm = found if found is not None else (None, None)

        category_raw = str(fields["category_raw"]).strip() if fields.get("category_raw") else None
        category_id = self._category_map.resolve(self.source_name, category_raw)

        condition_text = " ".join(
            str(part) for part in (title, description or "", fields.get("condition_text") or "") if part
        )
        resolved_condition = condition_mod.resolve_condition(
            condition_text, assume_new_if_undeclared=self._config.assume_new_if_undeclared
        )

        bundle_text = f"{title} {description or ''}"
        is_bundle, bundle_size = condition_mod.detect_bundle(bundle_text)

        feed_field_text = {k: str(v) for k, v in fields.items() if k not in {"title", "description"} and v}
        extracted_attrs = attrs_mod.extract_attrs(title=title, description=description, feed_fields=feed_field_text)
        if extracted_attrs:
            evidence["attrs"] = [a.model_dump(exclude_none=True) for a in extracted_attrs]

        try:
            offer = Offer(
                source_id=raw.source_id,
                external_id=raw.external_id,
                first_seen=raw.fetched_at,
                last_seen=raw.fetched_at,
                title=title,
                title_norm=title_mod.normalize_title(title),
                brand=brand,
                brand_norm=brand_norm,
                mpn=mpn,
                mpn_norm=mpn_norm,
                ean=ean,
                category_raw=category_raw,
                category_id=category_id,
                url=url,
                seller=str(fields["seller"]).strip() if fields.get("seller") else None,
                seller_rating=_to_float(fields.get("seller_rating")),
                condition=resolved_condition,
                is_bundle=is_bundle,
                bundle_size=bundle_size,
                price_gross=price_gross,
                shipping_cost=shipping_cost,
                currency="PLN",
                list_price_claimed=list_price_claimed,
                omnibus_min_30d=omnibus_min_30d,
                rating_avg=_to_float(fields.get("rating_avg")),
                rating_count=_to_int(fields.get("rating_count")),
                warranty_months=_to_int(fields.get("warranty_months")),
                in_stock=_to_bool(fields.get("in_stock")),
                data_issues=data_issues,
                evidence=evidence,
            )
        except Exception as exc:  # noqa: BLE001 - any pydantic ValidationError becomes a RejectedOffer, not a crash
            return self._reject(raw, "offer_validation_failed", f"{type(exc).__name__}: {exc}")
        return offer


# --------------------------------------------------------------------------------------------------
# normalize_batch / run_normalize / fill_run_report
# --------------------------------------------------------------------------------------------------


def _parse_iso_datetime(value: str) -> datetime:
    """Parses a datetime previously written via `datetime.isoformat()` back into a datetime."""
    return datetime.fromisoformat(value)


def _build_registry(
    session: Session,
) -> tuple[BrandIndex, CategoryMap, NormalizerConfigRegistry]:
    """Loads brands.yaml, category_map.yaml (ensuring its category tree exists in `session`), and normalizer configs."""
    brand_index = load_brand_index(BRANDS_PATH)
    category_map = load_category_map(CATEGORY_MAP_PATH)
    category_map.ensure_categories(session)
    normalizer_registry = load_normalizer_registry(NORMALIZERS_DIR)
    return brand_index, category_map, normalizer_registry


def normalize_batch(session: Session, since: datetime | None) -> NormalizeReport:
    """Normalizes every raw_offer (or those with fetched_at >= since) into `offer`/`rejected_offer`; returns the report.

    This is idempotent the same way `db.upsert_offer` is idempotent: rerunning normalize on unchanged
    raw_offer rows produces the same Offer rows again (keyed by source_id + external_id) rather than
    duplicates. RejectedOffer rows are append-only (each run's rejections are logged again), matching
    `raw_offer` itself being append-only (Z1) -- a row that was fixable and later re-collected with better
    data produces a *new* rejection or acceptance, not a silent overwrite of the old one.
    """
    store.ensure_rejected_offer_table(session)
    brand_index, category_map, normalizer_registry = _build_registry(session)
    sources = store.fetch_sources(session)

    normalizer_cache: dict[str, GenericNormalizer] = {}

    def get_normalizer(name: str, kind: str) -> GenericNormalizer:
        if name not in normalizer_cache:
            cfg = normalizer_registry.resolve(name, kind)
            normalizer_cache[name] = GenericNormalizer(
                name, config=cfg, brand_index=brand_index, category_map=category_map, hook=HOOKS.get(name)
            )
        return normalizer_cache[name]

    since_iso = since.isoformat() if since is not None else None
    raw_rows = store.fetch_raw_offers(session, since_iso=since_iso)

    report = NormalizeReport()
    for row in raw_rows:
        report.processed += 1
        source_id = int(row["source_id"])
        source_info = sources.get(source_id)

        if source_info is None:
            rejected = RejectedOffer(
                raw_offer_id=int(row["id"]),
                source_id=source_id,
                external_id=str(row["external_id"]),
                reason="unknown_source",
                detail=f"source_id {source_id} not present in `source` table",
                rejected_at=datetime.now(timezone.utc),
            )
            store.insert_rejected_offer(session, rejected)
            report.rejected += 1
            report.rejected_by_reason["unknown_source"] = report.rejected_by_reason.get("unknown_source", 0) + 1
            continue

        source_name, kind = source_info
        raw = RawOffer(
            id=int(row["id"]),
            source_id=source_id,
            external_id=str(row["external_id"]),
            fetched_at=_parse_iso_datetime(str(row["fetched_at"])),
            payload_json=store.parse_payload_json(str(row["payload_json"])),
            content_hash=str(row["content_hash"]),
        )

        result = get_normalizer(source_name, kind).to_offer(raw)
        coverage = report.by_source.setdefault(source_name, SourceCoverage(source_name=source_name))

        if isinstance(result, RejectedOffer):
            store.insert_rejected_offer(session, result)
            report.rejected += 1
            coverage.rejected += 1
            report.rejected_by_reason[result.reason] = report.rejected_by_reason.get(result.reason, 0) + 1
            continue

        db.upsert_offer(session, result)
        report.accepted += 1
        coverage.accepted += 1
        if result.ean:
            coverage.with_ean += 1
        if result.brand_norm:
            coverage.with_brand += 1
        if result.category_id is not None:
            coverage.with_category += 1
        if result.evidence.get("attrs"):
            coverage.with_any_attr += 1

    if report.accepted:
        report.pct_with_ean = 100.0 * sum(c.with_ean for c in report.by_source.values()) / report.accepted
        report.pct_with_brand = 100.0 * sum(c.with_brand for c in report.by_source.values()) / report.accepted
        report.pct_with_category = 100.0 * sum(c.with_category for c in report.by_source.values()) / report.accepted
        report.pct_with_any_attr = 100.0 * sum(c.with_any_attr for c in report.by_source.values()) / report.accepted

    report.unmapped_categories = [
        [source_name, category_raw, count] for source_name, category_raw, count in category_map.unmapped_report()
    ]
    return report


def run_normalize(session: Session, since: datetime | None = None) -> NormalizeReport:
    """Runs normalize_batch; kept as the stable, CLI-facing name for etap 2 (thin alias, same contract)."""
    return normalize_batch(session, since)


def fill_run_report(session: Session, report: RunReport, since: datetime | None) -> None:
    """Runs normalize_batch and copies its results into `report` (Z7); `since` is already-parsed, like
    cli.py's `_parse_since(since)` passes to `run_collect`/`run_snapshot_prices` for the sibling stages.

    This is the single function `dealradar normalize` (cli.py) needs to call -- it lets the CLI stay
    ignorant of NormalizeReport's shape and only deal in the A0-defined RunReport it already knows how to
    print and persist to run_log.
    """
    result = normalize_batch(session, since)

    report.offers_fetched = result.processed
    report.offers_new = result.accepted
    report.pct_with_ean = result.pct_with_ean
    if result.rejected_by_reason:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(result.rejected_by_reason.items()))
        report.notes.append(f"rejected={result.rejected} ({reasons})")
    for label, pct in (
        ("pct_with_brand", result.pct_with_brand),
        ("pct_with_category", result.pct_with_category),
        ("pct_with_any_attr", result.pct_with_any_attr),
    ):
        if pct is not None:
            report.notes.append(f"{label}={pct:.1f}%")
    if result.unmapped_categories:
        report.notes.append(f"top_unmapped_categories={result.unmapped_categories[:5]}")
