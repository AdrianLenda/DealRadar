"""iBood daily-deals feed collector -- ARCHITEKTURA.md section 6.

Small volume (iBood runs a handful of deals per day), simple JSON shape. Exists mainly to serve as the
smallest possible end-to-end pipeline test: fetch -> RawOffer -> raw_offer table. No pagination, no auth.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from dealradar.models import RawOffer, SourceKind, compute_content_hash

from . import BaseCollector


class IboodCollector(BaseCollector):
    """Fetches iBood's daily deals feed (JSON: a bare list, or {"deals": [...]}) and yields each deal raw."""

    kind: SourceKind = "feed"

    def __init__(
        self,
        *,
        source_id: int,
        source_name: str = "ibood",
        url: str,
        rps: float = 1.0,
        cache_dir: Path | None = None,
        offline: bool = False,
        **kwargs: Any,
    ) -> None:
        """Configures this collector's feed URL; opens no connection until fetch() runs."""
        super().__init__(source_id, rps=rps, cache_dir=cache_dir, offline=offline, **kwargs)
        self.source_name = source_name
        self.url = url

    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:
        """Yields one RawOffer per deal in the feed at self.url; skips entries with neither an id nor content."""
        response = self.get(self.url)
        data = response.json()
        deals = data.get("deals", []) if isinstance(data, dict) else data
        if not isinstance(deals, list):
            return
        for deal in deals:
            if not isinstance(deal, dict) or not deal:
                continue
            external_id = _external_id(deal)
            yield self.make_raw_offer(external_id, deal)


def _external_id(deal: dict[str, Any]) -> str:
    """Returns a stable per-deal id: the deal's own id field if present, else a content hash of the record."""
    for field in ("id", "deal_id", "sku"):
        value = deal.get(field)
        if value is not None and str(value).strip():
            return str(value).strip()
    return compute_content_hash(deal)[:24]
