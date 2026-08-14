"""Etap 5 (score): `quality_score` -- the deterministic 1-10 value-for-money rubric. Owner: A5.

Implements ARCHITEKTURA.md section 9 literally: a same-category, same-price-shelf peer group; four
components (reliability, value, deal strength, risk) each converted to a *percentile within that peer group*
rather than used as an absolute number; a weighted blend (weights per category, renormalized when a
component is missing) scaled to 1-10; and gate G3, which returns `None` + a `missing_reason` instead of a
number whenever the inputs do not support one (Z4: "ocena wymyślona jest gorsza niż jej brak, bo wygląda tak
samo jak prawdziwa").

Two defenses this module exists specifically to implement:

- **F4 (ocena z powietrza)**: `_reliability_raw` shrinks every rating toward the category median with a
  Bayesian pseudo-count (C=30) so "5.0 from three reviews" cannot outrank "4.6 from two thousand", and
  penalizes (drops) the higher of two shops' ratings when they disagree by more than 0.8 -- a locally
  inflated rating on one shop is not independent confirmation.
- **The "no fabricated bonus" rule for optional formula variables**: `config/categories/*.yaml` formulas may
  reference attributes that are not in `required_attrs`. `evaluate_formula` resolves an absent variable to
  `0.0`. Every formula in this repo is written so that default lands on "no bonus, unchanged baseline" --
  never on "look, a bonus for missing data" -- see the comments in each category YAML for the specific
  reasoning per formula. `required_attrs` still gates the component to `None` outright when the *primary*
  signal is missing; `0.0`-for-absent only ever applies to secondary/bonus terms.

No `eval()` anywhere (Zadanie 2's explicit requirement): `compile_formula` walks the parsed AST once at
config-load time and rejects anything outside a small whitelist (arithmetic, numeric literals, variable
names, and calls to `norm`/`min`/`max`/`log`) before a formula is ever evaluated against real data.
"""

from __future__ import annotations

import ast
import json
import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.errors import DealRadarError
from dealradar.models import ConfidenceLevel, QualityScore, compute_content_hash

DEFAULT_CATEGORIES_DIR = Path(__file__).resolve().parents[3] / "config" / "categories"


class FormulaError(DealRadarError):
    """Raised when a config/categories/*.yaml utility formula is malformed or fails the AST safety whitelist."""


# --------------------------------------------------------------------------------------------------
# Safe expression evaluator (Zadanie 2: "biala lista funkcji ... na AST. Nie eval()").
# --------------------------------------------------------------------------------------------------

_ALLOWED_BINOPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a**b,
    ast.Mod: lambda a, b: a % b,
}
_ALLOWED_UNARYOPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: lambda a: +a,
    ast.USub: lambda a: -a,
}


def _safe_norm(x: float, lo: float, hi: float) -> float:
    """Linearly rescales x from [lo, hi] to [0, 1], clamped at both ends; returns 0.0 when hi == lo."""
    if hi == lo:
        return 0.0
    return max(0.0, min(1.0, (x - lo) / (hi - lo)))


def _safe_min(*args: float) -> float:
    """Whitelisted `min()` for formulas: returns the smallest of one or more numeric arguments."""
    if not args:
        raise FormulaError("min() requires at least one argument")
    return float(min(args))


def _safe_max(*args: float) -> float:
    """Whitelisted `max()` for formulas: returns the largest of one or more numeric arguments."""
    if not args:
        raise FormulaError("max() requires at least one argument")
    return float(max(args))


def _safe_log(x: float, base: float = math.e) -> float:
    """Whitelisted `log()` for formulas: natural log by default, or log base `base`; returns 0.0 for x <= 0."""
    if x <= 0:
        return 0.0
    return math.log(x, base)


_ALLOWED_FUNCS: dict[str, Callable[..., float]] = {
    "norm": _safe_norm,
    "min": _safe_min,
    "max": _safe_max,
    "log": _safe_log,
}


def _validate_ast(node: ast.AST, expr: str) -> None:
    """Recursively raises FormulaError unless every node under `node` is on the arithmetic/whitelist-call grammar."""
    if isinstance(node, ast.Expression):
        _validate_ast(node.body, expr)
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _ALLOWED_BINOPS:
            raise FormulaError(f"operator {type(node.op).__name__!r} not allowed in formula: {expr!r}")
        _validate_ast(node.left, expr)
        _validate_ast(node.right, expr)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _ALLOWED_UNARYOPS:
            raise FormulaError(f"unary operator {type(node.op).__name__!r} not allowed in formula: {expr!r}")
        _validate_ast(node.operand, expr)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            bad = node.func.id if isinstance(node.func, ast.Name) else type(node.func).__name__
            raise FormulaError(f"call to {bad!r} not in the whitelist (norm/min/max/log) in formula: {expr!r}")
        if node.keywords:
            raise FormulaError(f"keyword arguments not allowed in formula: {expr!r}")
        for arg in node.args:
            _validate_ast(arg, expr)
    elif isinstance(node, ast.Name):
        return
    elif isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise FormulaError(f"only numeric literals allowed in formula: {expr!r}")
    else:
        raise FormulaError(f"disallowed expression element {type(node).__name__!r} in formula: {expr!r}")


