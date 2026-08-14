"""Optional Python hooks for source quirks that a flat key -> key YAML mapping cannot express.

Per the A2 task spec's architecture ("deklaratywne mapowanie pól w YAML + opcjonalny hak w Pythonie"), the
YAML mapping in `mapping.py` covers the common case: a field lives under a differently-named top-level key.
A hook is for when the value is buried in a nested structure (Allegro's `parameters` array of `{name,
value}` dicts) or needs source-specific logic (Pepper's price sometimes only appears inside the title text,
never as its own field). Every hook only *fills gaps* the declarative mapping left empty -- it never
overrides a value the mapping already found.
"""

from __future__ import annotations

import re
from typing import Any, Protocol


class Hook(Protocol):
    """A source-specific post-processing step run after the declarative field mapping."""

    def apply(self, payload: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        """Returns `fields` with any additional canonical values this hook can dig out of `payload`."""
        ...


class AllegroParametersHook:
    """Pulls EAN/brand/condition out of Allegro's `parameters: [{name, value}, ...]` offer-parameter array.

    Allegro's official API returns product identifiers as named parameters rather than top-level fields
    (ARCHITEKTURA.md §6: "oferty często mają EAN w parametrach"); this is exactly the kind of nesting a flat
    YAML key list cannot reach.
    """

    _EAN_PARAM_NAMES = {"ean", "gtin", "kod ean", "ean (gtin)"}
    _BRAND_PARAM_NAMES = {"marka", "producent", "brand"}
    _CONDITION_PARAM_NAMES = {"stan", "condition"}

    def apply(self, payload: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        """Scans payload['parameters'] for EAN/brand/condition parameters, filling only fields not already set."""
        parameters = payload.get("parameters")
        if not isinstance(parameters, list):
            return fields
        for param in parameters:
            if not isinstance(param, dict):
                continue
            name = str(param.get("name", "")).strip().lower()
            value = param.get("value")
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            if name in self._EAN_PARAM_NAMES and "ean" not in fields:
                fields["ean"] = value
            elif name in self._BRAND_PARAM_NAMES and "brand" not in fields:
                fields["brand"] = value
            elif name in self._CONDITION_PARAM_NAMES and "condition_text" not in fields:
                fields["condition_text"] = value
        return fields


_TITLE_PRICE = re.compile(r"(\d[\d\s]{2,7}(?:[.,]\d{1,2})?)\s*(?:zł|PLN)", re.IGNORECASE)


class PepperTitlePriceHook:
    """Falls back to a price embedded in the RSS title/description when Pepper gives no dedicated price field.

    Pepper listings are user-submitted deal posts; the structured price field is sometimes absent while the
    title itself reads like "[Deal] LG UltraGear 27GP850 27\" 165Hz -30% @ 1799 zł" (A1 spec: "Rekord RSS
    jest ubogi ... zapisz całość surowo -- A2 wyciągnie z tego, co się da").
    """

    def apply(self, payload: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        """Fills price_gross from a "<number> zł/PLN" pattern in the title when no price field was mapped."""
        if "price_gross" in fields:
            return fields
        title = fields.get("title") or payload.get("title") or ""
        match = _TITLE_PRICE.search(str(title))
        if match:
            fields["price_gross"] = match.group(1)
        return fields


HOOKS: dict[str, Hook] = {
    "allegro": AllegroParametersHook(),
    "pepper": PepperTitlePriceHook(),
}
"""Source name -> Hook, applied by the pipeline after the declarative mapping. A source with no entry here
runs with the YAML mapping alone -- most sources (plain affiliate feeds) need no Python hook at all."""
