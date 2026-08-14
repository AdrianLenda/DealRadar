"""Offline tests for AliexpressCollector.

No AliExpress affiliate credentials exist in this environment (see the A1 final report), so only the
credential-missing "disabled" path -- the one the task spec explicitly requires -- is exercised end to end
here. `_signed_params` is checked separately as a pure function: it is implemented per AliExpress's own
published (external) signing contract but has not been run against their live API.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dealradar.collectors.aliexpress import AliexpressCollector


def test_fetch_yields_nothing_and_does_not_raise_when_credentials_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ALIEXPRESS_APP_KEY", raising=False)
    monkeypatch.delenv("ALIEXPRESS_APP_SECRET", raising=False)
    collector = AliexpressCollector(source_id=1, keywords=["ssd"], cache_dir=tmp_path, offline=True)
    assert list(collector.fetch(None)) == []


def test_fetch_yields_nothing_when_only_one_credential_is_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALIEXPRESS_APP_KEY", "only-key-no-secret")
    monkeypatch.delenv("ALIEXPRESS_APP_SECRET", raising=False)
    collector = AliexpressCollector(source_id=1, cache_dir=tmp_path, offline=True)
    assert list(collector.fetch(None)) == []


def test_signed_params_includes_a_sign_and_the_configured_keyword(tmp_path: Path) -> None:
    collector = AliexpressCollector(source_id=1, cache_dir=tmp_path, offline=True)
    params = collector._signed_params("app-key-123", "app-secret-456", "powerbank", 1)
    assert params["app_key"] == "app-key-123"
    assert params["keywords"] == "powerbank"
    assert isinstance(params["sign"], str) and len(params["sign"]) == 32  # MD5 hex digest length
