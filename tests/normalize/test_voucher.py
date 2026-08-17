"""Tests for voucher detection.

Every "should be rejected" case below is a real title collected live from Pepper on 2026-08-17, together
with the structured price Pepper attached to it. The Foodsi one is the motivating example: Pepper sends
`merchant_attrib = {"price": "10zł"}` for a post offering 10 zł off an order, so the pipeline used to
create a ten-złoty "product".
"""

from __future__ import annotations

import pytest

from dealradar.normalize.voucher import looks_like_voucher

REAL_VOUCHER_TITLES = [
    "422° - 10 zł zniżki na zamówienie w Foodsi",
    "579° - 50 zł zniżki dla nowych na zakupy na Amazon.pl w akcji Back to School",
    "257° - Lucky Mondays! 20 zł rabatu na karty podarunkowe w T-mobile",
    "10 zl znizki do Decathlon za 210 incoinow w aplikacji Inpost",
    "Kod rabatowy -15% na cały asortyment",
    "Bon podarunkowy 100 zł do Empiku",
    "Darmowa dostawa w Zalando bez minimum",
    "20% taniej na wszystko w aplikacji",
]

REAL_PRODUCT_TITLES = [
    "209° - Kapsułki do prania Persil Deep Clean Discs 4w1 color 60 prań Inpost 65gr/1szt",
    "257° - Męskie buty adidas Ultimashow 2.0 za 108 zł - wybrane rozmiary",
    "232° - Rower elektryczny ROMET E-Modeco SUV 1.0 AN D U17 2024",
    "400° - Smartfon Iphone 17 PRO 256GB wszystkie kolory",
    "Realme GT 8 Pro 16/512GB",
    "202° - Procesor AMD Ryzen 7 9700X 5.5GHz wysyłka z PL",
    "Samsung 990 PRO 2TB M.2 NVMe za 649 zł",
    "DANISH ENDURANCE męska ultralekka kurtka wiatrówka z kapturem",
]


@pytest.mark.parametrize("title", REAL_VOUCHER_TITLES)
def test_voucher_titles_are_detected(title: str) -> None:
    assert looks_like_voucher(title) is True


@pytest.mark.parametrize("title", REAL_PRODUCT_TITLES)
def test_real_product_titles_are_not_rejected(title: str) -> None:
    """The costly failure mode is over-rejection: a dropped real product is data we never get back."""
    assert looks_like_voucher(title) is False


def test_description_opening_can_reveal_a_voucher_when_the_title_does_not() -> None:
    assert looks_like_voucher("Promocja w Foodsi", "<strong>10 zł zniżki</strong> na pierwsze zamówienie") is True


def test_a_discount_mentioned_late_in_a_description_does_not_condemn_the_listing() -> None:
    """A product page often plugs an unrelated promo further down; that says nothing about this listing."""
    description = "Świetny laptop Dell XPS 13, 16GB RAM, 512GB SSD. " + ("x" * 150) + " Dodatkowo 10 zł zniżki na kolejne zakupy."
    assert looks_like_voucher("Laptop Dell XPS 13 16/512GB", description) is False
