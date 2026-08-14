"""Price parsing: whatever a feed/RSS/API throws at us -> an int number of grosze, or None (Z2, Z4).

Handles the formats ARCHITEKTURA.md and the A2 task spec call out explicitly: "1 299,00 zł", "1299.00 PLN",
"1.299,00", 1299.0, "od 1299 zł" -- plus the classic trap of telling "1.299,00" (Polish: thousands=dot,
decimal=comma) apart from "1,299.00" (US: thousands=comma, decimal=dot) using only the string itself.

Never raises on bad input and never returns 0 for unparseable input -- a price we can't parse is None, not
a fabricated zero (Z4). Every value produced here is a plain int; no float ever represents money.
"""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

_CURRENCY_WORDS = re.compile(r"(?i)\b(zł|zl|pln|eur|usd|gbp|czk)\b|[€$£]")
_LEADING_JUNK = re.compile(r"(?i)^\s*(od|from|cena|price)\s*[:\-]?\s*")
_ALLOWED_CHARS = re.compile(r"[^0-9,.\-]")


def _strip_to_numeric(raw: str) -> str:
    """Returns raw with currency words/symbols, labels, and whitespace removed, keeping only digits/,/./-."""
    cleaned = _LEADING_JUNK.sub("", raw.strip())
    cleaned = _CURRENCY_WORDS.sub("", cleaned)
    cleaned = _ALLOWED_CHARS.sub("", cleaned)
    return cleaned


def _split_integer_and_fraction(cleaned: str) -> tuple[str, str] | None:
    """Splits a digits+separators string into (integer_part, fraction_part) using the PL-vs-US heuristic.

    When both ',' and '.' appear, whichever occurs last is the decimal separator (the other is a thousands
    grouping separator). When only one separator appears, it is treated as decimal iff it is followed by
    exactly 1-2 trailing digits (a plausible cents/grosze count); otherwise it is a thousands separator with
    no fractional part. Returns None when the cleaned string has no digits at all.
    """
    has_comma, has_dot = "," in cleaned, "." in cleaned

    if has_comma and has_dot:
        decimal_sep, thousands_sep = (",", ".") if cleaned.rfind(",") > cleaned.rfind(".") else (".", ",")
        int_part, _, frac_part = cleaned.rpartition(decimal_sep)
        int_part = int_part.replace(thousands_sep, "")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        int_part, _, frac_part = cleaned.rpartition(sep)
        if not (len(frac_part) in (1, 2) and frac_part.isdigit()):
            # Not a plausible decimal remainder (e.g. "1,299" or "12.345") -> grouping separator, no fraction.
            int_part, frac_part = cleaned.replace(sep, ""), ""
    else:
        int_part, frac_part = cleaned, ""

    if not int_part and not frac_part:
        return None
    return int_part or "0", frac_part


def parse_amount_to_grosze(value: Any) -> int | None:
    """Parses a raw feed value (str/int/float/None) as a PLN-denominated amount; returns whole grosze or None.

    Accepts messy source strings ("1 299,00 zł", "1.299,00", "od 1299 zł", ...) as well as bare JSON numbers.
    A bare numeric value is always interpreted as major currency units (e.g. 1299 means 1299.00, not 1299
    grosze) -- feeds and APIs observed in the wild state prices in PLN, not subunits. Negative or otherwise
    nonsensical results return None rather than raising or silently clamping to 0 (Z4).
    """
    if value is None:
        return None
    if isinstance(value, bool):  # bool is an int subclass; never a valid price
        return None
    if isinstance(value, (int, float)):
        try:
            decimal_value = Decimal(str(value))
        except InvalidOperation:
            return None
        return _decimal_to_grosze(decimal_value)
    if not isinstance(value, str):
        return None

    cleaned = _strip_to_numeric(value)
    if not cleaned or cleaned == "-":
        return None
    negative = cleaned.startswith("-")
    if negative:
        cleaned = cleaned[1:]

    split = _split_integer_and_fraction(cleaned)
    if split is None:
        return None
    int_part, frac_part = split
    if not int_part.isdigit() or (frac_part and not frac_part.isdigit()):
        return None

    frac_part = (frac_part + "00")[:2]  # pad/truncate to exactly 2 decimal digits (grosze)
    try:
        decimal_value = Decimal(f"{int_part}.{frac_part}")
    except InvalidOperation:
        return None
    if negative:
        decimal_value = -decimal_value
    return _decimal_to_grosze(decimal_value)


def _decimal_to_grosze(amount: Decimal) -> int | None:
    """Converts a Decimal PLN amount to whole grosze via round-half-up; returns None when the result is negative."""
    grosze = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return grosze if grosze >= 0 else None


_CURRENCY_CODE = re.compile(r"(?i)\b(PLN|EUR|USD|GBP|CZK)\b")
_CURRENCY_SYMBOL = {"€": "EUR", "$": "USD", "£": "GBP", "zł": "PLN", "zl": "PLN"}


def detect_currency(value: Any, *, default: str = "PLN") -> str:
    """Returns the ISO currency code found in a raw price string/field, or `default` when none is recognizable."""
    if not isinstance(value, str):
        return default
    match = _CURRENCY_CODE.search(value)
    if match:
        return match.group(1).upper()
    for symbol, code in _CURRENCY_SYMBOL.items():
        if symbol.lower() in value.lower():
            return code
    return default
