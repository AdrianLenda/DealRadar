"""Collector contract (etap 1: collect). Owner of this file: A0. Owner of concrete collectors: A1.

This module defines only the shared protocol and the base machinery every concrete collector reuses:
token-bucket rate limiting, retry with exponential backoff, an on-disk response cache (for offline tests),
and a helper to build a RawOffer with its content_hash filled in. No concrete source (Allegro, Pepper,
feed_xml, ...) is implemented here — that is A1's job, in sibling modules of this package.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from dealradar.errors import CollectorError
from dealradar.models import RawOffer, SourceKind, compute_content_hash

logger = logging.getLogger("dealradar.collectors")

DEFAULT_USER_AGENT = "DealRadarBot/0.1 (+https://github.com/dealradar; contact: operator)"


@runtime_checkable
class Collector(Protocol):
    """The one interface every concrete collector implements (ARCHITEKTURA.md section 6)."""

    source_name: str
    kind: SourceKind

    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:
        """Yields every RawOffer available from this source, optionally limited to items new since `since`."""
        ...


class TokenBucket:
    """A single-threaded-use token bucket for capping outbound requests to a configurable rate per second."""

    def __init__(self, rate_per_s: float, capacity: float | None = None) -> None:
        """Creates a bucket refilling at rate_per_s tokens/second, holding at most `capacity` (default rate_per_s)."""
        if rate_per_s <= 0:
            raise ValueError("rate_per_s must be > 0")
        self.rate_per_s = rate_per_s
        self.capacity = capacity if capacity is not None else max(rate_per_s, 1.0)
        self._tokens = self.capacity
        self._last_check = time.monotonic()

    def acquire(self, tokens: float = 1.0) -> None:
        """Blocks the calling thread until `tokens` are available, then consumes them; returns nothing."""
        while True:
            now = time.monotonic()
            elapsed = now - self._last_check
            self._last_check = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate_per_s)
            if self._tokens >= tokens:
                self._tokens -= tokens
                return
            time.sleep((tokens - self._tokens) / self.rate_per_s)


def _save_to_cache(path: Path, response: httpx.Response) -> None:
    """Writes an httpx.Response's status, headers, and body to `path` as JSON, for offline replay in tests."""
    payload = {"status_code": response.status_code, "headers": dict(response.headers), "text": response.text}
    path.write_text(json.dumps(payload), encoding="utf-8")


def _response_from_cache(path: Path) -> httpx.Response:
    """Reconstructs an httpx.Response from a file previously written by _save_to_cache."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    return httpx.Response(
        status_code=payload["status_code"],
        headers=payload["headers"],
        text=payload["text"],
        request=httpx.Request("GET", "https://cache.local/"),
    )


def _parse_retry_after(value: str | None) -> float | None:
    """Parses a Retry-After header's seconds form into a float; returns None if absent or unparseable."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


