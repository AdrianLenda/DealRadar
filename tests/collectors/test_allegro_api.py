"""Offline tests for AllegroApiCollector.

Credential-missing path uses no fixtures at all (nothing to fetch). The authenticated path pre-populates
the token cache file directly (bypassing the OAuth POST, which BaseCollector.get() cannot serve from its
GET-keyed cache) and pre-populates the response cache for the listing search GET with a recorded fixture
shaped like a real `offers/listing` response.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dealradar.collectors.allegro_api import SEARCH_URL, AllegroApiCollector

from conftest import read_fixture, write_cached_response


def _write_valid_token(token_cache_path: Path) -> None:
    token_cache_path.parent.mkdir(parents=True, exist_ok=True)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    token_cache_path.write_text(
        json.dumps({"access_token": "test-access-token-abc123", "expires_at": expires_at.isoformat()}),
        encoding="utf-8",
    )


def test_fetch_yields_nothing_and_does_not_raise_when_credentials_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALLEGRO_CLIENT_ID", raising=False)
    monkeypatch.delenv("ALLEGRO_CLIENT_SECRET", raising=False)
    collector = AllegroApiCollector(
        source_id=1,
        source_name="allegro",
        queries=["ssd"],
        token_cache_path=tmp_path / "token.json",
        cache_dir=tmp_path / "http_cache",
        offline=True,
    )
    assert list(collector.fetch(None)) == []


def test_fetch_paginates_offers_and_preserves_the_full_parameter_array(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLEGRO_CLIENT_ID", "fake-id")
    monkeypatch.setenv("ALLEGRO_CLIENT_SECRET", "fake-secret")
    token_cache_path = tmp_path / "token.json"
    _write_valid_token(token_cache_path)

    http_cache = tmp_path / "http_cache"
    write_cached_response(
        http_cache,
        SEARCH_URL,
        read_fixture("allegro", "listing_ssd_page1.json"),
        params={"limit": 60, "offset": 0, "phrase": "ssd"},
    )

    collector = AllegroApiCollector(
        source_id=1,
        source_name="allegro",
        queries=["ssd"],
        token_cache_path=token_cache_path,
        cache_dir=http_cache,
        offline=True,
    )
    offers = list(collector.fetch(None))
    assert len(offers) == 3
    ids = {o.external_id for o in offers}
    assert ids == {"13998877661", "13998877662", "13998877663"}

    promoted = next(o for o in offers if o.external_id == "13998877661")
    param_names = {p["name"] for p in promoted.payload_json["parameters"]}
    assert "EAN (GTIN)" in param_names  # the parameter array -- where EANs live on Allegro -- is kept intact
    assert promoted.payload_json["sellingMode"]["price"]["amount"] == "899.00"


def test_token_cache_round_trip(tmp_path: Path) -> None:
    collector = AllegroApiCollector(source_id=1, source_name="allegro", token_cache_path=tmp_path / "token.json")
    collector._save_token("tok-1", 3600)
    assert collector._load_cached_token() == "tok-1"


def test_expired_cached_token_is_not_reused(tmp_path: Path) -> None:
    path = tmp_path / "token.json"
    expired_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    path.write_text(json.dumps({"access_token": "stale", "expires_at": expired_at.isoformat()}), encoding="utf-8")
    collector = AllegroApiCollector(source_id=1, source_name="allegro", token_cache_path=path)
    assert collector._load_cached_token() is None
