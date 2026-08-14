"""Offline tests for IboodCollector against recorded JSON fixtures, via BaseCollector's on-disk cache."""

from __future__ import annotations

from pathlib import Path

from dealradar.collectors.ibood import IboodCollector

from conftest import read_fixture, write_cached_response

URL = "https://www.ibood.com/feed"


def _collector(cache_dir: Path) -> IboodCollector:
    return IboodCollector(source_id=1, source_name="ibood", url=URL, cache_dir=cache_dir, offline=True)


def test_fetch_yields_one_raw_offer_per_deal(tmp_path: Path) -> None:
    write_cached_response(tmp_path, URL, read_fixture("ibood", "run1.json"))
    offers = list(_collector(tmp_path).fetch(None))
    assert len(offers) == 3
    assert {o.external_id for o in offers} == {"12345", "12346", "12347"}
    airpods = next(o for o in offers if o.external_id == "12345")
    assert airpods.payload_json["ean"] == "0194252057626"
    vacuum = next(o for o in offers if o.external_id == "12346")
    assert vacuum.payload_json["ean"] is None  # missing EAN kept as None, not dropped or faked


def test_empty_feed_yields_nothing(tmp_path: Path) -> None:
    write_cached_response(tmp_path, URL, read_fixture("ibood", "empty.json"))
    offers = list(_collector(tmp_path).fetch(None))
    assert offers == []


def test_repeated_fetch_of_unchanged_feed_is_idempotent_by_content_hash(tmp_path: Path) -> None:
    write_cached_response(tmp_path, URL, read_fixture("ibood", "run1.json"))
    first = {o.external_id: o.content_hash for o in _collector(tmp_path).fetch(None)}
    second = {o.external_id: o.content_hash for o in _collector(tmp_path).fetch(None)}
    assert first == second


def test_price_change_between_runs_changes_hash_new_deal_appears_one_disappears(tmp_path: Path) -> None:
    write_cached_response(tmp_path, URL, read_fixture("ibood", "run1.json"))
    run1 = {o.external_id: o.content_hash for o in _collector(tmp_path).fetch(None)}

    tmp_path2 = tmp_path / "run2"
    write_cached_response(tmp_path2, URL, read_fixture("ibood", "run2.json"))
    run2 = {o.external_id: o.content_hash for o in _collector(tmp_path2).fetch(None)}

    assert run1["12345"] == run2["12345"]  # unchanged deal -> identical hash
    assert run1["12346"] != run2["12346"]  # price dropped 899 -> 799 -> different hash, same external_id
    assert "12347" not in run2  # deal 12347 rotated out
    assert "12348" in run2  # deal 12348 is new this run
