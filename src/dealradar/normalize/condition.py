"""Condition and bundle detection from title/description text (A2 task spec, section 4 -- the F2 defense).

Comparing a poleasing unit to a new one, or a single item to a 2-pack, is exactly the failure mode (F2) the
matcher's hard gates exist to block -- but the gates only work if `condition`, `is_bundle`, and `bundle_size`
are set correctly here first.
"""

from __future__ import annotations

import re

from dealradar.models import Condition

_CONDITION_PHRASES: tuple[tuple[str, Condition], ...] = (
    (r"poleasing\w*", "refurb"),
    (r"powystawow\w*", "refurb"),
    (r"refurbished", "refurb"),
    (r"odnowion\w*", "refurb"),
    (r"outlet", "refurb"),
    (r"open\s*box", "used"),
    (r"uzywan\w*|używan\w*", "used"),
    (r"uszkodzon\w*", "damaged"),
)
_CONDITION_PATTERNS: tuple[tuple[re.Pattern[str], Condition], ...] = tuple(
    (re.compile(rf"(?i)\b{phrase}\b"), condition) for phrase, condition in _CONDITION_PHRASES
)


def detect_condition_phrase(text: str) -> Condition | None:
    """Scans `text` for an explicit condition phrase (poleasingowy, uszkodzony, ...); returns it, or None if none found.

    Returns only the classes with a positive textual signal (refurb/used/damaged). "new" is never inferred
    from text here -- callers decide whether the *absence* of a phrase means "new" or "unknown" based on
    whether the source is one that reliably declares condition (see A2 task spec: absence on a marketplace
    like Allegro must stay None, not default to "new").
    """
    for pattern, condition in _CONDITION_PATTERNS:
        if pattern.search(text):
            return condition
    return None


def resolve_condition(text: str, *, assume_new_if_undeclared: bool) -> Condition | None:
    """Returns the condition declared in `text`, or "new"/None for undeclared text depending on the source's policy.

    `assume_new_if_undeclared` should be True only for sources whose catalog is first-party retail (feeds,
    iBood, Pepper reposting a retailer) where "no condition phrase" reliably means new stock. It must be
    False for peer-to-peer marketplaces (Allegro) where a seller silently omitting "used" is common and an
    unstated condition is genuinely unknown, not new (A2 task spec, section 4).
    """
    phrase = detect_condition_phrase(text)
    if phrase is not None:
        return phrase
    return "new" if assume_new_if_undeclared else None


_BUNDLE_WITH_COUNT = re.compile(r"(?i)\b(\d+)[\s-]*(?:pak|pack|szt\.?|sztuk|x)\b|\bx\s*(\d+)\b")
_BUNDLE_PHRASE_ONLY = re.compile(r"(?i)\b(zestaw\w*|komplet\w*|dwupak\w*)\b")
"""\w* suffixes tolerate regular Polish inflection (zestawie, zestawu, kompletem); irregular consonant
alternation (komplet -> komplecie) is not covered -- a rare miss judged an acceptable trade for staying a
plain regex instead of a Polish morphology engine (A2 spec: prefer under- to over-extraction anyway)."""


def detect_bundle(text: str) -> tuple[bool, int | None]:
    """Detects a multi-item listing in `text`; returns (is_bundle, bundle_size), with bundle_size None if unclear.

    A recognized count ("2-pak", "3 szt", "x2") gives an exact bundle_size. A bare bundle phrase with no
    parseable count ("zestaw", "komplet" of unlike items) still sets is_bundle=True but leaves bundle_size
    None, per the A2 task spec: the matcher must refuse to merge it with anything rather than guess a size.
    """
    count_match = _BUNDLE_WITH_COUNT.search(text)
    if count_match:
        digits = count_match.group(1) or count_match.group(2)
        size = int(digits)
        if size >= 2:
            return True, size
    if "dwupak" in text.lower() or "2-pak" in text.lower():
        return True, 2
    if _BUNDLE_PHRASE_ONLY.search(text):
        return True, None
    return False, None
