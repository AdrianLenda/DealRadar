"""Numeric attribute extraction: regex + unit parsing, fuel for the matcher's attribute gate G2 (A2 spec, sect. 6).

Precision matters more than coverage here. A wrong attribute is worse than a missing one: it either blocks
a correct match (G2 rejects "512GB" vs "512GB" because one side was mis-extracted as 512 from something
else) or, worse, waves through an incorrect one. Every extraction rule below is written to fail closed:
when a title is ambiguous (phone "8/256GB" RAM-vs-storage shorthand, "5G" network generation vs. "5 g"
mass), the rule returns nothing rather than guessing.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel

from dealradar.models import ExtractedFrom

AttrKey = Literal[
    "capacity_gb",
    "ram_gb",
    "capacity_wh",
    "capacity_mah",
    "diagonal_in",
    "refresh_hz",
    "resolution_px_w",
    "resolution_px_h",
    "mass_kg",
    "length_m",
    "power_w",
]


class ExtractedAttr(BaseModel):
    """One regex-extracted numeric (or text) product attribute, mirroring the shape of the `product_attr` table."""

    key: AttrKey
    value_num: float | None = None
    value_text: str | None = None
    unit: str | None = None
    extracted_from: ExtractedFrom


def _to_float(raw: str) -> float:
    """Parses a PL/EN-formatted decimal token ("1,5", "1.5") into a float; assumes `raw` is already digits+sep."""
    return float(raw.replace(",", "."))


# --------------------------------------------------------------------------------------------------
# Storage capacity: GB / TB, with the RAM/storage "8/256GB" phone-spec shorthand handled explicitly
# and everything else involving a slash between two numbers left unextracted (ambiguous).
# --------------------------------------------------------------------------------------------------

_RAM_STORAGE_SHORTHAND = re.compile(r"(?<!\d)(\d{1,2})\s*/\s*(\d{2,5})\s*GB\b", re.IGNORECASE)
_SLASH_NUMBER_GB = re.compile(r"\d+\s*/\s*\d+\s*(GB|TB)\b", re.IGNORECASE)
_CAPACITY = re.compile(r"(?<![\d/])(\d{1,5}(?:[.,]\d)?)\s*(TB|GB)\b(?!\s*/)", re.IGNORECASE)


def _extract_capacity(text: str, extracted_from: ExtractedFrom) -> list[ExtractedAttr]:
    """Extracts storage capacity_gb (and ram_gb, for the "8/256GB" phone-spec shorthand) from `text`."""
    results: list[ExtractedAttr] = []

    shorthand = _RAM_STORAGE_SHORTHAND.search(text)
    if shorthand:
        ram, storage = int(shorthand.group(1)), int(shorthand.group(2))
        if 1 <= ram <= 32 and storage >= 16:  # plausible phone/tablet RAM vs. storage magnitudes
            results.append(ExtractedAttr(key="ram_gb", value_num=float(ram), unit="GB", extracted_from=extracted_from))
            results.append(
                ExtractedAttr(key="capacity_gb", value_num=float(storage), unit="GB", extracted_from=extracted_from)
            )
            return results

    for match in _CAPACITY.finditer(text):
        span_text = text[max(0, match.start() - 6) : match.end() + 3]
        if _SLASH_NUMBER_GB.search(span_text):
            continue  # part of an ambiguous "N/M GB" shorthand we didn't confidently resolve above
        value = _to_float(match.group(1))
        unit = match.group(2).upper()
        gb_value = value * 1000 if unit == "TB" else value
        results.append(ExtractedAttr(key="capacity_gb", value_num=gb_value, unit="GB", extracted_from=extracted_from))
        break  # first unambiguous capacity token only; a second one is more likely noise than a second attr
    return results


# --------------------------------------------------------------------------------------------------
# Energy: Wh direct, or mAh (+ optional voltage -> Wh); mAh alone is kept as its own attribute rather
# than guessing a voltage.
# --------------------------------------------------------------------------------------------------

_WH = re.compile(r"(\d{1,3}(?:[.,]\d)?)\s*Wh\b")
_MAH_WITH_VOLTAGE = re.compile(r"(\d{3,6})\s*mAh\D{0,10}?(\d{1,2}(?:[.,]\d)?)\s*V\b", re.IGNORECASE)
_MAH = re.compile(r"(\d{3,6})\s*mAh\b", re.IGNORECASE)


def _extract_energy(text: str, extracted_from: ExtractedFrom) -> list[ExtractedAttr]:
    """Extracts battery energy: capacity_wh when stated directly or derivable from mAh+voltage, else capacity_mah."""
    wh_match = _WH.search(text)
    if wh_match:
        return [ExtractedAttr(key="capacity_wh", value_num=_to_float(wh_match.group(1)), unit="Wh", extracted_from=extracted_from)]

    with_voltage = _MAH_WITH_VOLTAGE.search(text)
    if with_voltage:
        mah, volts = float(with_voltage.group(1)), _to_float(with_voltage.group(2))
        wh = round(mah * volts / 1000, 2)
        return [ExtractedAttr(key="capacity_wh", value_num=wh, unit="Wh", extracted_from=extracted_from)]

    mah_match = _MAH.search(text)
    if mah_match:
        # No voltage given -> converting to Wh would require assuming a cell chemistry/voltage we don't
        # know. Keep the raw mAh as its own attribute instead of fabricating an energy figure.
        return [ExtractedAttr(key="capacity_mah", value_num=float(mah_match.group(1)), unit="mAh", extracted_from=extracted_from)]
    return []


# --------------------------------------------------------------------------------------------------
# Screen: diagonal inches, refresh rate, resolution.
# --------------------------------------------------------------------------------------------------

_DIAGONAL = re.compile(r"(\d{1,2}(?:[.,]\d)?)\s*(?:\"|cali|cal\b)", re.IGNORECASE)
_REFRESH = re.compile(r"(\d{2,3})\s*Hz\b", re.IGNORECASE)
_RESOLUTION = re.compile(r"\b(\d{3,5})\s*[xX×]\s*(\d{3,5})\b")


def _extract_screen(text: str, extracted_from: ExtractedFrom) -> list[ExtractedAttr]:
    """Extracts screen diagonal_in, refresh_hz, and resolution_px_w/h from `text`."""
    results: list[ExtractedAttr] = []
    diagonal = _DIAGONAL.search(text)
    if diagonal:
        results.append(ExtractedAttr(key="diagonal_in", value_num=_to_float(diagonal.group(1)), unit="in", extracted_from=extracted_from))
    refresh = _REFRESH.search(text)
    if refresh:
        results.append(ExtractedAttr(key="refresh_hz", value_num=float(refresh.group(1)), unit="Hz", extracted_from=extracted_from))
    resolution = _RESOLUTION.search(text)
    if resolution:
        w, h = int(resolution.group(1)), int(resolution.group(2))
        if 200 <= w <= 10000 and 200 <= h <= 10000:  # plausible pixel dimensions, guards against e.g. "10x20cm"
            results.append(ExtractedAttr(key="resolution_px_w", value_num=float(w), unit="px", extracted_from=extracted_from))
            results.append(ExtractedAttr(key="resolution_px_h", value_num=float(h), unit="px", extracted_from=extracted_from))
    return results


# --------------------------------------------------------------------------------------------------
# Mass, length, power. Mass deliberately requires a lower-case "g"/"kg" unit token so "5G"/"4G" mobile
# network generation mentions (always upper-case in the wild) never get misread as grams (F2-adjacent trap).
# --------------------------------------------------------------------------------------------------

_MASS = re.compile(r"(?<![a-zA-Z])(\d{1,4}(?:[.,]\d+)?)\s*(kg|g)(?![a-zA-Z])")
_LENGTH = re.compile(r"(\d{1,3}(?:[.,]\d+)?)\s*m(?![a-zA-Z])")
_POWER = re.compile(r"\b(\d{1,4})\s*W\b(?!h)")


def _extract_physical(text: str, extracted_from: ExtractedFrom) -> list[ExtractedAttr]:
    """Extracts mass_kg, length_m, and power_w from `text`, guarding each against its common false-positive."""
    results: list[ExtractedAttr] = []

    mass = _MASS.search(text)
    if mass:
        value, unit = _to_float(mass.group(1)), mass.group(2)
        kg_value = value if unit == "kg" else value / 1000
        results.append(ExtractedAttr(key="mass_kg", value_num=kg_value, unit="kg", extracted_from=extracted_from))

    length = _LENGTH.search(text)
    if length:
        value = _to_float(length.group(1))
        if value <= 20:  # a "length" beyond this is almost certainly a mis-tagged different unit/typo
            results.append(ExtractedAttr(key="length_m", value_num=value, unit="m", extracted_from=extracted_from))

    power = _POWER.search(text)
    if power:
        results.append(ExtractedAttr(key="power_w", value_num=float(power.group(1)), unit="W", extracted_from=extracted_from))

    return results


def extract_attrs(
    *,
    title: str,
    description: str | None = None,
    feed_fields: dict[str, str] | None = None,
) -> list[ExtractedAttr]:
    """Extracts every numeric attribute this module recognizes from a title/description/feed-field payload.

    Order of precedence for `extracted_from`: dedicated feed fields (most trustworthy, structured data)
    first, then title, then description -- matching the A2 task spec's intent that `extracted_from` let
    later analysis measure extraction quality per field type. A key already found in a higher-precedence
    source is not re-derived from a lower one, so results never contradict themselves.
    """
    found: dict[AttrKey, ExtractedAttr] = {}

    def _merge(attrs: list[ExtractedAttr]) -> None:
        for attr in attrs:
            found.setdefault(attr.key, attr)

    if feed_fields:
        combined_feed_text = " ".join(str(v) for v in feed_fields.values() if v)
        if combined_feed_text:
            _merge(_extract_capacity(combined_feed_text, "feed_field"))
            _merge(_extract_energy(combined_feed_text, "feed_field"))
            _merge(_extract_screen(combined_feed_text, "feed_field"))
            _merge(_extract_physical(combined_feed_text, "feed_field"))

    _merge(_extract_capacity(title, "title"))
    _merge(_extract_energy(title, "title"))
    _merge(_extract_screen(title, "title"))
    _merge(_extract_physical(title, "title"))

    if description:
        _merge(_extract_capacity(description, "description"))
        _merge(_extract_energy(description, "description"))
        _merge(_extract_screen(description, "description"))
        _merge(_extract_physical(description, "description"))

    return list(found.values())
