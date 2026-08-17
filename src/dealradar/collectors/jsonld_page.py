"""Collects offers from schema.org JSON-LD embedded in an HTML page.

Why this exists: not every site publishes a usable feed. LowcyDeals.pl serves a valid but *empty* RSS
envelope (last built 2026-05-10, zero items) while its homepage carries ten `@type: Product` nodes with
current prices -- the structured data shops and deal sites publish precisely so machines can read it, which
is what Google Shopping consumes. Reading it needs no browser and no JavaScript: the markup is in the
server-rendered HTML.

Deliberately generic. The same collector serves any page with Product JSON-LD, which is the intended path
for shop catalogues later (a product page reached from a sitemap looks exactly like this to us).

Politeness: inherits BaseCollector's token-bucket rate limiting, retry/backoff and on-disk cache. Only
pages the site's own robots.txt permits should be configured here -- this collector does not defeat any
access control, it reads published markup.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from dealradar.models import RawOffer, SourceKind

from . import BaseCollector

logger = logging.getLogger("dealradar.collectors.jsonld")

_LD_BLOCK = re.compile(
    r"<script[^>]*type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>", re.S | re.I
)


def extract_products(html: str) -> list[dict[str, Any]]:
    """Returns every schema.org Product node found in the page's JSON-LD blocks, in document order.

    Walks nested structures (`@graph`, arrays, products embedded in ItemList) because sites nest these
    inconsistently. A block that fails to parse is skipped rather than aborting the page -- one malformed
    script tag must not cost us the other nine products on the page (Z7).
    """
    products: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            node_type = node.get("@type")
            types = node_type if isinstance(node_type, list) else [node_type]
            if "Product" in types:
                products.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for raw_block in _LD_BLOCK.findall(html):
        try:
            walk(json.loads(raw_block.strip()))
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("skipping malformed JSON-LD block: %s", exc)
    return products


def flatten_product(product: dict[str, Any]) -> dict[str, Any]:
    """Flattens one Product node into a plain dict of the fields A2 normalizes; returns it unparsed.

    Values stay exactly as the site wrote them (Z1: the collector never parses prices or validates
    identifiers) -- this only lifts nested `offers`/`brand` shapes up to top level so a flat YAML field map
    can reach them.
    """
    flat: dict[str, Any] = {}
    for key in ("name", "sku", "mpn", "gtin13", "gtin8", "gtin12", "gtin14", "gtin", "description", "url", "image"):
        value = product.get(key)
        if value is not None and not isinstance(value, (dict, list)):
            flat[key] = value

    brand = product.get("brand")
    if isinstance(brand, dict):
        if brand.get("name"):
            flat["brand"] = brand["name"]
    elif isinstance(brand, str):
        flat["brand"] = brand

    offers = product.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if isinstance(offers, dict):
        for src, dst in (
            ("price", "price"),
            ("priceCurrency", "currency"),
            ("availability", "availability"),
            ("itemCondition", "condition"),
            ("url", "offer_url"),
        ):
            value = offers.get(src)
            if value is not None and not isinstance(value, (dict, list)):
                flat[dst] = value
        seller = offers.get("seller")
        if isinstance(seller, dict) and seller.get("name"):
            flat["seller"] = seller["name"]

    rating = product.get("aggregateRating")
    if isinstance(rating, dict):
        for src, dst in (("ratingValue", "rating"), ("reviewCount", "rating_count"), ("ratingCount", "rating_count")):
            value = rating.get(src)
            if value is not None and dst not in flat:
                flat[dst] = value
    return flat


def product_external_id(flat: dict[str, Any], page_url: str, index: int) -> str:
    """Returns the most stable identifier available for a product: its own URL, else sku/gtin, else position."""
    for key in ("offer_url", "url", "sku", "gtin13", "gtin", "mpn"):
        value = flat.get(key)
        if value:
            return str(value).strip()
    return f"{page_url}#product-{index}"


class JsonLdPageCollector(BaseCollector):
    """Fetches configured pages and yields one RawOffer per schema.org Product found in their JSON-LD."""

    source_name = "jsonld"
    kind: SourceKind = "feed"

    def __init__(
        self,
        *,
        source_id: int,
        source_name: str = "jsonld",
        url: str | None = None,
        urls: list[str] | None = None,
        rps: float = 0.5,
        cache_dir: Path | None = None,
        offline: bool = False,
        **kwargs: Any,
    ) -> None:
        """Configures which page(s) to read; makes no request until fetch() runs."""
        super().__init__(source_id, rps=rps, cache_dir=cache_dir, offline=offline, **kwargs)
        self.source_name = source_name
        self.urls = [u for u in ([url] if url else []) + list(urls or []) if u]

    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:
        """Yields one RawOffer per Product node across every configured page, skipping pages that fail."""
        if not self.urls:
            logger.info("source=%s disabled: no url/urls configured in sources.yaml", self.source_name)
            return
        for page_url in self.urls:
            products = extract_products(self.get(page_url).text)
            if not products:
                logger.warning("source=%s no JSON-LD Product found at %s", self.source_name, page_url)
                continue
            for index, product in enumerate(products):
                flat = flatten_product(product)
                if not flat.get("name"):
                    continue
                flat["_page_url"] = page_url
                yield self.make_raw_offer(product_external_id(flat, page_url, index), flat)