def compile_formula(expr: str) -> ast.Expression:
    """Parses `expr` as a Python expression and returns its AST, or raises FormulaError if any node is unsafe.

    config/categories/*.yaml is untrusted input, not trusted code (task spec: "Plik konfiguracyjny nie jest
    zaufanym kodem"), so this never calls `eval()`/`compile()`-and-exec: it parses once with `ast.parse` and
    walks the tree, permitting only arithmetic (+ - * / % **, unary +/-), numeric literals, bare variable
    names, and calls to `norm`/`min`/`max`/`log` with positional arguments. Attribute access, subscripts,
    string/bytes/collection literals, comprehensions, lambdas, `__import__`, and calls to any other name are
    all rejected here, before the formula is ever evaluated against real data.
    """
    tree = ast.parse(expr, mode="eval")
    _validate_ast(tree, expr)
    return tree


def evaluate_formula(tree: ast.Expression, variables: dict[str, float]) -> float:
    """Evaluates a compile_formula() result against `variables`; a name absent from `variables` resolves to 0.0.

    See this module's docstring: 0.0-for-absent is the documented default for OPTIONAL formula variables
    only (every formula here is written so that lands on "no bonus", never a fabricated advantage).
    `required_attrs` presence is checked by `utility_for` before this ever runs, so a *required* attribute
    is never silently defaulted -- its absence returns None one layer up, not a number computed with 0.0.
    """
    return _eval_node(tree.body, variables)


def _eval_node(node: ast.AST, variables: dict[str, float]) -> float:
    """Evaluates one AST node already validated by compile_formula; raises FormulaError on any other node type."""
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left, variables)
        right = _eval_node(node.right, variables)
        return _ALLOWED_BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp):
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_node(node.operand, variables))
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)  # guaranteed by compile_formula's validation pass
        func = _ALLOWED_FUNCS[node.func.id]
        args = [_eval_node(arg, variables) for arg in node.args]
        return float(func(*args))
    if isinstance(node, ast.Name):
        return float(variables.get(node.id, 0.0))
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            # Unreachable given compile_formula's validation pass, but narrows the type for mypy and
            # keeps this function safe to call directly (as the tests do) without that pass having run.
            raise FormulaError(f"only numeric literals allowed, got {value!r}")
        return float(value)
    raise FormulaError(f"disallowed expression element at eval time: {type(node).__name__!r}")


# --------------------------------------------------------------------------------------------------
# config/categories/*.yaml -> validated, compiled CategoryFormula
# --------------------------------------------------------------------------------------------------


class ComponentWeights(BaseModel):
    """The four total_1_10 weights for one category, as given in its YAML file, before renormalization."""

    model_config = ConfigDict(extra="forbid")

    value: float = Field(ge=0.0)
    reliability: float = Field(ge=0.0)
    deal: float = Field(ge=0.0)
    risk: float = Field(ge=0.0)

    def as_dict(self) -> dict[str, float]:
        """Returns the four weights as a plain {component_name: weight} dict, for evidence and lookups."""
        return {"value": self.value, "reliability": self.reliability, "deal": self.deal, "risk": self.risk}


class CategoryFormulaConfig(BaseModel):
    """One parsed config/categories/<category>.yaml file, before its `utility` expression is AST-compiled."""

    model_config = ConfigDict(extra="forbid")

    category: str
    required_attrs: list[str] = Field(default_factory=list)
    utility: str
    weights: ComponentWeights


@dataclass(frozen=True)
class CategoryFormula:
    """A validated category utility formula, with `utility` already AST-compiled (compile_formula)."""

    slug: str
    required_attrs: tuple[str, ...]
    weights: ComponentWeights
    compiled_utility: ast.Expression
    source_path: Path


