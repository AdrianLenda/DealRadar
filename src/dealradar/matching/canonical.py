"""Builds a canonical `product` row (and its attrs) from a cluster's member offers. Owner: A3.

Two rules from the task spec drive everything here:
- `canonical_title` is the **medoid** title (highest sum of similarity to the other members), not
  the longest or the first -- a medoid resists being dragged around by one outlier, badly-formatted
  listing (e.g. a Pepper title full of "[Deal] -30%!!!" clutter).
- `product.id` stability: this module never invents a product id itself when a cluster already has
  members pointing at an existing product -- see `majority_existing_product`, used by service.py to
  decide UPDATE vs INSERT.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import TypeVar

from rapidfuzz import fuzz

from dealradar.matching.records import OfferRecord

T = TypeVar("T")


def medoid(members: list[OfferRecord]) -> OfferRecord:
    """Returns the member with the highest sum of title_norm similarity to every other member (ties: lowest id)."""
    if len(members) == 1:
        return members[0]
    n = len(members)
    sums = [0.0] * n
    for i in range(n):
        for j in range(i + 1, n):
            text_a = members[i].title_norm or members[i].title
            text_b = members[j].title_norm or members[j].title
            sim = fuzz.token_set_ratio(text_a, text_b) / 100.0
            sums[i] += sim
            sums[j] += sim
    best_index = max(range(n), key=lambda i: (sums[i], -members[i].id))
    return members[best_index]


def majority_vote(members: list[OfferRecord], getter: Callable[[OfferRecord], T | None]) -> T | None:
    """Returns the most common non-None value of getter(member) across members; ties broken by highest match_confidence.

    Deterministic: Counter preserves first-insertion order for equal counts, and members are always
    passed in offer-id order by the caller, so a further count-and-confidence tie resolves to the
    lowest offer id's value.
    """
    counts: Counter[T] = Counter()
    best_confidence: dict[T, float] = {}
    for member in members:
        value = getter(member)
        if value is None:
            continue
        counts[value] += 1
        confidence = member.match_confidence or 0.0
        if value not in best_confidence or confidence > best_confidence[value]:
            best_confidence[value] = confidence
    if not counts:
        return None
    top_count = max(counts.values())
    tied = [value for value, count in counts.items() if count == top_count]
    if len(tied) == 1:
        return tied[0]
    tied.sort(key=lambda v: best_confidence[v], reverse=True)
    return tied[0]


def merge_attrs(members: list[OfferRecord]) -> dict[str, float]:
    """Returns the union of numeric attrs across members: the medoid's value when it has one, else the mean."""
    center = medoid(members)
    merged: dict[str, float] = dict(center.attrs)
    all_keys: set[str] = set()
    for member in members:
        all_keys |= member.attrs.keys()
    for key in sorted(all_keys - merged.keys()):
        values = [member.attrs[key] for member in members if key in member.attrs]
        merged[key] = sum(values) / len(values)
    return merged


def majority_existing_product(members: list[OfferRecord]) -> int | None:
    """Returns the product_id most represented among members' *current* product_id, or None if none has one yet.

    This is the sole mechanism keeping product.id stable across reruns (task requirement 5): a
    freshly (re)computed cluster is mapped onto whichever existing product its members already
    mostly belonged to, instead of minting a new id.
    """
    return majority_vote(members, lambda m: m.product_id)


def canonicalize(members: list[OfferRecord]) -> tuple[str, str | None, str | None, str | None, int | None]:
    """Returns (canonical_title, ean, mpn, brand, category_id) computed for a cluster's members."""
    center = medoid(members)
    ean = majority_vote(members, lambda m: m.ean)
    mpn = majority_vote(members, lambda m: m.mpn)
    brand = majority_vote(members, lambda m: m.brand) or majority_vote(members, lambda m: m.brand_norm)
    category_id = majority_vote(members, lambda m: m.category_id)
    return center.title, ean, mpn, brand, category_id
