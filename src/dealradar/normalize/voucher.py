"""Detects listings that are a discount, coupon or cashback rather than a product with a price.

Why this needs to exist, from real Pepper data: a post titled "10 zł zniżki na zamówienie w Foodsi"
arrives with `merchant_attrib = {"price": "10zł"}` -- Pepper puts the *voucher's value* in the same
structured price field a normal deal uses for the product's price. Nothing downstream can tell the two
apart, so without this check the pipeline invents a ten-złoty product, clusters it with whatever else is
cheap, and poisons the median every later discount is measured against (F1/F2).

The bias here is deliberate and matches Z5's logic: a fabricated product is a lie in the report, while a
dropped voucher costs nothing, because a voucher has no price history to track in the first place. So when
a title reads like a voucher, we reject it -- and record `not_a_product_offer` so the drop is auditable in
`rejected_offer` rather than silently absorbed (Z4).
"""

from __future__ import annotations

import re

_AMOUNT_THEN_DISCOUNT = re.compile(
    r"\d[\d\s]*(?:[.,]\d{1,2})?\s*(?:zł|zl|PLN|%)\s*"
    r"(?:zni[żz]ki|zni[żz]ka|rabatu|rabat|taniej|cashback|zwrotu|mniej)",
    re.IGNORECASE,
)
"""An amount immediately followed by a discount word: "10 zł zniżki", "20 zł rabatu", "15% taniej"."""

_VOUCHER_NOUNS = re.compile(
    r"(kod(?:y|em|u)?\s+rabatow|kupon|bon\s+(?:podarunkow|zakupow)|karta\s+podarunkow"
    r"|voucher|gift\s*card|darmowa\s+dostawa|za\s+darmo)",
    re.IGNORECASE,
)
"""Nouns naming the voucher itself rather than any product: coupon codes, gift cards, free delivery."""


def looks_like_voucher(title: str, description: str | None = None) -> bool:
    """Returns True when this listing offers a discount/coupon rather than a priced product.

    Checks the title first and only falls back to the description's opening, because a description often
    mentions an unrelated promotion further down ("...also 10 zł off your next order"), which says nothing
    about what this particular listing is.
    """
    haystack = title or ""
    if _AMOUNT_THEN_DISCOUNT.search(haystack) or _VOUCHER_NOUNS.search(haystack):
        return True
    if description:
        opening = description[:120]
        return bool(_AMOUNT_THEN_DISCOUNT.search(opening) or _VOUCHER_NOUNS.search(opening))
    return False
