"""Offline tests for AmazonPaApiCollector.

No Amazon Associates PA-API credentials exist in this environment, so only the credential/keyword-missing
"disabled" paths and the full fetch/pagination path against a recorded fixture are exercised end to end.
`sigv4_headers` (the from-scratch AWS Signature Version 4 implementation, stdlib hashlib/hmac only) is
checked separately for structural correctness and determinism -- it has not been run against a live PA-API
account, since this environment has no outbound network access. See amazon_pa_api.py's module docstring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dealradar.collectors.amazon_pa_api import (
    AmazonPaApiCollector,
    _serialize_body,
    sigv4_headers,
)

from conftest import read_fixture, write_cached_post_response


def test_fetch_yields_nothing_and_does_not_raise_when_credentials_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AMAZON_ACCESS_KEY", raising=False)
    monkeypatch.delenv("AMAZON_SECRET_KEY", raising=False)
    monkeypatch.delenv("AMAZON_PARTNER_TAG", raising=False)
    collector = AmazonPaApiCollector(source_id=1, keywords=["dysk SSD"], cache_dir=tmp_path, offline=True)
    assert list(collector.fetch(None)) == []


def test_fetch_yields_nothing_when_only_two_of_three_credentials_are_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMAZON_ACCESS_KEY", "fake-access")
    monkeypatch.setenv("AMAZON_SECRET_KEY", "fake-secret")
    monkeypatch.delenv("AMAZON_PARTNER_TAG", raising=False)
    collector = AmazonPaApiCollector(source_id=1, keywords=["dysk SSD"], cache_dir=tmp_path, offline=True)
    assert list(collector.fetch(None)) == []


def test_fetch_yields_nothing_when_no_keywords_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMAZON_ACCESS_KEY", "fake-access")
    monkeypatch.setenv("AMAZON_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("AMAZON_PARTNER_TAG", "fake-tag-21")
    collector = AmazonPaApiCollector(source_id=1, keywords=[], cache_dir=tmp_path, offline=True)
    assert list(collector.fetch(None)) == []


def test_fetch_paginates_items_and_preserves_the_full_item_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AMAZON_ACCESS_KEY", "fake-access")
    monkeypatch.setenv("AMAZON_SECRET_KEY", "fake-secret")
    monkeypatch.setenv("AMAZON_PARTNER_TAG", "fake-tag-21")

    http_cache = tmp_path / "http_cache"
    collector = AmazonPaApiCollector(
        source_id=1,
        keywords=["dysk SSD"],
        cache_dir=http_cache,
        offline=True,
    )
    body = collector._request_body("fake-tag-21", "dysk SSD", 1)
    payload = _serialize_body(body)
    url = f"https://{collector.host}{'/paapi5/searchitems'}"
    write_cached_post_response(http_cache, url, payload, read_fixture("amazon", "search_items_page1.json"))

    offers = list(collector.fetch(None))
    assert len(offers) == 2
    ids = {o.external_id for o in offers}
    assert ids == {"B0BHJVJ5Y8", "B09XYZ1234"}

    samsung = next(o for o in offers if o.external_id == "B0BHJVJ5Y8")
    assert samsung.payload_json["ItemInfo"]["ExternalIds"]["EANs"]["DisplayValues"] == ["8806094962078"]
    assert samsung.payload_json["Offers"]["Listings"][0]["Price"]["Amount"] == 649.0
    # Only 2 items came back on a page sized for 10 -- pagination must stop, not request a page 2 that has
    # no cached fixture (which would raise CollectorError in offline mode and fail this test).


def test_request_body_includes_partner_tag_and_configured_search_index(tmp_path: Path) -> None:
    collector = AmazonPaApiCollector(
        source_id=1, keywords=["powerbank"], search_index="Electronics", cache_dir=tmp_path, offline=True
    )
    body = collector._request_body("my-tag-21", "powerbank", 3)
    assert body["PartnerTag"] == "my-tag-21"
    assert body["PartnerType"] == "Associates"
    assert body["Keywords"] == "powerbank"
    assert body["SearchIndex"] == "Electronics"
    assert body["ItemPage"] == 3


def test_sigv4_headers_are_well_formed_and_deterministic_for_fixed_inputs() -> None:
    payload = _serialize_body({"Keywords": "ssd", "ItemPage": 1})
    headers = sigv4_headers(
        access_key="AKIAFAKEEXAMPLE",
        secret_key="fakeSecretKeyExample",
        host="webservices.amazon.pl",
        region="eu-west-1",
        payload=payload,
        amz_date="20260811T120000Z",
    )
    assert headers["X-Amz-Date"] == "20260811T120000Z"
    assert headers["X-Amz-Target"] == "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems"
    assert headers["Host"] == "webservices.amazon.pl"
    auth = headers["Authorization"]
    assert auth.startswith("AWS4-HMAC-SHA256 Credential=AKIAFAKEEXAMPLE/20260811/eu-west-1/ProductAdvertisingAPI/aws4_request, ")
    assert "SignedHeaders=content-encoding;content-type;host;x-amz-date;x-amz-target" in auth
    signature = auth.rsplit("Signature=", 1)[1]
    assert len(signature) == 64
    int(signature, 16)  # must be valid lowercase hex

    # Same inputs -> byte-identical signature (SigV4 is deterministic given a fixed timestamp).
    headers_again = sigv4_headers(
        access_key="AKIAFAKEEXAMPLE",
        secret_key="fakeSecretKeyExample",
        host="webservices.amazon.pl",
        region="eu-west-1",
        payload=payload,
        amz_date="20260811T120000Z",
    )
    assert headers_again["Authorization"] == auth


def test_sigv4_signature_changes_when_payload_changes() -> None:
    common = dict(
        access_key="AKIAFAKEEXAMPLE",
        secret_key="fakeSecretKeyExample",
        host="webservices.amazon.pl",
        region="eu-west-1",
        amz_date="20260811T120000Z",
    )
    sig_a = sigv4_headers(payload=_serialize_body({"Keywords": "ssd"}), **common)["Authorization"]
    sig_b = sigv4_headers(payload=_serialize_body({"Keywords": "hdd"}), **common)["Authorization"]
    assert sig_a != sig_b


def test_sigv4_signature_changes_when_secret_key_changes() -> None:
    payload = _serialize_body({"Keywords": "ssd"})
    sig_a = sigv4_headers(
        access_key="AKIAFAKEEXAMPLE", secret_key="secret-one", host="webservices.amazon.pl",
        region="eu-west-1", payload=payload, amz_date="20260811T120000Z",
    )["Authorization"]
    sig_b = sigv4_headers(
        access_key="AKIAFAKEEXAMPLE", secret_key="secret-two", host="webservices.amazon.pl",
        region="eu-west-1", payload=payload, amz_date="20260811T120000Z",
    )["Authorization"]
    assert sig_a != sig_b
