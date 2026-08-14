"""Numeric attribute extraction for gate G2: A2's `offer.evidence["attrs"]` plus a title_norm regex
fallback/supplement. Owner: A3.

Why this exists at all: ARCHITEKTURA.md section 5 ties `product_attr` to `product_id NOT NULL
REFERENCES product(id)`, but matching runs *before* a product exists for a fresh cluster -- there
is nowhere in the original schema to persist a numeric attribute on a bare `offer` row. A2
(normalize) hit this same wall and resolved it by writing extracted attrs into
`Offer.evidence["attrs"]` (a list of `{key, value_num, unit, extracted_from}` dicts) instead of
`product_attr` -- see `parse_evidence_attrs` below, which is the integration point with that.

`extract_numeric_attrs` (this module's own title_norm regex extractor) is kept as a genuine second
source, not just a fallback for when A2's data is absent: A2's documented extraction set (capacity,
energy, screen, mass/length/power -- A2-normalizator.md section 6) does not include `voltage_v`,
which this matcher needs for the EU/US-variant trap (ARCHITEKTURA.md section 7's implicit "region
variant" case). `records.py` merges both, with A2's evidence winning on any overlapping key --
A2's extractor sees the raw feed_field/description text this module never does, so it is the more
trustworthy source wherever both provide a value. See the A3 final report for the full reconciliation
note (this module was written before A2's evidence["attrs"] resolution was confirmed).

Every extractor here follows A2's stated principle (A2-normalizator.md section 6): ambiguity is
resolved against extraction. A missing attribute is a `None` that gate G2 quietly skips; a wrong
attribute would either block a correct match or -- worse -- silently pass one it shouldn't (F2).
"""

from __future__ import annotations

import re
from typing import Any

