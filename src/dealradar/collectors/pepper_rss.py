"""Pepper.pl hot-deals RSS collector -- ARCHITEKTURA.md section 6.

This is the system's "fast lane": Pepper's community has already filtered for interesting deals, so this
feed is meant to be polled hourly rather than nightly. Records are deliberately thin (title, link, a price
sometimes embedded in the title, a description that may contain HTML) -- we save the item exactly as parsed
and leave extracting anything usable from it to A2. When present, `pepper_temperature` (the community's vote
count / "heat" score) is preserved in the payload as an independent quality signal.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

from lxml import etree

from dealradar.models import RawOffer, SourceKind, compute_content_hash

from . import BaseCollector

# Matches a "temperature" badge such as "456°" or "1 234°", however it appears in a Pepper RSS item's title
# or description. Best-effort only: if the feed's HTML changes, this simply stops matching and
# pepper_temperature is omitted -- never fabricated as 0 (Z4 spirit).
_TEMPERATURE_RE = re.compile(r"(\d[\d\s]{0,6})\s*°")


class PepperRssCollector(BaseCollector):
    """Fetches Pepper.pl's hot-deals RSS channel and yields each `<item>` as a raw record."""

    kind: SourceKind = "rss"

    def __init__(
        self,
        *,
        source_id: int,
        source_name: str = "pepper",
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
        """Yields one RawOffer per RSS `<item>` in the feed at self.url."""
        response = self.get(self.url)
        root = etree.fromstring(response.content, parser=etree.XMLParser(recover=True))
        if root is None:
            return
        for item in root.iter():
            if etree.QName(item).localname != "item":
                continue
            record = _item_to_record(item)
            external_id = _external_id(record)
            yield self.make_raw_offer(external_id, record)


def _item_to_record(item: "etree._Element") -> dict[str, Any]:
    """Flattens one RSS <item>'s children into a {tag: text} dict and adds pepper_temperature when detectable."""
    record: dict[str, Any] = {}
    for child in item:
        name = etree.QName(child).localname
        text = "".join(str(part) for part in child.itertext()).strip()
        if name in record:
            existing = record[name]
            record[name] = [*existing, text] if isinstance(existing, list) else [existing, text]
        else:
            record[name] = text
        if child.attrib:
            record[f"{name}_attrib"] = dict(child.attrib)

    haystack = f"{record.get('title', '')} {record.get('description', '')}"
    match = _TEMPERATURE_RE.search(haystack)
    if match:
        digits = match.group(1).replace(" ", "")
        if digits.isdigit():
            record["pepper_temperature"] = int(digits)
    return record


def _external_id(record: dict[str, Any]) -> str:
    """Returns a stable per-item id: the RSS guid, else the link, else a content hash of the whole item."""
    guid = record.get("guid")
    if isinstance(guid, str) and guid.strip():
        return guid.strip()
    link = record.get("link")
    if isinstance(link, str) and link.strip():
        return link.strip()
    return compute_content_hash(record)[:24]
