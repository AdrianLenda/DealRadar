"""FX conversion for non-PLN offers, with an on-disk cache (A2 task spec: "w osobnym module z cache'em").

This is the one place in the normalize package allowed to touch the network (NBP's public exchange-rate
API) -- everything else in this package is a pure function of already-fetched data. Every conversion records
the rate, its NBP table id/date, and the source in the returned evidence dict: "inaczej za pół roku nie
odtworzysz, skąd wzięła się liczba" (A2 task spec, section 1).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger("dealradar.normalize.fx")

NBP_TABLE_A_URL = "https://api.nbp.pl/api/exchangerates/rates/a/{code}/{date}/?format=json"
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[3] / ".fx_cache"

HttpGet = Callable[[str], dict[str, Any]]


def _default_http_get(url: str) -> dict[str, Any]:
    """Performs a real GET against `url` and returns the parsed JSON body; the only network call A2 makes."""
    import httpx  # imported lazily so importing this module never requires a live network stack in tests

    response = httpx.get(url, timeout=10.0)
    response.raise_for_status()
    result: dict[str, Any] = response.json()
    return result


def _cache_path(cache_dir: Path, currency: str, as_of: date) -> Path:
    """Returns the on-disk cache file path for one (currency, date) NBP rate lookup."""
    return cache_dir / f"{currency.upper()}_{as_of.isoformat()}.json"


def get_fx_rate_to_pln(
    currency: str,
    as_of: date,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    http_get: HttpGet = _default_http_get,
) -> tuple[float, str] | None:
    """Returns (mid_rate, nbp_table_no) converting one unit of `currency` to PLN on `as_of`, or None on failure.

    Reads/writes a JSON cache file per (currency, date) under cache_dir so repeated runs and tests never hit
    the network twice for the same day. NBP publishes no rate on weekends/holidays; on a 404 this walks back
    up to 7 calendar days to find the last published rate, same as any FX-on-a-given-day lookup must.
    """
    currency = currency.upper()
    if currency == "PLN":
        return 1.0, "PLN"

    cache_dir.mkdir(parents=True, exist_ok=True)
    path = _cache_path(cache_dir, currency, as_of)
    if path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        return float(cached["rate"]), str(cached["table_no"])

    from datetime import timedelta

    for delta in range(8):  # today, then walk back over the weekend/holiday gap NBP leaves
        probe_date = as_of - timedelta(days=delta)
        url = NBP_TABLE_A_URL.format(code=currency, date=probe_date.isoformat())
        try:
            payload = http_get(url)
        except Exception as exc:  # noqa: BLE001 - any transport/HTTP-status failure means "try an earlier day"
            logger.info("fx lookup failed for %s on %s: %s", currency, probe_date, exc)
            continue
        rates = payload.get("rates") or []
        if not rates:
            continue
        rate = float(rates[0]["mid"])
        table_no = str(rates[0].get("no", "?"))
        path.write_text(json.dumps({"rate": rate, "table_no": table_no, "effective_date": probe_date.isoformat()}), encoding="utf-8")
        return rate, table_no

    return None


def convert_to_pln_grosze(
    amount_grosze: int,
    currency: str,
    as_of: date,
    *,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    http_get: HttpGet = _default_http_get,
) -> tuple[int, dict[str, Any]] | None:
    """Converts an amount already in `currency`'s grosze/cents to PLN grosze; returns (amount, evidence) or None.

    The evidence dict records everything needed to reconstruct the number later (Z6): the rate applied, the
    NBP table id, the original amount and currency, and the lookup date.
    """
    if currency.upper() == "PLN":
        return amount_grosze, {"fx": "PLN, no conversion"}

    result = get_fx_rate_to_pln(currency, as_of, cache_dir=cache_dir, http_get=http_get)
    if result is None:
        return None
    rate, table_no = result
    converted = (Decimal(amount_grosze) * Decimal(str(rate))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    evidence = {
        "fx_rate": rate,
        "fx_currency": currency.upper(),
        "fx_nbp_table_no": table_no,
        "fx_as_of": as_of.isoformat(),
        "fx_source": "NBP table A",
        "fx_original_amount_minor": amount_grosze,
    }
    return int(converted), evidence
