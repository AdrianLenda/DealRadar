"""Tests for dealradar.normalize.fx -- NBP-rate caching and PLN conversion, with the network fully stubbed out.

No test in this file (or anywhere else in tests/normalize/) makes a real network call: `http_get` is always
a fake function injected by the test, and the cache directory is always a pytest tmp_path. This matches the
project-wide expectation (see A1's own task spec) that pytest never depends on live network access.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from dealradar.normalize.fx import convert_to_pln_grosze, get_fx_rate_to_pln


def _fake_http_get(rate: float, table_no: str = "155/A/NBP/2026") -> Any:
    calls: list[str] = []

    def _get(url: str) -> dict[str, Any]:
        calls.append(url)
        return {"rates": [{"no": table_no, "mid": rate}]}

    _get.calls = calls  # type: ignore[attr-defined]
    return _get


def test_pln_short_circuits_without_any_lookup(tmp_path: Path) -> None:
    def _should_never_be_called(url: str) -> dict[str, Any]:
        raise AssertionError("must not be called for PLN")

    result = get_fx_rate_to_pln("PLN", date(2026, 8, 9), cache_dir=tmp_path, http_get=_should_never_be_called)
    assert result == (1.0, "PLN")


def test_fetches_and_caches_rate(tmp_path: Path) -> None:
    fake = _fake_http_get(4.32)
    result = get_fx_rate_to_pln("EUR", date(2026, 8, 9), cache_dir=tmp_path, http_get=fake)
    assert result == (4.32, "155/A/NBP/2026")
    assert len(fake.calls) == 1  # type: ignore[attr-defined]

    # Second call for the same (currency, date) must hit the cache, not the network.
    result2 = get_fx_rate_to_pln("EUR", date(2026, 8, 9), cache_dir=tmp_path, http_get=fake)
    assert result2 == (4.32, "155/A/NBP/2026")
    assert len(fake.calls) == 1  # type: ignore[attr-defined]  # still 1 -- no second network call


def test_walks_back_over_a_no_rate_day(tmp_path: Path) -> None:
    """NBP publishes no rate on weekends/holidays; a 404-like empty response should fall back a day."""
    call_log: list[str] = []

    def _get(url: str) -> dict[str, Any]:
        call_log.append(url)
        if len(call_log) == 1:
            return {"rates": []}  # simulate "no rate published today" (e.g. a Sunday)
        return {"rates": [{"no": "1/A/NBP/2026", "mid": 4.5}]}

    result = get_fx_rate_to_pln("USD", date(2026, 8, 9), cache_dir=tmp_path, http_get=_get)
    assert result == (4.5, "1/A/NBP/2026")
    assert len(call_log) == 2


def test_returns_none_when_no_rate_found_within_the_lookback_window(tmp_path: Path) -> None:
    def _always_empty(url: str) -> dict[str, Any]:
        return {"rates": []}

    result = get_fx_rate_to_pln("USD", date(2026, 8, 9), cache_dir=tmp_path, http_get=_always_empty)
    assert result is None


def test_returns_none_on_transport_failure(tmp_path: Path) -> None:
    def _boom(url: str) -> dict[str, Any]:
        raise ConnectionError("simulated network failure")

    result = get_fx_rate_to_pln("USD", date(2026, 8, 9), cache_dir=tmp_path, http_get=_boom)
    assert result is None


def test_convert_to_pln_grosze_pln_is_a_no_op() -> None:
    result = convert_to_pln_grosze(19999, "PLN", date(2026, 8, 9))
    assert result is not None
    amount, evidence = result
    assert amount == 19999
    assert evidence == {"fx": "PLN, no conversion"}


def test_convert_to_pln_grosze_applies_rate_and_records_evidence(tmp_path: Path) -> None:
    fake = _fake_http_get(4.30)
    result = convert_to_pln_grosze(1000, "EUR", date(2026, 8, 9), cache_dir=tmp_path, http_get=fake)
    assert result is not None
    amount, evidence = result
    assert amount == 4300  # 1000 grosze (10.00 EUR) * 4.30 -> 4300 grosze (43.00 PLN)
    assert evidence["fx_rate"] == 4.30
    assert evidence["fx_currency"] == "EUR"
    assert evidence["fx_source"] == "NBP table A"
    assert evidence["fx_as_of"] == "2026-08-09"


def test_convert_to_pln_grosze_returns_none_when_rate_unavailable(tmp_path: Path) -> None:
    def _always_empty(url: str) -> dict[str, Any]:
        return {"rates": []}

    result = convert_to_pln_grosze(1000, "USD", date(2026, 8, 9), cache_dir=tmp_path, http_get=_always_empty)
    assert result is None
