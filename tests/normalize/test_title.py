"""Tests for dealradar.normalize.title -- title_norm: junk-stripped, lowercased, diacritics preserved."""

from __future__ import annotations

from dealradar.normalize.title import normalize_title


def test_lowercases_and_collapses_whitespace() -> None:
    assert normalize_title("Dysk   SSD   Samsung") == "dysk ssd samsung"


def test_strips_junk_phrases() -> None:
    result = normalize_title("Powerbank Anker PowerCore 20000 mAh NOWY FV23 GWARANCJA 24m PROMOCJA")
    assert "nowy" not in result
    assert "fv" not in result
    assert "gwarancja" not in result
    assert "promocja" not in result
    assert "powerbank" in result and "anker" in result and "powercore" in result


def test_strips_percent_off_and_bracketed_deal_tags() -> None:
    result = normalize_title('[Deal] LG UltraGear 27GP850 27" 165Hz -30%')
    assert "deal" not in result
    assert "-30%" not in result
    assert "30%" not in result
    assert "lg ultragear 27gp850" in result


def test_strips_wysylka_and_raty_phrases() -> None:
    result = normalize_title("Laptop Dell WYSYŁKA 24H RATY 0%")
    assert "wysyłka" not in result
    assert "24h" not in result or "wysyłka 24h" not in result
    assert "raty" not in result


def test_preserves_polish_diacritics() -> None:
    """Diacritics must be kept -- transliterating would desync TF-IDF from correctly-accented competitor titles."""
    result = normalize_title("Słuchawki bezprzewodowe z redukcją szumów")
    assert "słuchawki" in result
    assert "sluchawki" not in result
    assert "redukcją" in result
    assert "szumów" in result


def test_strips_decorative_emoji() -> None:
    result = normalize_title("🔥 Dysk SSD 1TB M.2 🔥🔥 SUPER CENA!!!")
    assert "🔥" not in result
    assert "dysk ssd 1tb m.2" in result


def test_idempotent_on_already_clean_title() -> None:
    clean = "dysk ssd samsung 990 pro 2tb m.2 nvme"
    assert normalize_title(clean) == clean


def test_empty_and_whitespace_only_title() -> None:
    assert normalize_title("") == ""
    assert normalize_title("   ") == ""
