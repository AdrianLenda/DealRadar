"""Blocking for cascade step 3 -- ARCHITEKTURA.md section 7: "bez tego masz porownanie N^2". Owner: A3.

Block key is (brand_norm, category_id): both must be present, or the offer sits out step 3 entirely
("Oferta bez marki lub bez kategorii nie wchodzi do kroku 3 -- zostaje samotna. To celowe."). The
price-bucket part of the key ("+/-35% wokol ceny oferty") is inherently a sliding, per-offer window
rather than a fixed partition, so it is applied as a pairwise filter within each (brand, category)
group instead of a third grouping key -- see the A3 final report for why a fixed-width bucket would
create boundary artifacts (two nearly-identical prices landing in different buckets).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from dealradar.matching.records import OfferRecord


def group_for_step3(records: Iterable[OfferRecord]) -> dict[tuple[str, int], list[OfferRecord]]:
    """Returns offers eligible for step 3 (non-empty brand_norm and non-null category_id) grouped by (brand_norm, category_id)."""
    groups: dict[tuple[str, int], list[OfferRecord]] = defaultdict(list)
    for record in records:
        if record.brand_norm and record.category_id is not None:
            groups[(record.brand_norm, record.category_id)].append(record)
    return groups


def price_compatible(price_a: int, price_b: int, price_bucket_pct: float) -> bool:
    """Returns True iff the higher of price_a/price_b is within price_bucket_pct of the lower (the +/-35% window)."""
    low, high = (price_a, price_b) if price_a <= price_b else (price_b, price_a)
    if low <= 0:
        return high == low
    return high <= low * (1 + price_bucket_pct)
