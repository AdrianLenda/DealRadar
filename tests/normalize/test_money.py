"""Tests for dealradar.normalize.money -- price string parsing across >= 20 real-world formats.

Includes the classic PL-vs-US decimal/thousands separator trap the A2 task spec calls out explicitly:
"1.299,00" (Polish: dot=thousands, comma=decimal) must parse to the same amount as "1299,00" while
"1,299.00" (US: comma=thousands, dot=decimal) must ALSO parse to that same amount -- using only the string
itself to tell them apart.
"""

from __future__ import annotations

import pytest

from dealradar.normalize.money import detect_currency, parse_amount_to_grosze


@pytest.mark.parametrize(
    "raw,expected_grosze",
    [
        # -- plain formats --
        ("1299.00", 129900),
        ("1299,00", 129900),
        ("1299", 129900),
        (1299, 129900),
        (1299.0, 129900),
        (1299.5, 129950),
        # -- currency-suffixed / prefixed --
        ("1 299,00 zł", 129900),
        ("1299.00 PLN", 129900),
        ("1299,00zł", 129900),
        ("1299 zł", 129900),
        ("od 1299 zł", 129900),
        ("Cena: 249,99 zł", 24999),
        ("cena 249.99 zł", 24999),
        # -- the PL-vs-US separator trap --
        ("1.299,00", 129900),  # PL: dot=thousands, comma=decimal
        ("1,299.00", 129900),  # US: comma=thousands, dot=decimal -- must land on the SAME amount
        ("12.999,99", 1299999),
        ("12,999.99", 1299999),
        # -- thousands separator with no decimals --
        ("3 500 zł", 350000),
        ("3.500", 350000),  # dot as thousands sep, no decimal remainder (3 digits after dot)
        ("3,500", 350000),  # comma as thousands sep, no decimal remainder (3 digits after comma)
        # -- single-digit / short fractional part --
        ("1299,5 zł", 129950),
        ("19,9", 1990),
        # -- whole numbers, various spacing --
        ("2000", 200000),
        ("0", 0),
        ("0 zł", 0),
        # -- edge / negative / garbage --
        (None, None),
        ("", None),
        ("   ", None),
        ("brak ceny", None),
        ("zapytaj o cenę", None),
        ("-50 zł", None),  # negative price is nonsensical -> None, not a fabricated positive/zero
        (True, None),  # bool is an int subclass; must never be treated as a price
    ],
)
def test_parse_amount_to_grosze(raw: object, expected_grosze: int | None) -> None:
    assert parse_amount_to_grosze(raw) == expected_grosze


def test_pl_and_us_separator_variants_agree() -> None:
    """"1.299,00" (PL) and "1,299.00" (US) must both resolve to the identical amount -- the whole point of the trap."""
    assert parse_amount_to_grosze("1.299,00") == parse_amount_to_grosze("1,299.00") == 129900


def test_result_is_always_a_plain_int() -> None:
    result = parse_amount_to_grosze("199,99 zł")
    assert isinstance(result, int)
    assert not isinstance(result, bool)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("19.99 EUR", "EUR"),
        ("19.99 USD", "USD"),
        ("19,99 zł", "PLN"),
        ("19.99", "PLN"),  # default when nothing is stated
        ("€19.99", "EUR"),
        ("$19.99", "USD"),
    ],
)
def test_detect_currency(raw: str, expected: str) -> None:
    assert detect_currency(raw) == expected