def load_category_formulas(config_dir: Path | None = None) -> dict[str, CategoryFormula]:
    """Loads/validates/compiles every config/categories/*.yaml file; returns {leaf_category_slug: CategoryFormula}.

    `slug` is matched against the last "/"-separated segment of `category.path` (e.g. "elektronika/dyski_ssd"
    -> "dyski_ssd") by the caller. Raises FormulaError if a file is not a mapping, fails ComponentWeights/
    required_attrs validation, its `utility` expression fails the AST whitelist, or two files declare the
    same `category` slug. A missing or empty directory is not an error -- it simply means every product
    falls through gate G3 with missing_reason="no_utility_formula" (Z4: a coverage gap is data, not a crash).
    """
    directory = config_dir or DEFAULT_CATEGORIES_DIR
    result: dict[str, CategoryFormula] = {}
    if not directory.exists():
        return result
    for path in sorted(directory.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise FormulaError(f"expected a YAML mapping at the top level of {path}, got {type(data).__name__}")
        try:
            parsed = CategoryFormulaConfig.model_validate(data)
        except Exception as exc:  # pydantic.ValidationError, normalized to our own error type
            raise FormulaError(f"invalid category formula config in {path}: {exc}") from exc
        compiled = compile_formula(parsed.utility)
        if parsed.category in result:
            raise FormulaError(
                f"duplicate category slug {parsed.category!r}: {path} and {result[parsed.category].source_path}"
            )
        result[parsed.category] = CategoryFormula(
            slug=parsed.category,
            required_attrs=tuple(parsed.required_attrs),
            weights=parsed.weights,
            compiled_utility=compiled,
            source_path=path,
        )
    return result


def utility_for(formula: CategoryFormula, attrs: dict[str, float]) -> float | None:
    """Returns formula's utility value for `attrs`, or None if any required_attrs key is absent (Z4) or eval fails.

    A runtime arithmetic failure (division by zero from the *data*, e.g. an attribute value of 0 in a
    denominator) degrades this one product's value component to None rather than aborting the whole
    `score --quality` run -- a malformed *formula* is caught earlier, at config-load time, by compile_formula.
    """
    for key in formula.required_attrs:
        if key not in attrs:
            return None
    try:
        return evaluate_formula(formula.compiled_utility, attrs)
    except (ZeroDivisionError, TypeError, ValueError, OverflowError):
        return None


# --------------------------------------------------------------------------------------------------
# Peer group (Zadanie 1)
# --------------------------------------------------------------------------------------------------

PEER_GROUP_MIN_BAND = 0.25
PEER_GROUP_MAX_BAND = 0.60
PEER_GROUP_BAND_STEP = 0.05
PEER_GROUP_TARGET_SIZE = 5
"""Below this many members, the band widens (up to PEER_GROUP_MAX_BAND) before giving up."""
PEER_GROUP_MIN_MEMBERS = 3
"""Gate G3: fewer than this many members even at the widest band -> total_1_10=None (peer_group_too_small)."""


@dataclass(frozen=True)
class PeerMember:
    """One other product in a PeerGroup: its id and representative (median, eligible-offer) price in grosze."""

    product_id: int
    price_total: int


@dataclass(frozen=True)
class PeerGroup:
    """The comparison set for product_id: same leaf category, price within `band` of its own reference price."""

    product_id: int
    category_id: int
    category_path: str
    reference_price: int
    """Median price_total (grosze) across product_id's own new/non-bundle/in-stock offers -- the band's center."""
    band: float
    """The price-band fraction actually used (0.25..0.60) after widening, per ARCHITEKTURA.md sec. 9."""
    members: list[PeerMember]
    """Other products in the same leaf category within the band -- excludes product_id itself."""
    confidence: ConfidenceLevel
    peer_group_id: str


def _leaf_category_path(session: Session, category_id: int) -> str | None:
    """Returns category.path for category_id, or None if no such category row exists."""
    row = session.execute(text("SELECT path FROM category WHERE id = :id"), {"id": category_id}).scalar_one_or_none()
    return str(row) if row is not None else None


def _eligible_offer_prices(session: Session, product_id: int) -> list[int]:
    """Returns price_total (grosze) for product_id's new, non-bundle, in-stock offers -- the peer-group price filter.

    Filters `condition = 'new'`, `is_bundle = 0`, `in_stock = 1` on the offer side before any median is
    taken, per the task spec's explicit trap: an offer also sold as a bundle or a refurbished/poleasing unit
    must never sneak into the price used to build or rank a peer group as an artificially cheap competitor.
    """
    rows = session.execute(
        text(
            "SELECT price_total FROM offer "
            "WHERE product_id = :product_id AND condition = 'new' AND is_bundle = 0 AND in_stock = 1"
        ),
        {"product_id": product_id},
    ).scalars().all()
    return [int(r) for r in rows]


def build_peer_group(session: Session, product_id: int) -> PeerGroup | None:
    """Builds product_id's comparison set (ARCHITEKTURA.md sec. 9): same leaf category, price within a widening band.

    Returns None only when a group cannot even be attempted: product_id does not exist, has no category_id,
    its category_id does not resolve to a category row, or it has no eligible (new/non-bundle/in-stock)
    offer to anchor a reference price on. Otherwise this always returns a PeerGroup -- even one with fewer
    than PEER_GROUP_MIN_MEMBERS members -- so score_quality (and tests) can inspect exactly how far the band
    widened and how many competitors were actually found; the "< 3 members" gate (G3,
    missing_reason="peer_group_too_small") is applied by the caller, not here.
    """
    product_row = session.execute(
        text("SELECT category_id FROM product WHERE id = :id"), {"id": product_id}
    ).mappings().first()
    if product_row is None or product_row["category_id"] is None:
        return None
    category_id = int(product_row["category_id"])
    category_path = _leaf_category_path(session, category_id)
    if category_path is None:
        return None

    own_prices = _eligible_offer_prices(session, product_id)
    if not own_prices:
        return None
    reference_price = int(statistics.median(own_prices))

    candidate_rows = session.execute(
        text(
            "SELECT o.product_id AS product_id, o.price_total AS price_total FROM offer o "
            "WHERE o.category_id = :category_id AND o.condition = 'new' AND o.is_bundle = 0 "
            "AND o.in_stock = 1 AND o.product_id IS NOT NULL AND o.product_id != :product_id"
        ),
        {"category_id": category_id, "product_id": product_id},
    ).mappings().all()
    prices_by_product: dict[int, list[int]] = {}
    for row in candidate_rows:
        prices_by_product.setdefault(int(row["product_id"]), []).append(int(row["price_total"]))
    candidate_prices = {pid: int(statistics.median(prices)) for pid, prices in prices_by_product.items()}

    band = PEER_GROUP_MIN_BAND
    members: list[PeerMember] = []
    while True:
        low = reference_price * (1 - band)
        high = reference_price * (1 + band)
        members = [
            PeerMember(product_id=pid, price_total=price)
            for pid, price in sorted(candidate_prices.items())
            if low <= price <= high
        ]
        if len(members) >= PEER_GROUP_TARGET_SIZE or band >= PEER_GROUP_MAX_BAND - 1e-9:
            break
        band = round(band + PEER_GROUP_BAND_STEP, 2)

    confidence: ConfidenceLevel
    if len(members) >= PEER_GROUP_TARGET_SIZE:
        confidence = "high" if band == PEER_GROUP_MIN_BAND else "medium"
    else:
        confidence = "low"

    peer_group_id = compute_content_hash(
        {"product_id": product_id, "band": band, "members": sorted(m.product_id for m in members)}
    )[:16]

    return PeerGroup(
        product_id=product_id,
        category_id=category_id,
        category_path=category_path,
        reference_price=reference_price,
        band=band,
        members=members,
        confidence=confidence,
        peer_group_id=peer_group_id,
    )


# --------------------------------------------------------------------------------------------------
# Percentile-within-peer-group (Zadanie 2 intro: "kazda [skladowa] jako percentyl w grupie
# porownawczej, nie do wartosci bezwzglednej").
# --------------------------------------------------------------------------------------------------


def _percentile(value: float, population: list[float]) -> float:
    """Returns value's mid-rank percentile (0..1) within population (population must include value itself).

    Mid-rank -- (n_less + 0.5*n_equal) / n -- handles ties and the single-member case gracefully: with n=1
    it resolves to exactly 0.5 ("no comparison possible"), never a fabricated 1.0 or 0.0.
    """
    n = len(population)
    n_less = sum(1 for v in population if v < value)
    n_equal = sum(1 for v in population if v == value)
    return (n_less + 0.5 * n_equal) / n


# --------------------------------------------------------------------------------------------------
# Component 1: reliability (F4 defense -- bayesian shrinkage + cross-shop agreement modifier)
# --------------------------------------------------------------------------------------------------

BAYES_C = 30.0
"""Bayesian pseudo-count: r* = (C*m + sum(rating_avg*rating_count)) / (C + sum(rating_count))."""
AGREEMENT_BONUS_THRESHOLD = 0.4
DISAGREEMENT_PENALTY_THRESHOLD = 0.8
AGREEMENT_BONUS_AMOUNT = 0.15
"""Flat bonus (0-5 scale) applied to r* when >=2 sources agree within AGREEMENT_BONUS_THRESHOLD -- an exact
value is not specified in ARCHITEKTURA.md sec. 9 ("premia"); this is A5's own choice, documented here."""


@dataclass(frozen=True)
class _ProductOfferSignals:
    """The raw per-offer signals of one product's current offers, used by reliability_raw and risk_raw."""

    ratings: list[tuple[int, float, int]]
    """(source_id, rating_avg, rating_count) for offers that have both a rating average and a positive count."""
    warranty_months: list[int]
    seller_ratings: list[float]
    risk_priors: list[float]
    condition: str | None
    """The product's condition, taken from any offer that has one (the matcher never merges different
    condition classes into one product, so this is well-defined whenever any offer states a condition)."""


def _load_offer_signals(session: Session, product_id: int) -> _ProductOfferSignals:
    """Returns product_id's rating/warranty/seller-rating/source-risk/condition signals across ALL its offers.

    Deliberately unfiltered by condition/is_bundle/in_stock (unlike _eligible_offer_prices, which is scoped
    to peer-group price banding specifically) -- reliability and risk should reflect every store currently
    selling this product, not just the subset used to anchor the peer-group price band.
    """
    rows = session.execute(
        text(
            "SELECT o.source_id AS source_id, o.rating_avg AS rating_avg, o.rating_count AS rating_count, "
            "o.warranty_months AS warranty_months, o.seller_rating AS seller_rating, o.condition AS condition, "
            "s.risk_prior AS risk_prior "
            "FROM offer o JOIN source s ON s.id = o.source_id WHERE o.product_id = :product_id"
        ),
        {"product_id": product_id},
    ).mappings().all()
    ratings = [
        (int(r["source_id"]), float(r["rating_avg"]), int(r["rating_count"]))
        for r in rows
        if r["rating_avg"] is not None and r["rating_count"] is not None and int(r["rating_count"]) > 0
    ]
    warranty = [int(r["warranty_months"]) for r in rows if r["warranty_months"] is not None]
    seller = [float(r["seller_rating"]) for r in rows if r["seller_rating"] is not None]
    risk_priors = [float(r["risk_prior"]) for r in rows]
    condition = next((str(r["condition"]) for r in rows if r["condition"] is not None), None)
    return _ProductOfferSignals(
        ratings=ratings, warranty_months=warranty, seller_ratings=seller, risk_priors=risk_priors, condition=condition
    )


def _category_median_rating(signals_by_product: dict[int, _ProductOfferSignals]) -> float:
    """Returns the median per-product weighted-average rating across a peer group, or a neutral 3.0 fallback.

    Scoped to the peer group itself (same leaf category, similar price -- already a representative
    same-shelf sample) rather than a second, broader query across the whole category table. The 3.0
    fallback only matters when literally no member of the group has any rating; in that case it is never
    actually used, because `_reliability_raw` returns None for every member before `m` would apply to any.
    """
    per_product_avgs: list[float] = []
    for signals in signals_by_product.values():
        if not signals.ratings:
            continue
        total_weighted = sum(avg * cnt for _, avg, cnt in signals.ratings)
        total_count = sum(cnt for _, _, cnt in signals.ratings)
        per_product_avgs.append(total_weighted / total_count)
    if not per_product_avgs:
        return 3.0
    return float(statistics.median(per_product_avgs))


@dataclass(frozen=True)
class ReliabilityResult:
    """One product's bayesian-shrunk reliability rating (0-5 scale) plus the evidence behind it."""

    shrunk_rating: float
    n_ratings: int
    per_source_avg: dict[int, float]
    modifier: str
    """"none" | "agreement_bonus" | "disagreement_penalty"."""
    category_median_m: float


def _reliability_raw(signals: _ProductOfferSignals, category_median_m: float) -> ReliabilityResult | None:
    """Returns the product's bayesian-shrunk rating (0-5), or None if it has no ratings at all (Z4).

    F4 defense in two parts: (1) the C=30 pseudo-count formula pulls a rating with few reviews toward the
    category median, so "5.0 from 3 reviews" cannot outrank "4.6 from 2000" (see test_quality.py); (2) when
    ratings come from >= 2 sources and disagree by more than DISAGREEMENT_PENALTY_THRESHOLD, the source(s)
    with the *highest* average are dropped from the sum before shrinking -- a locally inflated rating on one
    shop (bought reviews) does not get to drag the combined estimate up. Agreement within
    AGREEMENT_BONUS_THRESHOLD across >= 2 sources instead earns a small flat bonus (independent confirmation).
    """
    if not signals.ratings:
        return None
    per_source: dict[int, list[tuple[float, int]]] = {}
    for source_id, avg, count in signals.ratings:
        per_source.setdefault(source_id, []).append((avg, count))
    per_source_avg = {
        sid: sum(avg * cnt for avg, cnt in pairs) / sum(cnt for _, cnt in pairs) for sid, pairs in per_source.items()
    }

    modifier = "none"
    used_ratings = list(signals.ratings)
    if len(per_source_avg) >= 2:
        spread = max(per_source_avg.values()) - min(per_source_avg.values())
        if spread > DISAGREEMENT_PENALTY_THRESHOLD:
            modifier = "disagreement_penalty"
            max_avg = max(per_source_avg.values())
            used_ratings = [(sid, avg, cnt) for sid, avg, cnt in signals.ratings if per_source_avg[sid] < max_avg - 1e-9]
            if not used_ratings:  # every source tied at the max (degenerate) -- fall back to the full set
                used_ratings = list(signals.ratings)
        elif spread <= AGREEMENT_BONUS_THRESHOLD:
            modifier = "agreement_bonus"

    sum_r = sum(avg * cnt for _, avg, cnt in used_ratings)
    n = sum(cnt for _, _, cnt in used_ratings)
    shrunk = (BAYES_C * category_median_m + sum_r) / (BAYES_C + n)
    if modifier == "agreement_bonus":
        shrunk = min(5.0, shrunk + AGREEMENT_BONUS_AMOUNT)
    return ReliabilityResult(
        shrunk_rating=shrunk, n_ratings=n, per_source_avg=per_source_avg, modifier=modifier, category_median_m=category_median_m
    )


# --------------------------------------------------------------------------------------------------
# Component 4: risk (odwrocone -- mniejsze ryzyko, wyzszy wklad)
# --------------------------------------------------------------------------------------------------

_CONDITION_BADNESS = {"new": 0.0, "refurb": 0.3, "used": 0.5, "damaged": 1.0}
WARRANTY_GOOD_MONTHS = 24.0
"""warranty_months at/above this is treated as full risk credit (norm() saturates at 1.0); an A5 choice --
ARCHITEKTURA.md sec. 9 names warranty as a risk input but not a scale, so this picks the common EU statutory
minimum-plus figure as "as good as it needs to be", documented here rather than left as a magic number."""


@dataclass(frozen=True)
class RiskResult:
    """One product's risk badness/goodness (0..1) plus the sub-signals that went into it."""

    badness: float
    goodness: float
    warranty_months_avg: float | None
    seller_rating_avg: float | None
    source_risk_prior_avg: float | None
    condition_badness: float | None


def _risk_raw(signals: _ProductOfferSignals) -> RiskResult | None:
    """Returns the product's risk badness/goodness (0..1) from warranty, seller rating, source risk_prior, condition.

    Every sub-signal that is actually present is averaged in; a sub-signal with no data anywhere is simply
    left out of the average rather than fabricated as 0 or worst-case (Z4). Returns None only when NONE of
    the four sub-signals have any data at all -- in practice this means a product with zero offers, which
    build_peer_group would already have excluded upstream (every registered `source` has a required
    risk_prior, so any product with at least one offer always has at least that one sub-signal).
    """
    parts: list[float] = []

    warranty_val: float | None = None
    if signals.warranty_months:
        warranty_val = statistics.mean(signals.warranty_months)
        parts.append(1.0 - _safe_norm(warranty_val, 0.0, WARRANTY_GOOD_MONTHS))

    seller_val: float | None = None
    if signals.seller_ratings:
        seller_val = statistics.mean(signals.seller_ratings)
        parts.append(1.0 - _safe_norm(seller_val, 0.0, 5.0))

    source_val: float | None = None
    if signals.risk_priors:
        source_val = statistics.mean(signals.risk_priors)
        parts.append(source_val)

    condition_val: float | None = None
    if signals.condition is not None:
        condition_val = _CONDITION_BADNESS.get(signals.condition, 0.5)
        parts.append(condition_val)

    if not parts:
        return None
    badness = statistics.mean(parts)
    return RiskResult(
        badness=badness,
        goodness=1.0 - badness,
        warranty_months_avg=warranty_val,
        seller_rating_avg=seller_val,
        source_risk_prior_avg=source_val,
        condition_badness=condition_val,
    )


# --------------------------------------------------------------------------------------------------
# Component 3: deal strength -- read-only from A4's `deal` table (Zadanie 2.3: "nie przeliczasz").
# --------------------------------------------------------------------------------------------------


def _latest_deal_score(session: Session, product_id: int) -> float | None:
    """Returns the best (max) deal_score among product_id's offers' most recent deal row each, or None.

    Purely a read of A4's `deal` table -- deal_score is never recomputed here. Taking the max across the
    product's current offers reflects "the best price currently available for this product right now",
    which is the number a shopper deciding whether to buy cares about; taking the latest row per offer
    (rather than every historical row) avoids a stale deal_score from weeks ago outweighing today's.
    """
    rows = session.execute(
        text(
            "SELECT d.offer_id AS offer_id, d.deal_score AS deal_score, d.computed_at AS computed_at "
            "FROM deal d JOIN offer o ON o.id = d.offer_id WHERE o.product_id = :product_id"
        ),
        {"product_id": product_id},
    ).mappings().all()
    if not rows:
        return None
    latest_per_offer: dict[int, tuple[str, float]] = {}
    for r in rows:
        offer_id = int(r["offer_id"])
        computed = str(r["computed_at"])
        score = float(r["deal_score"])
        if offer_id not in latest_per_offer or computed > latest_per_offer[offer_id][0]:
            latest_per_offer[offer_id] = (computed, score)
    return max(score for _, score in latest_per_offer.values())


def _product_attrs(session: Session, product_id: int) -> dict[str, float]:
    """Returns {key: value_num} from product_attr for product_id, skipping rows with no numeric value."""
    rows = session.execute(
        text('SELECT "key" AS key, value_num FROM product_attr WHERE product_id = :product_id AND value_num IS NOT NULL'),
        {"product_id": product_id},
    ).mappings().all()
    return {str(r["key"]): float(r["value_num"]) for r in rows}


# --------------------------------------------------------------------------------------------------
# score_quality (Zadanie 3 + gate G3)
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityReport:
    """Counts describing one score_quality run: coverage, missing-reason breakdown, and score distribution."""

    computed_at: datetime
    products_considered: int
    products_scored: int
    products_missing: int
    missing_by_reason: dict[str, int] = field(default_factory=dict)
    score_distribution: dict[str, int] = field(default_factory=dict)
    """Counts of total_1_10 bucketed into whole-point bins "1-2", ..., "9-10" (10.0 falls in "9-10")."""

    @property
    def pct_missing(self) -> float | None:
        """Returns the percentage of considered products with no quality score, or None if none were considered."""
        if self.products_considered == 0:
            return None
        return 100.0 * self.products_missing / self.products_considered

    def headline(self) -> str:
        """Returns one plain-language coverage sentence, foregrounding a >=50% miss rate as the main finding (Z4/F4)."""
        pct = self.pct_missing
        if pct is None:
            return "quality: no products in scope"
        base = (
            f"{self.products_scored}/{self.products_considered} products scored "
            f"({pct:.1f}% missing; by reason: {dict(sorted(self.missing_by_reason.items()))})"
        )
        if pct >= 50.0:
            return f"MAIN FINDING: {pct:.1f}% of products have no quality score -- {base}"
        return base

    def summary_line(self) -> str:
        """Returns one human-readable line summarizing this run, in the same spirit as RunReport.summary_line."""
        return f"[score --quality] {self.headline()}; distribution={dict(sorted(self.score_distribution.items()))}"


def _score_bucket(total: float) -> str:
    """Returns the whole-point bucket label ("1-2".."9-10") that `total` (a 1.0-10.0 score) falls into."""
    lo = min(9, max(1, int(total)))
    return f"{lo}-{lo + 1}"


def _reliability_evidence(rel: ReliabilityResult | None) -> dict[str, Any] | None:
    """Returns a JSON-serializable evidence dict for one ReliabilityResult, or None if there isn't one."""
    if rel is None:
        return None
    return {
        "shrunk_rating_0_5": round(rel.shrunk_rating, 4),
        "n_ratings": rel.n_ratings,
        "per_source_avg": {str(k): v for k, v in rel.per_source_avg.items()},
        "modifier": rel.modifier,
        "category_median_m": rel.category_median_m,
        "bayes_c": BAYES_C,
    }


def _risk_evidence(risk: RiskResult | None) -> dict[str, Any] | None:
    """Returns a JSON-serializable evidence dict for one RiskResult, or None if there isn't one."""
    if risk is None:
        return None
    return {
        "badness": round(risk.badness, 4),
        "goodness": round(risk.goodness, 4),
        "warranty_months_avg": risk.warranty_months_avg,
        "seller_rating_avg": risk.seller_rating_avg,
        "source_risk_prior_avg": risk.source_risk_prior_avg,
        "condition_badness": risk.condition_badness,
    }


def _score_one_product(
    session: Session, product_id: int, formulas: dict[str, CategoryFormula], computed_at: datetime
) -> QualityScore:
    """Computes one product's QualityScore, applying gate G3; never raises for missing/insufficient data.

    Always returns a QualityScore -- the pydantic model's own validator enforces Z4 (total_1_10=None implies
    missing_reason is set) for every code path here, including the ones below that return early.
    """
    product_row = session.execute(text("SELECT category_id FROM product WHERE id = :id"), {"id": product_id}).mappings().first()
    if product_row is None:
        return QualityScore(
            product_id=product_id,
            computed_at=computed_at,
            missing_reason="peer_group_too_small",
            evidence={"reason_detail": "product not found"},
        )

    category_id = product_row["category_id"]
    category_path = _leaf_category_path(session, int(category_id)) if category_id is not None else None
    slug = category_path.rsplit("/", 1)[-1] if category_path else None
    formula = formulas.get(slug) if slug else None
    if formula is None:
        return QualityScore(
            product_id=product_id,
            computed_at=computed_at,
            missing_reason="no_utility_formula",
            evidence={"category_id": category_id, "category_path": category_path},
        )

    peer_group = build_peer_group(session, product_id)
    if peer_group is None or len(peer_group.members) < PEER_GROUP_MIN_MEMBERS:
        evidence: dict[str, Any] = {"category_path": category_path}
        confidence: ConfidenceLevel | None = None
        if peer_group is not None:
            evidence["peer_group"] = {
                "band": peer_group.band,
                "reference_price": peer_group.reference_price,
                "members": [{"product_id": m.product_id, "price_total": m.price_total} for m in peer_group.members],
            }
            confidence = peer_group.confidence
        return QualityScore(
            product_id=product_id,
            computed_at=computed_at,
            missing_reason="peer_group_too_small",
            confidence=confidence,
            evidence=evidence,
        )

    peer_ids = [product_id] + [m.product_id for m in peer_group.members]
    price_by_product: dict[int, int] = {product_id: peer_group.reference_price}
    price_by_product.update({m.product_id: m.price_total for m in peer_group.members})
    attrs_by_product = {pid: _product_attrs(session, pid) for pid in peer_ids}
    signals_by_product = {pid: _load_offer_signals(session, pid) for pid in peer_ids}
    category_median_m = _category_median_rating(signals_by_product)

    value_raw: dict[int, float] = {}
    reliability_raw: dict[int, float] = {}
    deal_raw: dict[int, float] = {}
    risk_raw: dict[int, float] = {}
    reliability_detail: ReliabilityResult | None = None
    risk_detail: RiskResult | None = None

    for pid in peer_ids:
        # Zadanie 2.2: "Wartosc -- uzytecznosc NA ZLOTOWKE" -- utility_for returns raw desirability
        # (specs only); dividing by the product's own reference price is what turns that into a
        # value-for-money figure, and is also what makes the required test case true by construction:
        # two products with identical attrs (identical utility) but different prices must end up with
        # different value components, cheaper one higher.
        u = utility_for(formula, attrs_by_product[pid])
        price_zloty = price_by_product[pid] / 100.0
        if u is not None and price_zloty > 0:
            value_raw[pid] = u / price_zloty
        rel = _reliability_raw(signals_by_product[pid], category_median_m)
        if rel is not None:
            reliability_raw[pid] = rel.shrunk_rating
            if pid == product_id:
                reliability_detail = rel
        d = _latest_deal_score(session, pid)
        if d is not None:
            deal_raw[pid] = d / 100.0
        risk = _risk_raw(signals_by_product[pid])
        if risk is not None:
            risk_raw[pid] = risk.goodness
            if pid == product_id:
                risk_detail = risk

    percentiles: dict[str, float | None] = {
        "value": _percentile(value_raw[product_id], list(value_raw.values())) if product_id in value_raw else None,
        "reliability": (
            _percentile(reliability_raw[product_id], list(reliability_raw.values())) if product_id in reliability_raw else None
        ),
        "deal": _percentile(deal_raw[product_id], list(deal_raw.values())) if product_id in deal_raw else None,
        "risk": _percentile(risk_raw[product_id], list(risk_raw.values())) if product_id in risk_raw else None,
    }
    available = {k: v for k, v in percentiles.items() if v is not None}

    peer_group_evidence = {
        "band": peer_group.band,
        "confidence": peer_group.confidence,
        "reference_price": peer_group.reference_price,
        "members": [{"product_id": m.product_id, "price_total": m.price_total} for m in peer_group.members],
    }
    evidence = {
        "category_path": category_path,
        "peer_group": peer_group_evidence,
        "raw": {
            "value": value_raw.get(product_id),
            "reliability": reliability_raw.get(product_id),
            "deal": deal_raw.get(product_id),
            "risk_goodness": risk_raw.get(product_id),
        },
        "percentiles": percentiles,
        "reliability_detail": _reliability_evidence(reliability_detail),
        "risk_detail": _risk_evidence(risk_detail),
        "weights_config": formula.weights.as_dict(),
    }

    if len(available) < 2:
        return QualityScore(
            product_id=product_id,
            computed_at=computed_at,
            peer_group_id=peer_group.peer_group_id,
            value_score=percentiles["value"],
            reliability_score=percentiles["reliability"],
            deal_strength=percentiles["deal"],
            risk_score=percentiles["risk"],
            confidence=peer_group.confidence,
            missing_reason="insufficient_signals",
            evidence=evidence,
        )

    weight_map = formula.weights.as_dict()
    weight_sum = sum(weight_map[k] for k in available)
    if weight_sum <= 0:
        evidence["reason_detail"] = "available component weights sum to 0"
        return QualityScore(
            product_id=product_id,
            computed_at=computed_at,
            peer_group_id=peer_group.peer_group_id,
            value_score=percentiles["value"],
            reliability_score=percentiles["reliability"],
            deal_strength=percentiles["deal"],
            risk_score=percentiles["risk"],
            confidence=peer_group.confidence,
            missing_reason="insufficient_signals",
            evidence=evidence,
        )

    weighted = sum(weight_map[k] * available[k] for k in available) / weight_sum
    total_1_10 = round(1.0 + 9.0 * weighted, 1)
    total_1_10 = max(1.0, min(10.0, total_1_10))
    evidence["renormalized_weights"] = {k: weight_map[k] / weight_sum for k in available}

    return QualityScore(
        product_id=product_id,
        computed_at=computed_at,
        peer_group_id=peer_group.peer_group_id,
        value_score=percentiles["value"],
        reliability_score=percentiles["reliability"],
        deal_strength=percentiles["deal"],
        risk_score=percentiles["risk"],
        total_1_10=total_1_10,
        confidence=peer_group.confidence,
        missing_reason=None,
        evidence=evidence,
    )


def score_quality(session: Session, product_ids: list[int] | None = None, *, config_dir: Path | None = None) -> QualityReport:
    """Computes/writes one quality_score row per product in scope; returns coverage + score-distribution counts.

    "In scope" is `product_ids` if given, else every product currently in the `product` table -- every
    product in scope gets a row, even one with total_1_10=None, so absence is visible in the report rather
    than the product silently disappearing from results (Z4; ARCHITEKTURA.md: "Produkt bez oceny musi
    przejsc przez system i pojawic sie w raporcie"). Applies gate G3: missing_reason is "no_utility_formula"
    (no category_id, or no formula for its leaf category), "peer_group_too_small" (fewer than
    PEER_GROUP_MIN_MEMBERS same-category, same-price-band competitors even at the widest +/-60% band), or
    "insufficient_signals" (fewer than 2 of the 4 components could be computed).
    """
    formulas = load_category_formulas(config_dir)
    computed_at = datetime.now(timezone.utc)

    if product_ids is not None:
        target_ids = list(product_ids)
    else:
        target_ids = [int(r) for r in session.execute(text("SELECT id FROM product ORDER BY id")).scalars().all()]

    products_scored = 0
    missing_by_reason: dict[str, int] = {}
    score_distribution: dict[str, int] = {}
    insert_params: list[dict[str, Any]] = []

    for product_id in target_ids:
        qs = _score_one_product(session, product_id, formulas, computed_at)
        insert_params.append(
            {
                "product_id": qs.product_id,
                "computed_at": qs.computed_at.isoformat(),
                "peer_group_id": qs.peer_group_id,
                "value_score": qs.value_score,
                "reliability_score": qs.reliability_score,
                "deal_strength": qs.deal_strength,
                "risk_score": qs.risk_score,
                "total_1_10": qs.total_1_10,
                "confidence": qs.confidence,
                "missing_reason": qs.missing_reason,
                "evidence_json": json.dumps(qs.evidence, ensure_ascii=False, default=str),
            }
        )
        if qs.total_1_10 is None:
            reason = qs.missing_reason or "unknown"
            missing_by_reason[reason] = missing_by_reason.get(reason, 0) + 1
        else:
            products_scored += 1
            bucket = _score_bucket(qs.total_1_10)
            score_distribution[bucket] = score_distribution.get(bucket, 0) + 1

    if insert_params:
        session.execute(
            text(
                """
                INSERT INTO quality_score (
                    product_id, computed_at, peer_group_id, value_score, reliability_score,
                    deal_strength, risk_score, total_1_10, confidence, missing_reason, evidence_json
                ) VALUES (
                    :product_id, :computed_at, :peer_group_id, :value_score, :reliability_score,
                    :deal_strength, :risk_score, :total_1_10, :confidence, :missing_reason, :evidence_json
                )
                """
            ),
            insert_params,
        )

    return QualityReport(
        computed_at=computed_at,
        products_considered=len(target_ids),
        products_scored=products_scored,
        products_missing=len(target_ids) - products_scored,
        missing_by_reason=missing_by_reason,
        score_distribution=score_distribution,
    )


# --------------------------------------------------------------------------------------------------
# Coverage reporting -- "Raport koncowy": which leaf categories have a formula, and what share of
# offers in the database they cover. Not part of the Kontrakt signature, but needed for the
# completion report and useful to any future CLI/digest.
# --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryCoverage:
    """Per-leaf-category coverage: whether a utility formula exists, and how many current offers are in it."""

    category_path: str
    has_formula: bool
    n_offers: int


def formula_coverage(session: Session, config_dir: Path | None = None) -> list[CategoryCoverage]:
    """Returns per-leaf-category offer counts and formula-existence, sorted by n_offers descending.

    Only counts leaf categories that appear on at least one current `offer` row -- a category with zero
    offers is not useful "missing formula" backlog. This is what the task spec's "Raport koncowy" coverage
    summary and "10 najczestszych kategorii bez formuly" list are built from.
    """
    formulas = load_category_formulas(config_dir)
    rows = session.execute(
        text(
            "SELECT c.path AS path, COUNT(*) AS n FROM offer o JOIN category c ON c.id = o.category_id "
            "GROUP BY c.path ORDER BY n DESC"
        )
    ).mappings().all()
    result: list[CategoryCoverage] = []
    for row in rows:
        path = str(row["path"])
        slug = path.rsplit("/", 1)[-1]
        result.append(CategoryCoverage(category_path=path, has_formula=slug in formulas, n_offers=int(row["n"])))
    return result
