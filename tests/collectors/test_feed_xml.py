"""Offline tests for FeedXmlCollector -- the generic affiliate-feed collector -- against recorded fixtures.

All tests here use `local_path` to read a fixture file directly, so no network and no on-disk response
cache are involved; `test_ibood.py`/`test_pepper_rss.py` cover the cache-based offline path instead.
"""

from __future__ import annotations

import pytest

from dealradar.collectors.feed_xml import FeedXmlCollector
from dealradar.errors import CollectorError
from dealradar.models import compute_content_hash

from conftest import FIXTURES_DIR

FEED_XML_DIR = FIXTURES_DIR / "feed_xml"


def _collector(**overrides: object) -> FeedXmlCollector:
    kwargs: dict[str, object] = dict(
        source_id=1,
        source_name="examplepl_awin_template",
        url="https://productdata.awin.com/datafeed/download/xml",
        feed_format="xml",
        record_tag="product",
        id_field="aw_product_id",
    )
    kwargs.update(overrides)
    return FeedXmlCollector(**kwargs)  # type: ignore[arg-type]


def test_fetch_xml_yields_one_raw_offer_per_product_with_raw_untouched_payload() -> None:
    collector = _collector(local_path=FEED_XML_DIR / "awin_sample.xml")
    offers = list(collector.fetch(None))
    assert len(offers) == 4
    assert offers[0].external_id == "MX-991"
    assert offers[0].payload_json["ean"] == "8806094928591"
    assert offers[0].payload_json["search_price"] == "249.00"  # untouched string, A1 does not parse prices
    assert offers[0].source_id == 1


def test_fetch_xml_falls_back_to_content_hash_id_when_id_field_is_absent() -> None:
    collector = _collector(local_path=FEED_XML_DIR / "awin_sample.xml")
    offers = list(collector.fetch(None))
    last = offers[-1]
    assert "aw_product_id" not in last.payload_json
    assert last.external_id == compute_content_hash(last.payload_json)[:24]


def test_fetch_xml_without_record_tag_raises_collector_error() -> None:
    collector = _collector(record_tag=None, local_path=FEED_XML_DIR / "awin_sample.xml")
    with pytest.raises(CollectorError):
        list(collector.fetch(None))


def test_fetch_gzip_xml_produces_identical_records_to_the_plain_file() -> None:
    plain = list(_collector(local_path=FEED_XML_DIR / "awin_sample.xml").fetch(None))
    gz = list(_collector(local_path=FEED_XML_DIR / "awin_sample.xml.gz").fetch(None))
    assert [o.payload_json for o in plain] == [o.payload_json for o in gz]
    assert [o.external_id for o in plain] == [o.external_id for o in gz]


def test_fetch_csv_auto_detects_semicolon_delimiter_and_windows1250_encoding() -> None:
    collector = _collector(
        feed_format="csv",
        record_tag=None,
        id_field="sku",
        local_path=FEED_XML_DIR / "tradedoubler_sample_cp1250.csv",
    )
    offers = list(collector.fetch(None))
    assert len(offers) == 4
    assert offers[0].external_id == "TD-1001"
    assert offers[1].payload_json["nazwa"] == "Czajnik elektryczny Żelazny 1.7L"
    assert offers[2].payload_json["gtin"] == "5901234567891"


def test_fetch_tsv_with_explicit_delimiter() -> None:
    collector = _collector(
        feed_format="tsv",
        record_tag=None,
        id_field="id",
        local_path=FEED_XML_DIR / "convertiser_sample.tsv",
    )
    offers = list(collector.fetch(None))
    assert len(offers) == 3
    assert offers[0].external_id == "CV-2001"
    assert offers[0].payload_json["barcode"] == "5901234000012"
    assert offers[1].payload_json["barcode"] == ""


def test_repeated_fetch_of_unchanged_feed_produces_identical_content_hashes() -> None:
    """Idempotency at the collector level: unchanged input -> identical content_hash across two fetches."""
    first = {o.external_id: o.content_hash for o in _collector(local_path=FEED_XML_DIR / "awin_sample.xml").fetch(None)}
    second = {o.external_id: o.content_hash for o in _collector(local_path=FEED_XML_DIR / "awin_sample.xml").fetch(None)}
    assert first == second


def test_changed_price_between_runs_changes_the_content_hash_but_not_the_external_id() -> None:
    run1 = {o.external_id: o.content_hash for o in _collector(local_path=FEED_XML_DIR / "awin_sample.xml").fetch(None)}
    run2 = {
        o.external_id: o.content_hash
        for o in _collector(local_path=FEED_XML_DIR / "awin_sample_run2.xml").fetch(None)
    }
    # MX-991's price changed between fixtures -> same external_id, different hash (a genuine change to record).
    assert run1["MX-991"] != run2["MX-991"]
    # MX-992 is byte-identical in both fixtures -> same hash (this is what makes dedup-by-hash idempotent).
    assert run1["MX-992"] == run2["MX-992"]
