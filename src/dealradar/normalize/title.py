"""title_norm: lowercase, de-junked title text for the fuzzy matcher (A3) to run TF-IDF/RapidFuzz over.

Polish diacritics are deliberately preserved -- transliterating "słuchawki" to "sluchawki" would make it
diverge from every correctly-accented competitor title and quietly wreck TF-IDF cosine similarity (A2 task
spec, section 7). Only junk that carries no product-identifying signal is stripped: marketing decoration,
emoji, boilerplate phrases every seller stamps on every listing.
"""

from __future__ import annotations

import re
import unicodedata

_JUNK_PHRASES = (
    r"nowy",
    r"nowe",
    r"gwarancj\w*",
    r"fv\s*(?:23)?%?",
    r"faktura\s*vat",
    r"promocj\w*",
    r"wysy[łl]ka\s*24\s*h",
    r"wysy[łl]ka\s*gratis",
    r"darmowa\s*wysy[łl]ka",
    r"raty\s*\d*%?",
    r"najta[nń]sz\w*",
    r"hit",
    r"okazj\w*",
    r"super\s*cena",
)
_JUNK_PATTERN = re.compile(r"(?i)\b(" + "|".join(_JUNK_PHRASES) + r")\b")

_BRACKETED_TAG = re.compile(r"[\[\(]\s*(deal|gorące|gorace|promocja|okazja)\s*[\]\)]", re.IGNORECASE)
_PERCENT_OFF = re.compile(r"-?\d{1,3}\s*%")
_PUNCT_RUN = re.compile(r"[!?]{2,}")
_WHITESPACE = re.compile(r"\s+")


def _strip_decorative(text: str) -> str:
    """Removes emoji and other decorative (non-letter/digit/space/basic-punct) Unicode symbols from `text`."""
    kept = []
    for ch in text:
        category = unicodedata.category(ch)
        if category.startswith(("L", "N", "Z")) or ch in "-/.,%\"'+":
            kept.append(ch)
        # Symbol (So), emoji, box-drawing, etc. are dropped silently -- they carry no matching signal.
    return "".join(kept)


def normalize_title(title: str) -> str:
    """Returns title_norm: lowercased, de-emoji'd, junk-phrase-stripped, whitespace-collapsed; diacritics kept."""
    text = _BRACKETED_TAG.sub(" ", title)
    text = _PERCENT_OFF.sub(" ", text)
    text = _strip_decorative(text)
    text = _PUNCT_RUN.sub(" ", text)
    text = _JUNK_PATTERN.sub(" ", text)
    text = text.lower()
    text = _WHITESPACE.sub(" ", text).strip()
    return text