# Keys with zero tolerance (gate G2 exact_keys in config/matching.yaml) -- capacities, resolutions,
# counts. Keys not in this module's output never appear with a value here; gate logic elsewhere
# only ever compares keys actually present on both sides.
_CAPACITY_GB_RE = re.compile(r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*(tb|gb)(?![a-z])")
_CAPACITY_WH_RE = re.compile(r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*wh(?![a-z])")
_CAPACITY_MAH_RE = re.compile(r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*mah(?![a-z])")
_RESOLUTION_RE = re.compile(r"(?<![a-z0-9])(\d{3,4})\s*[x×]\s*(\d{3,4})(?![a-z0-9])")
_REFRESH_HZ_RE = re.compile(r"(?<![a-z0-9])(\d{2,3})\s*hz(?![a-z])")
_VOLTAGE_V_RE = re.compile(r"(?<![a-z0-9.])(\d{1,3})\s*v(?![a-z0-9])")
_DIAGONAL_IN_RE = re.compile(r"(?<![a-z0-9])(\d{1,2}(?:[.,]\d)?)\s*(?:\"|''|cali|cal)(?![a-z])")
_WEIGHT_KG_RE = re.compile(r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*kg(?![a-z])")
_POWER_W_RE = re.compile(r"(?<![a-z0-9])(\d{1,4})\s*w(?![a-z])")
_DIMS_CM_RE = re.compile(
    r"(?<![a-z0-9])(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*[x×]\s*(\d+(?:[.,]\d+)?)\s*(cm|mm|m)\b"
)

_QUANTITY_WORDS = {
    "dwupak": 2,
    "dwupack": 2,
    "trzypak": 3,
    "czteropak": 4,
    "piecopak": 5,
}
_QUANTITY_PAK_RE = re.compile(r"(?<![a-z0-9])(\d+)[\s-]*(?:pak|pack|szt|sztuk)\b")
_QUANTITY_X_RE = re.compile(r"(?<![a-z0-9])x\s*(\d+)(?![a-z0-9])")

# model_number: the generation/model digits immediately following a recognized product-line word
# (iPhone 15 vs iPhone 13, Galaxy S21 vs S22, RTX 4070 vs 4060, Ryzen 7 vs 9, Core i9 vs i7...).
# Added after discovering (A3 final report) that TF-IDF char n-grams + token_set_ratio alone rate
# "iPhone 13 128GB" and "iPhone 15 128GB" as highly similar -- the only token that differs is the
# generation number, a small fraction of the string -- and gate G2's original key list (capacity,
# resolution, count, dimensions, weight) has no attribute that would ever catch it. This is a
# deliberately curated, not exhaustive, list of common line names; a line this pattern doesn't
# recognize gets no model_number and is gated only by the original attribute set, same as before.
_MODEL_NUMBER_RE = re.compile(
    r"\b(?:iphone|ipad|galaxy\s*s|galaxy\s*note|galaxy\s*a|galaxy\s*z|redmi\s*note|redmi|mi\s*note|"
    r"pixel|rtx|gtx|rx|ryzen|core\s*i|radeon)\D{0,15}?(\d{1,4})\b"
)

# battery_size: found the same failure class one step further while reviewing real (in the A3
# fixture) cluster output -- "Baterie Duracell AA" and "Baterie Duracell AAA" differ by a single
# character and share every other token, so TF-IDF/token_set_ratio rate them as a near-duplicate;
# no existing numeric attribute (capacity/resolution/count/dims/weight) ever fires for either. The
# mapped integers are arbitrary labels, not physical quantities -- gate G2 only ever tests equality
# on this key, never a tolerance band, so the specific numbers don't matter, only that different
# cell types get different ones.
_BATTERY_SIZE_RE = re.compile(r"\b(aaaa|aaa|aa|9v|cr2032|cr2025|cr2016|18650|lr44)\b")
_BATTERY_SIZE_CODES = {"aaaa": 1.0, "aaa": 2.0, "aa": 3.0, "9v": 4.0, "cr2032": 5.0, "cr2025": 6.0, "cr2016": 7.0, "18650": 8.0, "lr44": 9.0}


def _to_float(raw: str) -> float:
    """Returns raw (using ',' or '.' as the decimal separator) parsed as a float."""
    return float(raw.replace(",", "."))


def extract_numeric_attrs(title_norm: str | None) -> dict[str, float]:
    """Returns a dict of attribute_key -> value extracted from title_norm via regex; empty if title_norm is None.

    Extraction is best-effort and precision-first: a pattern that could plausibly mean something
    else (e.g. a bare gram count, or "v2" as a model suffix rather than a voltage) is left out
    rather than guessed at, per A2's "wieloznaczność na niekorzyść ekstrakcji" principle. Only one
    value per key is returned (the first match) -- titles rarely state the same physical quantity
    twice with different values, and if they do, picking either deterministically is better than
    silently overwriting with the last one seen.
    """
    if not title_norm:
        return {}
    text = title_norm.lower()
    attrs: dict[str, float] = {}

    cap_matches = list(_CAPACITY_GB_RE.finditer(text))
    if cap_matches:
        cap_values = [
            _to_float(m.group(1)) * 1024 if m.group(2) == "tb" else _to_float(m.group(1)) for m in cap_matches
        ]
        # Titles routinely state two capacities ("i5/16GB/512GB" = RAM/storage). Storage is almost
        # always the larger of the two in current consumer electronics (RAM tops out well below
        # entry-level SSD/phone storage sizes), so the maximum is the right one to treat as THE
        # capacity for gate G2 -- taking the first match would silently grab RAM instead of
        # storage on exactly the titles where getting this wrong matters most (F2).
        attrs["capacity_gb"] = max(cap_values)

    wh = _CAPACITY_WH_RE.search(text)
    if wh is not None:
        attrs["capacity_wh"] = _to_float(wh.group(1))

    mah = _CAPACITY_MAH_RE.search(text)
    if mah is not None:
        attrs["capacity_mah"] = _to_float(mah.group(1))

    res = _RESOLUTION_RE.search(text)
    if res is not None:
        attrs["resolution_w"] = float(res.group(1))
        attrs["resolution_h"] = float(res.group(2))

    hz = _REFRESH_HZ_RE.search(text)
    if hz is not None:
        value = float(hz.group(1))
        # 50/60 Hz almost always means mains frequency (chargers, PSUs), not a display refresh
        # rate; extracting it as refresh_hz would create false G2 conflicts between a charger and
        # an unrelated 50/60Hz-mentioning accessory. Monitors advertising a genuine 60Hz panel are
        # rare enough in practice that this precision-first trade-off is the right one (Z5).
        if value not in (50.0, 60.0):
            attrs["refresh_hz"] = value

    dims = _DIMS_CM_RE.search(text)
    if dims is not None:
        unit = dims.group(4)
        scale = {"mm": 0.001, "cm": 0.01, "m": 1.0}[unit]
        attrs["length_m"] = _to_float(dims.group(1)) * scale
        attrs["width_m"] = _to_float(dims.group(2)) * scale
        attrs["height_m"] = _to_float(dims.group(3)) * scale

    diag = _DIAGONAL_IN_RE.search(text)
    if diag is not None:
        attrs["diagonal_in"] = _to_float(diag.group(1))

    weight = _WEIGHT_KG_RE.search(text)
    if weight is not None:
        attrs["weight_kg"] = _to_float(weight.group(1))

    volt = _VOLTAGE_V_RE.search(text)
    if volt is not None:
        value = float(volt.group(1))
        # Plausible mains/device voltage range only -- excludes stray "v2"/"v3" model-suffix hits
        # (which regex alone can't tell apart from a real voltage).
        if 3 <= value <= 260:
            attrs["voltage_v"] = value

    power = _POWER_W_RE.search(text)
    if power is not None:
        # The trailing (?![a-z]) lookahead in _POWER_W_RE already keeps this from matching inside
        # "74wh" (the char after 'w' would be 'h', failing the lookahead), so no extra guard needed.
        attrs["power_w"] = float(power.group(1))

    for word, qty in _QUANTITY_WORDS.items():
        if word in text:
            attrs["quantity"] = float(qty)
            break
    else:
        pak = _QUANTITY_PAK_RE.search(text)
        if pak is not None:
            attrs["quantity"] = float(pak.group(1))
        else:
            xq = _QUANTITY_X_RE.search(text)
            if xq is not None:
                attrs["quantity"] = float(xq.group(1))

    model = _MODEL_NUMBER_RE.search(text)
    if model is not None:
        attrs["model_number"] = float(model.group(1))

    battery = _BATTERY_SIZE_RE.search(text)
    if battery is not None:
        attrs["battery_size"] = _BATTERY_SIZE_CODES[battery.group(1)]

    return attrs


def parse_evidence_attrs(evidence: dict[str, Any] | None) -> dict[str, float]:
    """Returns {key: value_num} from A2's `offer.evidence["attrs"]` list, skipping entries with no numeric value.

    A2's contract (per the A3<->A2 integration note, since `product_attr` cannot be populated
    pre-match -- see this module's docstring): `evidence["attrs"]` is a list of
    `{"key": str, "value_num": float | None, "unit": str | None, "extracted_from": str | None}`
    dicts. Anything malformed (wrong types, missing "key") is skipped rather than raising --
    evidence is data from another stage, not a contract this function should crash on.
    """
    if not evidence:
        return {}
    raw_attrs = evidence.get("attrs")
    if not isinstance(raw_attrs, list):
        return {}
    result: dict[str, float] = {}
    for entry in raw_attrs:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        value = entry.get("value_num")
        if isinstance(key, str) and key and isinstance(value, (int, float)) and not isinstance(value, bool):
            result[key] = float(value)
    return result


def attrs_for_offer(title_norm: str | None, evidence: dict[str, Any] | None) -> dict[str, float]:
    """Returns the numeric attrs gate G2 should use for one offer: this module's title_norm regex extraction,

    overridden key-by-key by A2's `evidence["attrs"]` where A2 provides a value. A2 sees more of
    the raw record (feed_field, description) than a title_norm regex ever can, so it wins on any
    key both sources produce; this module's extractor's only exclusive contribution in practice is
    `voltage_v`, which A2's documented extraction set does not cover.
    """
    merged = extract_numeric_attrs(title_norm)
    merged.update(parse_evidence_attrs(evidence))
    return merged
