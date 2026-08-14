"""Generic affiliate-network product feed collector (XML/CSV/TSV) -- ARCHITEKTURA.md section 6.

This is the project's main multiplier: one collector, driven entirely by per-source configuration in
`config/sources.yaml`, can ingest product feeds from Awin / TradeDoubler / Convertiser / Adtraction and
similar affiliate networks without any source-specific code. Records are streamed -- `lxml.etree.iterparse`
for XML, line-by-line `csv.DictReader` for CSV/TSV -- so a 500MB feed never has to be held fully parsed in
memory.

Fields are passed through completely raw and unrenamed: this collector does not know what an EAN or a price
*means*, only where the record boundaries are. A2 owns turning raw field names (`ean`/`gtin`/`barcode`,
`price`/`price_gross`/`cena`) into canonical `Offer` fields (Z1: raw data is untouchable).
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from dealradar.errors import CollectorError
from dealradar.models import RawOffer, SourceKind, compute_content_hash

from . import BaseCollector
from ._feed_parsing import (
    IterBytesStream,
    ReadableBinary,
    guess_format,
    iter_csv_records,
    iter_xml_records,
    open_maybe_gzip,
)

logger = logging.getLogger("dealradar.collectors.feed_xml")

# Field names commonly used for a stable per-record id across affiliate networks, tried in order when a
# source doesn't configure an explicit `id_field`.
_ID_CANDIDATE_FIELDS = ("id", "sku", "product_id", "aw_product_id", "offer_id", "ean", "gtin")


class FeedXmlCollector(BaseCollector):
    """Streams one affiliate product feed (XML or CSV/TSV, plain or gzip) and yields it as raw records.

    Configured per source via `config/sources.yaml` extra fields (`SourceConfig` has `extra="allow"`, so no
    change to `config.py` is needed to add a field):

    - `feed_format`: "xml" / "csv" / "tsv". Guessed from the URL extension or content when omitted.
    - `record_tag`: XML only -- the local name of the repeating record element (e.g. "product"). Required
      when `feed_format` resolves to "xml"; there is no safe way to guess it without buffering the feed.
    - `id_field`: the raw field holding a stable per-record id. Falls back to a short content hash of the
      record when absent or empty on a given row, which is still stable run-to-run for unchanged content.
    - `encoding`, `delimiter`: CSV/TSV overrides. Auto-detected otherwise (delimiter via `csv.Sniffer`,
      encoding via a BOM check / utf-8 probe with a windows-1250 fallback -- see `_feed_parsing.py`).
    """

    kind: SourceKind = "feed"

    def __init__(
        self,
        *,
        source_id: int,
        source_name: str,
        url: str,
        feed_format: str | None = None,
        record_tag: str | None = None,
        id_field: str | None = None,
        encoding: str | None = None,
        delimiter: str | None = None,
        rps: float = 2.0,
        cache_dir: Path | None = None,
        offline: bool = False,
        local_path: Path | None = None,
        **kwargs: Any,
    ) -> None:
        """Configures this feed's URL, format, and field mapping; opens no connection until fetch() runs.

        `local_path`, when given, bypasses HTTP entirely and reads the feed from a local file -- used by
        tests to run fully offline against a recorded fixture without touching BaseCollector's URL-keyed
        response cache.
        """
        super().__init__(source_id, rps=rps, cache_dir=cache_dir, offline=offline, **kwargs)
        self.source_name = source_name
        self.url = url
        self.feed_format = feed_format
        self.record_tag = record_tag
        self.id_field = id_field
        self.encoding = encoding
        self.delimiter = delimiter
        self.local_path = local_path

    def fetch(self, since: datetime | None) -> Iterator[RawOffer]:
        """Yields one RawOffer per feed record, streaming the whole feed without ever holding it fully parsed."""
        for record in self._iter_raw_records():
            external_id = self._external_id(record)
            yield self.make_raw_offer(external_id, record)

    def _iter_raw_records(self) -> Iterator[dict[str, Any]]:
        """Dispatches to a local file, the on-disk response cache (offline mode), or a true streaming GET."""
        if self.local_path is not None:
            with open(self.local_path, "rb") as fh:
                yield from self._parse(fh)
            return
        if self.offline:
            # Reuses BaseCollector's URL-keyed disk cache / offline replay -- appropriate for the small
            # (~50-record) recorded fixtures the task spec asks for. A real multi-hundred-MB feed in
            # production instead goes through _fetch_streaming below, which never buffers the full body.
            cached_response = self.get(self.url)
            yield from self._parse(io.BytesIO(cached_response.content))
            return
        streaming_response = self._fetch_streaming()
        if streaming_response is None:
            return  # 304 Not Modified -- nothing changed since the last run, so nothing to re-parse.
        try:
            yield from self._parse(IterBytesStream(streaming_response.iter_bytes()))
        finally:
            streaming_response.close()

    def _fetch_streaming(self) -> httpx.Response | None:
        """Opens a true streaming GET (no full-body buffering) for a production-size feed; returns None on 304.

        Deliberately bypasses `BaseCollector.get()`: that helper always fully buffers the response body
        (`response.text`) to populate its cache, which is exactly the behaviour a 500MB feed cannot afford.
        Rate limiting still goes through the shared token bucket, and conditional GET headers make repeat
        runs against an unchanged feed cheap (ETag/Last-Modified, per the task spec).
        """
        self._bucket.acquire()
        headers = self._load_conditional_headers()
        try:
            response = self.client.send(self.client.build_request("GET", self.url, headers=headers), stream=True)
        except httpx.TransportError as exc:
            raise CollectorError(f"feed fetch transport error: {self.url}: {exc}") from exc
        if response.status_code == 304:
            response.close()
            logger.info("source=%s feed unchanged since last fetch (304 Not Modified)", self.source_name)
            return None
        if response.status_code >= 400:
            response.close()
            raise CollectorError(f"feed fetch failed: {self.url} status={response.status_code}")
        self._save_conditional_headers(response)
        return response

    def _state_path(self) -> Path | None:
        """Returns where this feed's ETag/Last-Modified state is persisted, or None when no cache_dir is set."""
        if self.cache_dir is None:
            return None
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        return self.cache_dir / "_conditional_state.json"

    def _load_conditional_headers(self) -> dict[str, str]:
        """Returns {"If-None-Match": ..., "If-Modified-Since": ...} from persisted state, or {} if none exists."""
        path = self._state_path()
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        headers: dict[str, str] = {}
        etag = data.get("etag")
        if isinstance(etag, str):
            headers["If-None-Match"] = etag
        last_modified = data.get("last_modified")
        if isinstance(last_modified, str):
            headers["If-Modified-Since"] = last_modified
        return headers

    def _save_conditional_headers(self, response: httpx.Response) -> None:
        """Persists response's ETag/Last-Modified headers (if any) so the next run can send a conditional GET."""
        path = self._state_path()
        if path is None:
            return
        state: dict[str, str] = {}
        if "ETag" in response.headers:
            state["etag"] = response.headers["ETag"]
        if "Last-Modified" in response.headers:
            state["last_modified"] = response.headers["Last-Modified"]
        if state:
            path.write_text(json.dumps(state), encoding="utf-8")

    def _parse(self, raw: ReadableBinary) -> Iterator[dict[str, Any]]:
        """Detects gzip and format on `raw`'s leading bytes, then delegates to the matching streaming parser."""
        stream, sample = open_maybe_gzip(raw)
        fmt = self.feed_format or guess_format(self.url, sample)
        if fmt == "xml":
            if not self.record_tag:
                raise CollectorError(
                    f"source={self.source_name}: xml feed requires 'record_tag' to be set in config/sources.yaml"
                )
            yield from iter_xml_records(stream, self.record_tag)
        elif fmt in ("csv", "tsv"):
            delimiter = self.delimiter or ("\t" if fmt == "tsv" else None)
            yield from iter_csv_records(stream, encoding=self.encoding, delimiter=delimiter)
        else:
            raise CollectorError(f"source={self.source_name}: unsupported feed_format {fmt!r}")

    def _external_id(self, record: dict[str, Any]) -> str:
        """Returns a stable per-record id: the configured/candidate id field's value, or a content-hash fallback."""
        candidates = ((self.id_field,) if self.id_field else ()) + _ID_CANDIDATE_FIELDS
        for field in candidates:
            value = record.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return compute_content_hash(record)[:24]


