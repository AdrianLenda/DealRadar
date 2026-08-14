"""Tests for dealradar.normalize.identifiers -- EAN sanitizing and MPN extraction/normalization."""

from __future__ import annotations

import pytest

from dealradar.models import validate_gtin
from dealradar.normalize.identifiers import extract_mpn_from_title, normalize_mpn, sanitize_ean


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4006381333931", "4006381333931"),
        ("5901234 123457", "5901234123457"),
        ("5901234-123457", "5901234123457"),
        ("4006381333931.0", "4006381333931"),
        ("  4006381333931  ", "4006381333931"),
        (4006381333931, "4006381333931"),
        (None, None),
        ("", None),
        ("brak", None),
    ],
)
def test_sanitize_ean(raw: object, expected: str | None) -> None:
    assert sanitize_ean(raw) == expected


def test_sanitize_ean_output_still_needs_checksum_validation() -> None:
    """sanitize_ean only strips formatting -- it does not validate; validate_gtin is the real gate (owned by A0)."""
    sanitized = sanitize_ean("4006381333932")  # valid EAN with the check digit flipped
    assert sanitized == "4006381333932"
    assert validate_gtin(sanitized) is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("mz-v9p2t0bw", "MZV9P2T0BW"),
        ("MZ-V9P2T0BW/EU", "MZV9P2T0BW"),
        ("27GP850-B-EU", "27GP850B"),
        ("ABC123-PL", "ABC123"),
        ("ABC-123/A", "ABC123"),
        ("simple", "SIMPLE"),
        (" spaced out ", "SPACEDOUT"),
    ],
)
def test_normalize_mpn(raw: str, expected: str) -> None:
    assert normalize_mpn(raw) == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Dysk SSD Samsung 990 PRO 2TB M.2 NVMe MZ-V9P2T0BW", "MZ-V9P2T0BW"),
        ("Monitor LG UltraGear 27GP850-B 27\" 165Hz", "27GP850-B"),
    ],
)
def test_extract_mpn_from_title_finds_unambiguous_token(title: str, expected: str) -> None:
    assert extract_mpn_from_title(title) == expected


@pytest.mark.parametrize(
    "title",
    [
        "Dysk SSD Samsung 990 PRO 2TB M.2 NVMe",  # no plausible MPN token at all
        "Powerbank Anker PowerCore 20000mAh 22.5W",  # only unit-bearing tokens (20000MAH), correctly excluded
        "Laptop Dell Latitude 5420 poleasingowy i5/16GB",  # pure-digit-ish tokens only
    ],
)
def test_extract_mpn_from_title_returns_none_when_no_confident_candidate(title: str) -> None:
    assert extract_mpn_from_title(title) is None
