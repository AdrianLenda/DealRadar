"""Amazon Product Advertising API 5.0 collector -- ARCHITEKTURA.md section 6 lists Amazon PA-API as an
available source. Not in A1's original task list; added afterward on the project owner's explicit request,
following the same house pattern as the other collectors in this package.

Requires three environment variables (see .env.example -- read here only via `dealradar.config.get_secret`,
never from YAML): `AMAZON_ACCESS_KEY` / `AMAZON_SECRET_KEY` (from an *approved Amazon Associates account's*
PA-API credentials page -- not a generic AWS IAM account; PA-API access is granted only after the Associates
account has referred qualifying sales) and `AMAZON_PARTNER_TAG` (the Associate/Store ID that appears in your
affiliate links, e.g. `?tag=yoursite-21`). When any of the three is unset, or when no `keywords` are
configured, `fetch()` logs a notice and yields nothing rather than raising -- same graceful-disable pattern
as `allegro_api.py` / `aliexpress.py`.

Signing: every PA-API 5.0 request must carry a valid AWS Signature Version 4 (SigV4) `Authorization` header.
This is Amazon's own request-signing contract (not a security/anti-bot workaround of ours) -- see
https://webservices.amazon.com/paapi5/documentation/without-sdk.html . Implemented below with stdlib
hashlib/hmac only, no `boto3` (a full AWS SDK would be a heavy new dependency for one signature algorithm).
`BaseCollector.get()` is GET-only and cannot express a signed POST, so this collector uses
`BaseCollector.post()` instead (added alongside this file; see that method's docstring).

Marketplace/host/region are read from `config/sources.yaml` (`marketplace`, `host`, `region`, `search_index`,
`resources` extra fields), not hardcoded: Amazon hosts PA-API differently per marketplace and the mapping is
not published in one stable place. **The `www.amazon.pl` / `webservices.amazon.pl` / `eu-west-1` defaults
below are this collector's best-effort guess for the Polish marketplace -- they have not been verified
against a live Associates account from this environment (no outbound network access here). Confirm the
correct host/region for your account and marketplace on your Associates PA-API credentials page before
enabling this source.**
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dealradar.config import get_secret
from dealradar.errors import CollectorError
from dealradar.models import RawOffer, SourceKind

from . import BaseCollector

logger = logging.getLogger("dealradar.collectors.amazon")

SERVICE = "ProductAdvertisingAPI"
TARGET = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems"
CANONICAL_URI = "/paapi5/searchitems"
ALGORITHM = "AWS4-HMAC-SHA256"

DEFAULT_RESOURCES: tuple[str, ...] = (
    "ItemInfo.Title",
    "ItemInfo.ByLineInfo",
    "ItemInfo.ExternalIds",
    "ItemInfo.Features",
    "Offers.Listings.Price",
    "Offers.Listings.Condition",
    "Offers.Listings.DeliveryInfo.IsPrimeEligible",
    "Offers.Listings.MerchantInfo",
    "Images.Primary.Medium",
    "CustomerReviews.Count",
    "CustomerReviews.StarRating",
)
# PA-API 5.0 hard-caps SearchItems at 10 results per page (ItemPage 1..10 -> up to 100 items per keyword).
MAX_ITEM_COUNT_PER_PAGE = 10


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    """Returns the raw HMAC-SHA256 digest of msg under key -- one link in SigV4's signing-key derivation chain."""
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _serialize_body(body: dict[str, Any]) -> bytes:
    """Serializes a PA-API request body to canonical, deterministic JSON bytes -- must be byte-identical
    between what gets signed and what gets sent, or Amazon rejects the signature."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sigv4_headers(
    *,
    access_key: str,
    secret_key: str,
    host: str,
    region: str,
    payload: bytes,
    amz_date: str | None = None,
) -> dict[str, str]:
    """Builds the full header set PA-API's SearchItems requires for one signed POST, including a valid AWS
    Signature Version 4 Authorization header computed over exactly `payload`'s bytes; returns headers ready
    to pass to `BaseCollector.post()`. `amz_date` is only ever overridden by tests, for a reproducible
    signature -- production calls always use the real current UTC time.
    """
    now = datetime.now(timezone.utc)
    resolved_amz_date = amz_date or now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = resolved_amz_date[:8]

    payload_hash = hashlib.sha256(payload).hexdigest()
    canonical_headers = (
        f"content-encoding:amz-1.0\n"
        f"content-type:application/json; charset=UTF-8\n"
        f"host:{host}\n"
        f"x-amz-date:{resolved_amz_date}\n"
        f"x-amz-target:{TARGET}\n"
    )
    signed_headers = "content-encoding;content-type;host;x-amz-date;x-amz-target"
    canonical_request = "\n".join(
        ["POST", CANONICAL_URI, "", canonical_headers, signed_headers, payload_hash]
    )

    credential_scope = f"{date_stamp}/{region}/{SERVICE}/aws4_request"
    string_to_sign = "\n".join(
        [
            ALGORITHM,
            resolved_amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )

    k_date = _hmac_sha256(f"AWS4{secret_key}".encode("utf-8"), date_stamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, SERVICE)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    authorization = (
        f"{ALGORITHM} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Content-Encoding": "amz-1.0",
        "Content-Type": "application/json; charset=UTF-8",
        "Host": host,
        "X-Amz-Date": resolved_amz_date,
        "X-Amz-Target": TARGET,
        "Authorization": authorization,
    }


class AmazonPaApiCollector(BaseCollector):
    """Searches Amazon's Product Advertising API 5.0 (SearchItems) and yields raw items; disables itself
    without credentials or without any configured keyword (SearchItems has no "list everything" mode)."""

    source_name = "amazon"
    kind: SourceKind = "api"

    def __init__(
        self,
        *,
        source_id: int,
        source_name: str = "amazon",
        keywords: list[str] | None = None,
        search_index: str = "All",
        marketplace: str = "www.amazon.pl",
        host: str = "webservices.amazon.pl",
        region: str = "eu-west-1",
        resources: list[str] | None = None,
        page_size: int = MAX_ITEM_COUNT_PER_PAGE,
        max_pages: int = 10,
        rps: float = 1.0,
        cache_dir: Path | None = None,
        offline: bool = False,
        **kwargs: Any,
    ) -> None:
        """Configures search parameters and the target marketplace/host/region; makes no request until fetch()."""
        super().__init__(source_id, rps=rps, cache_dir=cache_dir, offline=offline, **kwargs)
        self.source_name = source_name
        self.keywords = list(keywords) if keywords else []
        self.search_index = search_index
        self.marketplace = marketplace
        self.host = host
        self.region = region
        self.resources = list(resources) if resources else list(DEFAULT_RESOURCES)
        self.page_size = max(1, min(page_size, MAX_ITEM_COUNT_PER_PAGE))
        self.max_pages = max_pages

    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:
        """Yields one RawOffer per item found across all configured keywords; yields nothing (after logging)
        when credentials are unset or no keyword is configured."""
        creds = self._credentials()
        if creds is None:
            logger.info(
                "source=%s disabled: no credentials (AMAZON_ACCESS_KEY/AMAZON_SECRET_KEY/AMAZON_PARTNER_TAG unset)",
                self.source_name,
            )
            return
        if not self.keywords:
            logger.info(
                "source=%s no keywords configured in config/sources.yaml; SearchItems requires at least one "
                "search term, yielding nothing",
                self.source_name,
            )
            return
        access_key, secret_key, partner_tag = creds
        for keyword in self.keywords:
            yield from self._fetch_keyword(access_key, secret_key, partner_tag, keyword)

    def _credentials(self) -> tuple[str, str, str] | None:
        """Returns (access_key, secret_key, partner_tag) from the environment, or None if any is unset."""
        access_key = get_secret("AMAZON_ACCESS_KEY")
        secret_key = get_secret("AMAZON_SECRET_KEY")
        partner_tag = get_secret("AMAZON_PARTNER_TAG")
        if not access_key or not secret_key or not partner_tag:
            return None
        return access_key, secret_key, partner_tag

    def _request_body(self, partner_tag: str, keyword: str, page: int) -> dict[str, Any]:
        """Builds the SearchItems JSON request body for one keyword/page -- a pure function, directly testable."""
        return {
            "Keywords": keyword,
            "SearchIndex": self.search_index,
            "ItemPage": page,
            "ItemCount": self.page_size,
            "PartnerTag": partner_tag,
            "PartnerType": "Associates",
            "Marketplace": self.marketplace,
            "Resources": self.resources,
        }

    def _search_items_page(
        self, access_key: str, secret_key: str, partner_tag: str, keyword: str, page: int
    ) -> dict[str, Any]:
        """Sends one signed SearchItems request for keyword/page and returns the parsed JSON response body."""
        body = self._request_body(partner_tag, keyword, page)
        payload = _serialize_body(body)
        headers = sigv4_headers(
            access_key=access_key, secret_key=secret_key, host=self.host, region=self.region, payload=payload
        )
        response = self.post(f"https://{self.host}{CANONICAL_URI}", payload, headers=headers)
        data = response.json()
        return data if isinstance(data, dict) else {}

    def _fetch_keyword(
        self, access_key: str, secret_key: str, partner_tag: str, keyword: str
    ) -> Iterator[RawOffer]:
        """Pages through SearchItems for one keyword, yielding items until a short page or an error ends it."""
        for page in range(1, self.max_pages + 1):
            data = self._search_items_page(access_key, secret_key, partner_tag, keyword, page)
            errors = data.get("Errors")
            if errors:
                raise CollectorError(
                    f"Amazon PA-API returned an error for keyword={keyword!r} page={page}: {errors}"
                )
            items = data.get("SearchResult", {}).get("Items", [])
            if not isinstance(items, list) or not items:
                return
            for item in items:
                if not isinstance(item, dict):
                    continue
                asin = str(item.get("ASIN", "")).strip()
                if not asin:
                    continue
                yield self.make_raw_offer(asin, item)
            if len(items) < self.page_size:
                return
