"""EAN sanitization and MPN extraction/normalization (A2 task spec, section 2).

EAN validation itself (checksum, length) is owned by `models.validate_gtin` and enforced automatically by
`Offer`'s own validator -- an Offer constructed with a checksum-invalid EAN has it nulled and a `data_issues`
entry added for free. This module only does the sanitizing *before* that point (feeds hand back EANs with
stray spaces, hyphens, or a trailing ".0" from a spreadsheet export) so a genuinely valid code isn't dropped
just because of formatting noise. It never validates the checksum itself -- that would duplicate A0's logic
and risk drifting from it.
"""

from __future__ import annotations

import re

_NON_DIGIT = re.compile(r"[^0-9]")
_REGIONAL_SUFFIXES = ("/EU", "-EU", "-PL", "/A", "/US", "-US")


def sanitize_ean(raw: object) -> str | None:
    """Strips a raw feed EAN value down to digits only; returns None when nothing digit-like remains.

    Handles common feed noise: "5901234 123457", "5901234-123457", "5901234123457.0" (spreadsheet float
    export), leading/trailing whitespace. Does not check length or checksum -- `Offer` does that.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    text = re.sub(r"\.0+$", "", text)  # trailing ".0"/".00" artifact from a numeric spreadsheet column
    digits = _NON_DIGIT.sub("", text)
    return digits or None


_MPN_CANDIDATE = re.compile(r"\b([A-Z0-9]{2,}(?:[-/][A-Z0-9]+){0,4})\b")
_PURE_DIGITS = re.compile(r"^\d+$")
_PURE_LETTERS = re.compile(r"^[A-Z]+$")
_SPEC_SHORTHAND_SUFFIX = re.compile(r"\d+/\d+(GB|TB|MAH|WH)$")
"""Matches the trailing "<n>/<n>GB" CPU-or-RAM/storage shorthand phones and laptops are listed with
(e.g. "i5/16GB", "8/256GB") -- this is a spec pair attrs.py already owns, never a part number."""


def normalize_mpn(mpn: str) -> str:
    """Returns mpn upper-cased, with '-', '/' and spaces removed and regional suffixes stripped (A2 spec, sect. 2)."""
    value = mpn.strip().upper()
    for suffix in _REGIONAL_SUFFIXES:
        if value.endswith(suffix.upper()):
            value = value[: -len(suffix)]
            break
    return re.sub(r"[-/\s]", "", value)


def extract_mpn_from_title(title: str, *, min_len: int = 5, max_len: int = 20) -> str | None:
    """Finds a plausible manufacturer part number token in `title`; returns it verbatim, or None if unsure.

    Deliberately conservative (A2 spec, section 6: "wieloznaczność rozstrzygaj na niekorzyść ekstrakcji").
    A candidate must mix letters and digits (a pure number or a pure word is never an MPN), fall within a
    plausible length range, and must not itself look like a capacity/unit token (e.g. "512GB", "165HZ")
    that attrs.py already owns. When more than one candidate matches, this returns None rather than guess
    which one is the real part number -- a wrong MPN actively harms the matcher (F2), so silence is safer.
    """
    candidates = []
    for match in _MPN_CANDIDATE.finditer(title.upper()):
        token = match.group(1)
        bare = token.replace("-", "").replace("/", "")
        if not (min_len <= len(bare) <= max_len):
            continue
        if _PURE_DIGITS.match(bare) or _PURE_LETTERS.match(bare):
            continue
        if not (any(c.isdigit() for c in bare) and any(c.isalpha() for c in bare)):
            continue
        if re.fullmatch(r"\d+(GB|TB|MAH|WH|HZ|MM|CM|KG|G|W|V)", bare):
            continue  # a unit-bearing spec value, not a part number
        if _SPEC_SHORTHAND_SUFFIX.search(token):
            continue  # "i5/16GB" CPU/RAM-storage shorthand, not a part number
        candidates.append(token)

    unique = sorted(set(candidates))
    if len(unique) == 1:
        return unique[0]
    return None
