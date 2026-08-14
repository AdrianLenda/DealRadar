"""AliExpress affiliate API collector -- optional, per ARCHITEKTURA.md sections 6/7.

Requires `ALIEXPRESS_APP_KEY` / `ALIEXPRESS_APP_SECRET` (affiliate account approval, not just an API key --
see ARCHITEKTURA.md section 6). When either is unset, `fetch()` logs a notice and yields nothing rather than
raising, so a run with this source enabled but unconfigured degrades gracefully instead of failing the batch.

Note on scope: AliExpress's Open Platform API expects requests signed with `app_secret` using their own
(external, legacy) MD5 signing scheme -- that signing logic below is dictated by their API contract, not a
security or anti-bot workaround of ours. No AliExpress credentials are available in this environment, so only
the credential-missing path below is exercised by tests; the signed-request path is implemented per their
published contract but has not been run against the live API. See the A1 final report.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from dealradar.config import get_secret
from dealradar.models import RawOffer, SourceKind

from . import BaseCollector

logger = logging.getLogger("dealradar.collectors.aliexpress")

API_URL = "https://api-sg.aliexpress.com/sync"
METHOD = "aliexpress.affiliate.product.query"


class AliexpressCollector(BaseCollector):
    """Searches AliExpress's affiliate product-query API and yields raw products; disables itself without creds."""

    source_name = "aliexpress"
    kind: SourceKind = "api"

    def __init__(
        self,
        *,
        source_id: int,
        source_name: str = "aliexpress",
        keywords: list[str] | None = None,
        category_ids: list[str] | None = None,
        page_size: int = 50,
        max_pages: int = 5,
        rps: float = 1.0,
        cache_dir: Path | None = None,
        offline: bool = False,
        **kwargs: Any,
    ) -> None:
        """Configures search parameters; checks no credentials until fetch() runs."""
        super().__init__(source_id, rps=rps, cache_dir=cache_dir, offline=offline, **kwargs)
        self.source_name = source_name
        self.keywords = list(keywords) if keywords else []
        self.category_ids = list(category_ids) if category_ids else []
        self.page_size = page_size
        self.max_pages = max_pages

    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:
        """Yields one RawOffer per product found; yields nothing (after logging) when credentials are unset."""
        creds = self._credentials()
        if creds is None:
            logger.info(
                "source=%s disabled: no credentials (ALIEXPRESS_APP_KEY/ALIEXPRESS_APP_SECRET unset)",
                self.source_name,
            )
            return
        app_key, app_secret = creds
        for keyword in self.keywords or [""]:
            yield from self._fetch_keyword(app_key, app_secret, keyword)

    def _credentials(self) -> tuple[str, str] | None:
        """Returns (app_key, app_secret) from the environment, or None if either is unset."""
        app_key = get_secret("ALIEXPRESS_APP_KEY")
        app_secret = get_secret("ALIEXPRESS_APP_SECRET")
        if not app_key or not app_secret:
            return None
        return app_key, app_secret

    def _fetch_keyword(self, app_key: str, app_secret: str, keyword: str) -> Iterator[RawOffer]:
        """Pages through the product-query endpoint for one keyword, yielding products until a short page ends it."""
        for page in range(1, self.max_pages + 1):
            params = self._signed_params(app_key, app_secret, keyword, page)
            response = self.get(API_URL, params)
            data = response.json()
            products = _extract_products(data if isinstance(data, dict) else {})
            if not products:
                return
            for product in products:
                product_id = str(product.get("product_id") or product.get("productId") or "").strip()
                if not product_id:
                    continue
                yield self.make_raw_offer(product_id, product)
            if len(products) < self.page_size:
                return

    def _signed_params(self, app_key: str, app_secret: str, keyword: str, page: int) -> dict[str, Any]:
        """Builds AliExpress Open Platform request params with an MD5 signature, per their required signing scheme."""
        params: dict[str, Any] = {
            "app_key": app_key,
            "method": METHOD,
            "sign_method": "md5",
            "timestamp": str(int(time.time() * 1000)),
            "page_no": page,
            "page_size": self.page_size,
            "target_currency": "PLN",
            "target_language": "PL",
        }
        if keyword:
            params["keywords"] = keyword
        if self.category_ids:
            params["category_ids"] = ",".join(self.category_ids)
        base = "".join(f"{key}{params[key]}" for key in sorted(params))
        signature = hashlib.md5(f"{app_secret}{base}{app_secret}".encode("utf-8")).hexdigest().upper()
        params["sign"] = signature
        return params


def _extract_products(data: dict[str, Any]) -> list[dict[str, Any]]:
    """Digs the product list out of AliExpress's nested response envelope; returns [] if the shape doesn't match."""
    try:
        result = data["aliexpress_affiliate_product_query_response"]["resp_result"]["result"]
        products = result.get("products", {}).get("product", [])
    except (KeyError, TypeError, AttributeError):
        return []
    if isinstance(products, dict):
        return [products]
    return list(products) if isinstance(products, list) else []
