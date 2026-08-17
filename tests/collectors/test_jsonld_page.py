"""Tests for the JSON-LD page collector.

The HTML below mirrors what LowcyDeals.pl actually serves (verified live 2026-08-17): several ld+json
blocks, products nested inside an ItemList, prices under `offers`, and one deliberately malformed block --
because a single broken script tag must not cost us the rest of the page (Z7).
"""

from __future__ import annotations

from pathlib import Path

from dealradar.collectors.jsonld_page import (
    JsonLdPageCollector,
    extract_products,
    flatten_product,
    product_external_id,
)

from conftest import write_cached_response

PAGE_URL = "https://www.lowcydeals.pl/"

HTML = """
<html><head>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"BreadcrumbList"}</script>
<script type="application/ld+json">{ this is not valid json </script>
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"ItemList","itemListElement":[
    {"@type":"Product","name":"soundcore Q30","brand":{"@type":"Brand","name":"soundcore"},
     "offers":{"@type":"Offer","price":209.00,"priceCurrency":"PLN",
               "itemCondition":"http://schema.org/NewCondition",
               "url":"https://www.lowcydeals.pl/okazja/soundcore-q30"},
     "aggregateRating":{"@type":"AggregateRating","ratingValue":4.6,"reviewCount":1820}},
    {"@type":"Product","name":"CaDA RUF GT","gtin13":"4006381333931","sku":"CADA-1",
     "offers":[{"@type":"Offer","price":"214.90","priceCurrency":"PLN"}]}
  ]}
]}
</script>
</head><body></body></html>
"""


def test_extract_products_finds_nested_products_and_survives_a_broken_block() -> None:
    products = extract_products(HTML)
    assert [p["name"] for p in products] == ["soundcore Q30", "CaDA RUF GT"]


def test_flatten_lifts_offers_brand_and_rating_to_top_level() -> None:
    flat = flatten_product(extract_products(HTML)[0])
    assert flat["name"] == "soundcore Q30"
    assert flat["price"] == 209.00
    assert flat["currency"] == "PLN"
    assert flat["brand"] == "soundcore"
    assert flat["rating"] == 4.6
    assert flat["rating_count"] == 1820
    assert flat["offer_url"].endswith("/soundcore-q30")


def test_flatten_takes_the_first_offer_when_offers_is_a_list() -> None:
    flat = flatten_product(extract_products(HTML)[1])
    assert flat["price"] == "214.90"  # left exactly as published -- A2 parses money, not the collector
    assert flat["gtin13"] == "4006381333931"


def test_external_id_identifies_the_listing_not_the_product() -> None:
    """external_id keys `(source_id, external_id)` idempotency, so it must identify *this site's listing*.

    That is why the site's own url/sku wins over gtin13 even though a GTIN is the globally stronger
    identifier: one site can list the same GTIN twice (different sellers, bundles, variants), and treating
    those as one row would silently overwrite one listing with the other on every collect. Matching
    products across sources is A3's job, working from `ean`, and it happens later.
    """
    with_url, with_sku_and_gtin = (flatten_product(p) for p in extract_products(HTML))
    assert product_external_id(with_url, PAGE_URL, 0).endswith("/soundcore-q30")
    assert product_external_id(with_sku_and_gtin, PAGE_URL, 1) == "CADA-1"
    assert product_external_id({}, PAGE_URL, 7) == f"{PAGE_URL}#product-7"
    # The GTIN is not lost -- it still travels in the payload for A2 to validate and A3 to match on.
    assert with_sku_and_gtin["gtin13"] == "4006381333931"


def test_fetch_yields_one_raw_offer_per_product_offline(tmp_path: Path) -> None:
    write_cached_response(tmp_path, PAGE_URL, HTML)
    collector = JsonLdPageCollector(source_id=1, source_name="lowcydeals", url=PAGE_URL, cache_dir=tmp_path, offline=True)
    offers = list(collector.fetch(None))
    assert len(offers) == 2
    assert offers[0].payload_json["name"] == "soundcore Q30"
    assert offers[0].payload_json["_page_url"] == PAGE_URL
    assert all(o.content_hash for o in offers)


def test_fetch_yields_nothing_without_a_configured_url(tmp_path: Path) -> None:
    collector = JsonLdPageCollector(source_id=1, cache_dir=tmp_path, offline=True)
    assert list(collector.fetch(None)) == []
