"""Official Allegro REST API collector (OAuth2 client-credentials) -- ARCHITEKTURA.md section 6.

Search parameters (phrases, category ids) come from `config/sources.yaml`, never hardcoded. Credentials come
exclusively from the environment (`ALLEGRO_CLIENT_ID` / `ALLEGRO_CLIENT_SECRET`, loaded by `config.py` from
`.env` into `os.environ` -- read here only via `dealradar.config.get_secret`, never from YAML). The offer's
full parameter array is kept in the raw payload: that is where EANs and other structured attributes live,
and A2/A3 need them.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dealradar.config import get_secret
from dealradar.errors import CollectorError
from dealradar.models import RawOffer, SourceKind

from . import BaseCollector

logger = logging.getLogger("dealradar.collectors.allegro")

TOKEN_URL = "https://allegro.pl/auth/oauth/token"
SEARCH_URL = "https://api.allegro.pl/offers/listing"
DEFAULT_PAGE_SIZE = 60
# Refresh a bit before actual expiry so a long-running batch never gets caught mid-page with a stale token.
TOKEN_REFRESH_MARGIN_S = 120


class AllegroApiCollector(BaseCollector):
    """Searches Allegro's public offer-listing API with OAuth2 client-credentials auth and yields raw offers.

    Rate limits: Allegro returns 429 with `Retry-After` on overrun, which `BaseCollector.get()` already
    honors by pausing (not by looping unboundedly). This collector additionally watches
    `X-RateLimit-Remaining`/`X-RateLimit-Reset` response headers and pre-emptively pauses when the budget
    hits zero, so we back off before Allegro has to reject a request at all.
    """

    source_name = "allegro"
    kind: SourceKind = "api"

    def __init__(
        self,
        *,
        source_id: int,
        source_name: str = "allegro",
        queries: list[str] | None = None,
        category_ids: list[str] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = 20,
        token_cache_path: Path | None = None,
        rps: float = 2.0,
        cache_dir: Path | None = None,
        offline: bool = False,
        **kwargs: Any,
    ) -> None:
        """Configures search parameters and token cache location; requests no token until fetch() runs."""
        super().__init__(source_id, rps=rps, cache_dir=cache_dir, offline=offline, **kwargs)
        self.source_name = source_name
        self.queries = list(queries) if queries else []
        self.category_ids = list(category_ids) if category_ids else []
        self.page_size = page_size
        self.max_pages = max_pages
        self.token_cache_path = token_cache_path or (Path.home() / ".dealradar" / "allegro_token.json")

    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:
        """Yields one RawOffer per distinct offer found across all configured queries/categories, paginated."""
        token = self._ensure_token()
        if token is None:
            return
        self.client.headers["Authorization"] = f"Bearer {token}"
        self.client.headers["Accept"] = "application/vnd.allegro.public.v1+json"
        self.client.headers["Accept-Language"] = "pl-PL"
        seen_ids: set[str] = set()
        for query in self.queries or [""]:
            yield from self._fetch_query(query, seen_ids)

    def _fetch_query(self, phrase: str, seen_ids: set[str]) -> Iterator[RawOffer]:
        """Pages through the offer-listing endpoint for one search phrase, yielding offers not already seen."""
        offset = 0
        for _ in range(self.max_pages):
            params: dict[str, Any] = {"limit": self.page_size, "offset": offset}
            if phrase:
                params["phrase"] = phrase
            if self.category_ids:
                params["category.id"] = self.category_ids[0]
            response = self._get_respecting_rate_limit(SEARCH_URL, params)
            data = response.json()
            items = data.get("items", {}) if isinstance(data, dict) else {}
            offers = list(items.get("promoted", [])) + list(items.get("regular", []))
            if not offers:
                return
            for offer in offers:
                if not isinstance(offer, dict):
                    continue
                offer_id = str(offer.get("id", "")).strip()
                if not offer_id or offer_id in seen_ids:
                    continue
                seen_ids.add(offer_id)
                yield self.make_raw_offer(offer_id, offer)
            if len(offers) < self.page_size:
                return
            offset += self.page_size

    def _get_respecting_rate_limit(self, url: str, params: dict[str, Any]) -> Any:
        """Performs a rate-limited GET (via BaseCollector.get) and pauses if the response reports 0 budget left."""
        response = self.get(url, params)
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and remaining.isdigit() and int(remaining) == 0:
            reset = response.headers.get("X-RateLimit-Reset", "")
            wait_s = float(reset) if reset.replace(".", "", 1).isdigit() else 1.0
            logger.info("source=%s rate limit exhausted, pausing %.1fs before next page", self.source_name, wait_s)
            time.sleep(wait_s)
        return response

    def _credentials(self) -> tuple[str, str] | None:
        """Returns (client_id, client_secret) from the environment, or None if either is unset."""
        client_id = get_secret("ALLEGRO_CLIENT_ID")
        client_secret = get_secret("ALLEGRO_CLIENT_SECRET")
        if not client_id or not client_secret:
            return None
        return client_id, client_secret

    def _ensure_token(self) -> str | None:
        """Returns a usable access token: from cache if still fresh, else freshly requested; None if no creds."""
        creds = self._credentials()
        if creds is None:
            logger.info(
                "source=%s disabled: no credentials (ALLEGRO_CLIENT_ID/ALLEGRO_CLIENT_SECRET unset)",
                self.source_name,
            )
            return None
        cached = self._load_cached_token()
        if cached is not None:
            return cached
        return self._fetch_token(*creds)

    def _load_cached_token(self) -> str | None:
        """Returns the cached access token if the cache file exists and is not within the refresh margin of expiry."""
        if not self.token_cache_path.exists():
            return None
        try:
            data = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        expires_at_raw = data.get("expires_at")
        access_token = data.get("access_token")
        if not isinstance(expires_at_raw, str) or not isinstance(access_token, str):
            return None
        try:
            expires_at = datetime.fromisoformat(expires_at_raw)
        except ValueError:
            return None
        if expires_at - timedelta(seconds=TOKEN_REFRESH_MARGIN_S) <= datetime.now(timezone.utc):
            return None
        return access_token

    def _save_token(self, access_token: str, expires_in: int) -> None:
        """Persists access_token and its computed expiry to self.token_cache_path so later runs can reuse it."""
        self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        self.token_cache_path.write_text(
            json.dumps({"access_token": access_token, "expires_at": expires_at.isoformat()}), encoding="utf-8"
        )

    def _fetch_token(self, client_id: str, client_secret: str) -> str:
        """Requests a fresh client-credentials token from Allegro's OAuth endpoint and caches it; returns it."""
        if self.offline:
            cached = self._load_cached_token()
            if cached is not None:
                return cached
            raise CollectorError("offline mode: no cached Allegro token available and no network to fetch one")
        self._bucket.acquire()
        response = self.client.post(TOKEN_URL, params={"grant_type": "client_credentials"}, auth=(client_id, client_secret))
        if response.status_code >= 400:
            raise CollectorError(
                f"Allegro token request failed: status={response.status_code} body={response.text[:200]!r}"
            )
        data = response.json()
        access_token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(access_token, str):
            raise CollectorError("Allegro token response did not contain an access_token")
        expires_in_raw = data.get("expires_in", 3600) if isinstance(data, dict) else 3600
        expires_in = int(expires_in_raw) if isinstance(expires_in_raw, (int, float, str)) else 3600
        self._save_token(access_token, expires_in)
        return access_token
