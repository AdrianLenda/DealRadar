"""Shared helpers for offline collector tests.

`write_cached_response` pre-populates BaseCollector's on-disk response cache with a recorded fixture body,
keyed exactly the way `BaseCollector.get()` will look it up -- this is what lets tests exercise the *real*
`get()` / retry / cache code path (not a mock) while never touching the network, per the task spec's ban on
network-touching tests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from dealradar.collectors import BaseCollector

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def write_cached_response(
    cache_dir: Path,
    url: str,
    body_text: str,
    *,
    params: dict[str, Any] | None = None,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    """Writes a recorded response into cache_dir in exactly the format BaseCollector.get() reads back."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = BaseCollector.cache_key(url, params)
    payload = {"status_code": status_code, "headers": headers or {}, "text": body_text}
    (cache_dir / f"{key}.json").write_text(json.dumps(payload), encoding="utf-8")


def write_cached_post_response(
    cache_dir: Path,
    url: str,
    request_body: bytes,
    body_text: str,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> None:
    """Writes a recorded response into cache_dir keyed exactly the way BaseCollector.post() looks it up --
    by url + a hash of the exact request body bytes that will be sent (see amazon_pa_api.py)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = BaseCollector.cache_key(url, {"post_body_sha256": hashlib.sha256(request_body).hexdigest()})
    payload = {"status_code": status_code, "headers": headers or {}, "text": body_text}
    (cache_dir / f"{key}.json").write_text(json.dumps(payload), encoding="utf-8")


def read_fixture(*parts: str) -> str:
    """Reads a tests/fixtures/<parts...> file as UTF-8 text; a small convenience wrapper used across tests."""
    return (FIXTURES_DIR.joinpath(*parts)).read_text(encoding="utf-8")