class BaseCollector(ABC):
    """Shared machinery for every concrete collector: rate limiting, retry/backoff, and an on-disk response cache.

    Concrete subclasses (owned by A1) implement only `fetch()`, calling `self.get()` for HTTP instead of
    building their own httpx client, so rate limiting/retry/caching/content_hash handling is never duplicated
    per source.
    """

    source_name: str = ""
    kind: SourceKind = "api"

    def __init__(
        self,
        source_id: int,
        *,
        rps: float = 1.0,
        cache_dir: Path | None = None,
        offline: bool = False,
        max_retries: int = 5,
        timeout_s: float = 30.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        """Configures rate limit, retry budget, and optional on-disk cache; opens no connection yet."""
        self.source_id = source_id
        self.cache_dir = cache_dir
        self.offline = offline
        self.max_retries = max_retries
        self.timeout_s = timeout_s
        self.user_agent = user_agent
        self._bucket = TokenBucket(rps)
        self._client: httpx.Client | None = None

    @property
    def client(self) -> httpx.Client:
        """Returns a lazily-created httpx.Client configured with this collector's timeout and User-Agent."""
        if self._client is None:
            # follow_redirects=True: feed URLs move and answer 301 to their canonical form (Pepper's
            # /rss/hot -> /rss/gorące is exactly this). Without it raise_for_status() treats a perfectly
            # healthy permanent redirect as a dead source. Signed POSTs opt out explicitly -- see post().
            self._client = httpx.Client(
                timeout=self.timeout_s,
                headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        """Closes the underlying HTTP client, if one was ever opened."""
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "BaseCollector":
        """Returns self, allowing use as a context manager that closes its HTTP client on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Closes the HTTP client on context-manager exit."""
        self.close()

    @staticmethod
    def cache_key(url: str, params: dict[str, Any] | None = None) -> str:
        """Returns a stable sha256 hex digest identifying this exact URL + query params, for caching/tests."""
        canonical = json.dumps({"url": url, "params": params or {}}, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _cache_path(self, key: str) -> Path | None:
        """Returns the on-disk cache file path for `key`, or None when no cache_dir is configured."""
        if self.cache_dir is None:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / f"{key}.json"

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        """Performs a rate-limited, retried GET; serves from the on-disk cache when present, or in offline mode.

        Retries on 429/5xx and transport errors with exponential backoff, honoring a numeric Retry-After
        header when the server sends one. Raises CollectorError once max_retries is exhausted, or immediately
        in offline mode when no cached response exists for this exact URL + params.
        """
        key = self.cache_key(url, params)
        cache_path = self._cache_path(key)
        if cache_path is not None and cache_path.exists():
            return _response_from_cache(cache_path)
        if self.offline:
            raise CollectorError(f"offline mode: no cached response for {url} (key={key})")

        self._bucket.acquire()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.client.get(url, params=params)
            except httpx.TransportError as exc:
                last_exc = exc
                wait = float(2**attempt)
                logger.warning(
                    "source=%s transport error attempt=%d: %s (retrying in %.1fs)",
                    self.source_name, attempt + 1, exc, wait,
                )
                time.sleep(wait)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = retry_after if retry_after is not None else float(2**attempt)
                logger.warning(
                    "source=%s status=%s attempt=%d waiting %.1fs",
                    self.source_name, response.status_code, attempt + 1, wait,
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            if cache_path is not None:
                _save_to_cache(cache_path, response)
            return response

        raise CollectorError(f"giving up on {url} after {self.max_retries} attempts") from last_exc

    def post(self, url: str, content: bytes, *, headers: dict[str, str] | None = None) -> httpx.Response:
        """Performs a rate-limited, retried POST with a raw byte body and explicit headers; serves from the
        on-disk cache when present, or in offline mode.

        Mirrors get()'s retry/backoff/cache behavior for APIs that cannot be expressed as a signature-free
        GET -- e.g. AWS Signature Version 4 requests, where the caller must sign these exact bytes before
        calling this method (see amazon_pa_api.py). The cache key is derived from url + a hash of `content`,
        not from `headers` -- a fresh signature/timestamp on a byte-identical retry still hits the same
        cached fixture in tests.
        """
        key = self.cache_key(url, {"post_body_sha256": hashlib.sha256(content).hexdigest()})
        cache_path = self._cache_path(key)
        if cache_path is not None and cache_path.exists():
            return _response_from_cache(cache_path)
        if self.offline:
            raise CollectorError(f"offline mode: no cached response for POST {url} (key={key})")

        self._bucket.acquire()
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                # No redirect following here: an AWS SigV4 signature covers the exact host+path it was
                # computed for, so a silently-followed redirect would just fail validation elsewhere.
                response = self.client.post(url, content=content, headers=headers, follow_redirects=False)
            except httpx.TransportError as exc:
                last_exc = exc
                wait = float(2**attempt)
                logger.warning(
                    "source=%s transport error attempt=%d: %s (retrying in %.1fs)",
                    self.source_name, attempt + 1, exc, wait,
                )
                time.sleep(wait)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = retry_after if retry_after is not None else float(2**attempt)
                logger.warning(
                    "source=%s status=%s attempt=%d waiting %.1fs",
                    self.source_name, response.status_code, attempt + 1, wait,
                )
                time.sleep(wait)
                continue

            response.raise_for_status()
            if cache_path is not None:
                _save_to_cache(cache_path, response)
            return response

        raise CollectorError(f"giving up on POST {url} after {self.max_retries} attempts") from last_exc

    def make_raw_offer(
        self, external_id: str, payload: dict[str, Any], fetched_at: datetime | None = None
    ) -> RawOffer:
        """Builds a RawOffer for this source from a raw payload dict, computing its content_hash."""
        return RawOffer(
            source_id=self.source_id,
            external_id=external_id,
            fetched_at=fetched_at or datetime.now(timezone.utc),
            payload_json=payload,
            content_hash=compute_content_hash(payload),
        )

    @abstractmethod
    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:
        """Yields every RawOffer available from this source, optionally limited to items new since `since`."""
        raise NotImplementedError
