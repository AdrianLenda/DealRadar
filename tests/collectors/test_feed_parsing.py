"""Unit tests for the streaming parsing primitives in collectors/_feed_parsing.py -- no HTTP, no network.

These exercise the parsing logic directly against small in-memory byte buffers and the recorded fixture
files under tests/fixtures/feed_xml/, independent of FeedXmlCollector / BaseCollector.
"""

from __future__ import annotations

import gzip
import io

from dealradar.collectors._feed_parsing import (
    detect_text_encoding,
    guess_format,
    iter_csv_records,
    iter_xml_records,
    open_maybe_gzip,
)

from conftest import FIXTURES_DIR


def test_iter_xml_records_flattens_children_and_preserves_raw_field_names() -> None:
    raw = (FIXTURES_DIR / "feed_xml" / "awin_sample.xml").read_bytes()
    records = list(iter_xml_records(io.BytesIO(raw), "product"))
    assert len(records) == 4
    first = records[0]
    # Field names are exactly as they appeared in the feed (no renaming to a canonical schema -- Z1).
    assert first["aw_product_id"] == "MX-991"
    assert first["ean"] == "8806094928591"
    assert first["search_price"] == "249.00"
    assert first["brand_name"] == "Samsung"
    assert "Słuchawki" in first["category_name"]


def test_iter_xml_records_handles_missing_or_empty_fields_without_rejecting_the_record() -> None:
    raw = (FIXTURES_DIR / "feed_xml" / "awin_sample.xml").read_bytes()
    records = list(iter_xml_records(io.BytesIO(raw), "product"))
    last = records[-1]
    # This record has no aw_product_id and an empty <ean/> -- still yielded raw, not dropped.
    assert "aw_product_id" not in last
    assert last["ean"] == ""
    assert last["brand_name"] == "Baseus"


def test_guess_format_prefers_url_extension() -> None:
    assert guess_format("https://example.pl/feed.csv", b"") == "csv"
    assert guess_format("https://example.pl/feed.tsv", b"") == "tsv"
    assert guess_format("https://example.pl/feed.xml", b"") == "xml"


def test_guess_format_sniffs_content_when_extension_is_ambiguous() -> None:
    assert guess_format("https://example.pl/download?id=1", b"<?xml version='1.0'?><merchant>") == "xml"
    assert guess_format("https://example.pl/download?id=1", b"sku;price\nA;1\n") == "csv"


def test_detect_text_encoding_utf8_bom_and_windows1250_fallback() -> None:
    assert detect_text_encoding("hello".encode("ascii")) == "utf-8"
    assert detect_text_encoding(b"\xef\xbb\xbfhello") == "utf-8-sig"
    # "Żelazny" (Polish for "iron/steel") encoded as windows-1250 is not valid utf-8.
    cp1250_sample = "Żelazny".encode("cp1250")
    assert detect_text_encoding(cp1250_sample) == "windows-1250"


def test_iter_csv_records_auto_detects_semicolon_delimiter_and_windows1250_encoding() -> None:
    raw = (FIXTURES_DIR / "feed_xml" / "tradedoubler_sample_cp1250.csv").read_bytes()
    records = list(iter_csv_records(io.BytesIO(raw), encoding=None, delimiter=None))
    assert len(records) == 4
    assert records[0]["sku"] == "TD-1001"
    assert records[0]["gtin"] == "5901234567890"
    # Polish diacritics must come through correctly, not mangled -- this is the whole point of the fallback.
    assert records[1]["nazwa"] == "Czajnik elektryczny Żelazny 1.7L"
    assert records[2]["nazwa"] == "Głośnik Bluetooth JBL Flip 6"
    # A record with no gtin keeps that field present but empty -- not dropped, not fabricated (Z4 spirit).
    assert records[1]["gtin"] == ""


def test_iter_csv_records_explicit_tsv_delimiter() -> None:
    raw = (FIXTURES_DIR / "feed_xml" / "convertiser_sample.tsv").read_bytes()
    records = list(iter_csv_records(io.BytesIO(raw), encoding=None, delimiter="\t"))
    assert len(records) == 3
    assert records[0]["id"] == "CV-2001"
    assert records[0]["title"] == "Etui na telefon iPhone 15 Skórzane"
    assert records[1]["barcode"] == ""  # missing barcode in the source row, kept as empty string, not None/0


def test_open_maybe_gzip_transparently_decompresses() -> None:
    original = b"<merchant><product><id>1</id></product></merchant>"
    compressed = gzip.compress(original)
    stream, sample = open_maybe_gzip(io.BytesIO(compressed))
    assert sample.startswith(b"<merchant")
    # A single size=-1 read() on the returned stream replays the peeked sample plus whatever remains --
    # together, the fully decompressed original content.
    assert stream.read() == original


def test_open_maybe_gzip_full_round_trip_reads_all_bytes_for_a_larger_payload() -> None:
    original = b"<merchant>" + b"<product><id>1</id></product>" * 500 + b"</merchant>"
    compressed = gzip.compress(original)
    stream, sample = open_maybe_gzip(io.BytesIO(compressed))
    assert len(original) > 4096  # exercises the case where the 4096-byte peek does not cover the whole file
    assert stream.read() == original


def test_open_maybe_gzip_passes_through_plain_content_unchanged() -> None:
    original = b"<merchant><product><id>1</id></product></merchant>"
    stream, sample = open_maybe_gzip(io.BytesIO(original))
    assert stream.read() == original
