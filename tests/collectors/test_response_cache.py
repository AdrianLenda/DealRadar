"""Regression tests for BaseCollector's on-disk response cache.

These exist because of a bug that only ever appeared against a live server: Pepper serves the RSS feed
gzip-compressed, httpx transparently decodes it, and the cache used to store that *decoded* text together
with the original `Content-Encoding: gzip` header. Replaying such an entry made httpx try to gunzip plain
text and fail with "Error -3 while decompressing data: incorrect header check" -- which the runner then
reported as the source having silently died (F5). Every existing test wrote its own cache entries with
empty headers, so none of them could catch it.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import httpx

from dealradar.collectors import BaseCollector
from dealradar.models import RawOffer


class _StubCollector(BaseCollector):
    """Minimal concrete collector: fetch() is never used here, only get()'s cache path."""

    source_name = "stub"

    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:  # pragma: no cover -- unused
        raise NotImplementedError


def _gzip_response(url: str, body: str) -> httpx.Response:
    """Builds a response shaped exactly like a real gzip-compressed feed reply, headers included."""
    return httpx.Response(
        status_code=200,
        headers={"Content-Encoding": "gzip", "Content-Type": "text/xml; charset=utf-8"},
        content=gzip.compress(body.encode("utf-8")),
        request=httpx.Request("GET", url),
    )


def test_gzipped_response_survives_a_cache_round_trip(tmp_path: Path) -> None:
    """A gzip-encoded response must be replayable from cache -- this is the exact live failure."""
    url = "https://www.pepper.pl/rss/gor%C4%85ce"
    body = "<?xml version='1.0' encoding='UTF-8'?><rss><channel><item><title>Deal</title></item></channel></rss>"
    collector = _StubCollector(source_id=1, cache_dir=tmp_path, offline=True)

    # httpx decodes on access, exactly as it does for a real request.
    live = _gzip_response(url, body)
    assert live.text == body

    from dealradar.collectors import _save_to_cache  # noqa: PLC0415 -- private, deliberately pinned here

    cache_path = tmp_path / f"{BaseCollector.cache_key(url, None)}.json"
    _save_to_cache(cache_path, live)

    replayed = collector.get(url)  # offline=True, so this can only come from the cache
    assert replayed.text == body
    assert replayed.status_code == 200


def test_cache_entry_drops_transfer_encoding_headers_but_keeps_the_rest(tmp_path: Path) -> None:
    """Content-Encoding/Length describe the wire form and must not be replayed over a decoded body."""
    from dealradar.collectors import _save_to_cache  # noqa: PLC0415

    url = "https://example.invalid/feed"
    live = _gzip_response(url, "<rss/>")
    cache_path = tmp_path / "entry.json"
    _save_to_cache(cache_path, live)

    stored = json.loads(cache_path.read_text(encoding="utf-8"))
    header_names = {k.lower() for k in stored["headers"]}
    assert "content-encoding" not in header_names
    assert "content-length" not in header_names
    assert "content-type" in header_names  # non-transport headers are still worth keeping
    assert stored["text"] == "<rss/>"
