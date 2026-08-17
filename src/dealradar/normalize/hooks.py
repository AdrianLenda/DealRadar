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

from dealradar.normalize.mapping import source_family


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

_DISCOUNT_CONTEXT = re.compile(
    r"^\W*(zni[żz]k|rabat|tanie|mniej|off\b|kod|kupon|bon|cashback|zwrot)", re.IGNORECASE
)
"""Words that, right after an amount, mean it is money *taken off* rather than the price. Pepper carries a
lot of "10 zł zniżki na zamówienie" voucher posts; reading that 10 zł as a product price invents a
ten-złoty product and poisons the median for whatever it gets clustered with."""


class PepperHook:
    """Fills price and shop for Pepper's RSS, which hides both in an element's XML attributes.

    A Pepper `<item>` carries `<pepper:merchant name="InPost" price="38,98zł"/>`, which the collector
    flattens to `merchant_attrib = {"name": ..., "price": ...}`. That structured pair is far better than
    guessing from prose: it is the actual deal price, and `name` is the shop -- the only cross-shop signal
    this source gives us at all. Reading the title was the original approach and it rejected ~95% of real
    items for having no parseable price.

    The title regex survives only as a last resort, now refusing amounts that read as a discount.
    """

    def apply(self, payload: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        """Fills price_gross/seller from merchant_attrib, falling back to a non-discount amount in the title."""
        merchant = payload.get("merchant_attrib")
        if isinstance(merchant, dict):
            price = merchant.get("price")
            if "price_gross" not in fields and isinstance(price, str) and price.strip():
                fields["price_gross"] = price
            name = merchant.get("name")
            if "seller" not in fields and isinstance(name, str) and name.strip():
                fields["seller"] = name.strip()

        if "price_gross" not in fields:
            title = str(fields.get("title") or payload.get("title") or "")
            match = _TITLE_PRICE.search(title)
            if match and not _DISCOUNT_CONTEXT.match(title[match.end() :]):
                fields["price_gross"] = match.group(1)
        return fields


_PLN_IN_TITLE = re.compile(r"~?\s*(\d[\d\s]{0,7}(?:[.,]\d{1,2})?)\s*z[łl]\b", re.IGNORECASE)
_ALIEXPRESS_ITEM = re.compile(r"aliexpress\.com/item/(\d{6,20})", re.IGNORECASE)


class LowcyChinHook:
    """Extracts the PLN price and the AliExpress item id from a LowcyChin post.

    Its titles quote both currencies -- "Projektor Magcubic HY300 Pro za $27.37 / ~104zł" -- and we take
    the PLN figure deliberately: the dollar amount would need an FX rate as of the day the post was
    written, which we do not have and would have to guess (Z4).

    The AliExpress item id lifted out of the post body is the nearest thing to a stable identifier this
    source offers. Without it two posts about the same lamp months apart are just two similar strings; with
    it they are one product with a price history, which is the entire point of tracking this source.
    """

    def apply(self, payload: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
        """Fills price_gross from the title's PLN amount and mpn from the AliExpress item id, gaps only."""
        if "price_gross" not in fields:
            title = str(fields.get("title") or payload.get("title") or "")
            match = _PLN_IN_TITLE.search(title)
            if match and not _DISCOUNT_CONTEXT.match(title[match.end() :]):
                fields["price_gross"] = match.group(1)

        if "mpn" not in fields:
            blob = " ".join(
                str(payload.get(key, "")) for key in ("encoded", "description", "link", "guid")
            )
            item = _ALIEXPRESS_ITEM.search(blob)
            if item:
                # Namespaced so it can never be confused with a manufacturer part number from another
                # source: this identifies a listing on AliExpress, not a part in the world.
                fields["mpn"] = f"ALIEXPRESS-{item.group(1)}"
        return fields


HOOKS: dict[str, Hook] = {
    "allegro": AllegroParametersHook(),
    "pepper": PepperHook(),
    "lowcychin": LowcyChinHook(),
}
"""Source name -> Hook, applied by the pipeline after the declarative mapping. A source with no entry here
runs with the YAML mapping alone -- most sources (plain affiliate feeds) need no Python hook at all."""


def resolve_hook(source_name: str) -> Hook | None:
    """Returns the hook for `source_name`, letting per-channel variants inherit their site's hook; None if none.

    Mirrors the family step in `mapping.NormalizerConfigRegistry.resolve`, so `pepper_elektronika` gets
    Pepper's hook instead of silently running with none.
    """
    exact = HOOKS.get(source_name)
    if exact is not None:
        return exact
    return HOOKS.get(source_family(source_name))
