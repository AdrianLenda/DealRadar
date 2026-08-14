"""Offline tests for PepperRssCollector against recorded RSS fixtures, via BaseCollector's on-disk cache."""

from __future__ import annotations

from pathlib import Path

from dealradar.collectors.pepper_rss import PepperRssCollector

from conftest import FIXTURES_DIR, read_fixture, write_cached_response

URL = "https://www.pepper.pl/rss/hot"


def _collector(cache_dir: Path) -> PepperRssCollector:
    return PepperRssCollector(source_id=1, source_name="pepper", url=URL, cache_dir=cache_dir, offline=True)


def test_fetch_yields_one_raw_offer_per_item_with_temperature_extracted(tmp_path: Path) -> None:
    write_cached_response(tmp_path, URL, read_fixture("pepper", "run1.xml"))
    offers = list(_collector(tmp_path).fetch(None))
    assert len(offers) == 3

    airpods = offers[0]
    assert airpods.external_id == (
        "https://www.pepper.pl/promocje/apple-airpods-pro-2-899-zl-media-expert-123456"
    )
    assert airpods.payload_json["pepper_temperature"] == 456
    assert "899 zł" in airpods.payload_json["description"]

    laptop = offers[1]
    assert laptop.payload_json["pepper_temperature"] == 210

    # Third item's description has no detectable temperature badge -- key is absent, never fabricated as 0.
    ssd = offers[2]
    assert "pepper_temperature" not in ssd.payload_json


def test_repeated_fetch_of_unchanged_feed_is_idempotent_by_content_hash(tmp_path: Path) -> None:
    write_cached_response(tmp_path, URL, read_fixture("pepper", "run1.xml"))
    first = {o.external_id: o.content_hash for o in _collector(tmp_path).fetch(None)}
    second = {o.external_id: o.content_hash for o in _collector(tmp_path).fetch(None)}
    assert first == second


def test_temperature_change_between_runs_changes_content_hash_for_the_same_item(tmp_path: Path) -> None:
    write_cached_response(tmp_path, URL, read_fixture("pepper", "run1.xml"))
    run1 = {o.external_id: o.content_hash for o in _collector(tmp_path).fetch(None)}

    tmp_path2 = tmp_path / "run2"
    write_cached_response(tmp_path2, URL, read_fixture("pepper", "run2.xml"))
    run2 = {o.external_id: o.content_hash for o in _collector(tmp_path2).fetch(None)}

    airpods_guid = "https://www.pepper.pl/promocje/apple-airpods-pro-2-899-zl-media-expert-123456"
    assert airpods_guid in run1 and airpods_guid in run2
    assert run1[airpods_guid] != run2[airpods_guid]  # temperature moved 456 -> 512
    assert len(run2) == 2  # run2 fixture drops one item and adds a different one
