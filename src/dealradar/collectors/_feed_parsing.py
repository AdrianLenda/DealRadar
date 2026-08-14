"""Low-level, network-agnostic helpers for streaming XML/CSV/TSV feed parsing.

Kept separate from `feed_xml.py` so the parsing logic itself -- the part most worth unit-testing without any
HTTP involved -- can be exercised directly against small in-memory byte buffers in
`tests/collectors/test_feed_xml.py`. Nothing here talks to the network or knows about `RawOffer`/`Collector`.
"""

from __future__ import annotations

import csv
import gzip
import io
from collections.abc import Iterator
from typing import Any, Protocol, cast

from lxml import etree

GZIP_MAGIC = b"\x1f\x8b"


class ReadableBinary(Protocol):
    """Structural type for "anything with a binary .read()".

    Plain file objects, io.BytesIO, and our own stream adapters below all satisfy this without needing to
    match stdlib's stricter (and, for a RawIOBase subclass, not reliably mypy-compatible) `typing.IO[bytes]`.
    """

    def read(self, size: int = ...) -> bytes: ...


# Both stream adapters below subclass io.RawIOBase (not just duck-typing a bare .read()): io.TextIOWrapper
# -- needed to decode CSV/TSV text lazily, including embedded newlines inside quoted fields -- requires a
# real readable-stream object (readable() + readinto()), not merely something with a .read() method.


class _PrependStream(io.RawIOBase):
    """Wraps a binary stream so bytes already consumed for sniffing are replayed before the rest of the stream."""

    def __init__(self, head: bytes, rest: ReadableBinary) -> None:
        """Stores `head` to be replayed before subsequent reads fall through to `rest`."""
        super().__init__()
        self._head: bytes | None = head if head else None
        self._rest = rest

    def readable(self) -> bool:
        """Returns True: this stream always supports read()."""
        return True

    def read(self, size: int = -1) -> bytes:
        """Returns up to `size` bytes (or all remaining when size < 0), yielding buffered `head` bytes first."""
        if self._head is None:
            return self._rest.read(size)
        if size < 0:
            data = self._head + self._rest.read(-1)
            self._head = None
            return data
        if size >= len(self._head):
            data = self._head + self._rest.read(size - len(self._head))
            self._head = None
            return data
        data, self._head = self._head[:size], self._head[size:]
        return data

    def readinto(self, b: Any) -> int:
        """Fills `b` from read(); returns the number of bytes written, as required by the RawIOBase protocol."""
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n


class IterBytesStream(io.RawIOBase):
    """Adapts an iterator of byte chunks (e.g. `httpx.Response.iter_bytes()`) into a `.read()`-able file object.

    This is what lets `feed_xml.py` hand a truly streamed HTTP response body to `lxml.etree.iterparse` (or a
    CSV/TSV feed to `io.TextIOWrapper`) without httpx or us ever materializing the whole response in memory.
    """

    def __init__(self, chunks: Iterator[bytes]) -> None:
        """Wraps `chunks`; nothing is pulled from it until the first `read()` call."""
        super().__init__()
        self._chunks = chunks
        self._buf = b""
        self._exhausted = False

    def readable(self) -> bool:
        """Returns True: this stream always supports read()."""
        return True

    def read(self, size: int = -1) -> bytes:
        """Returns up to `size` bytes (or all remaining when size < 0) by draining the wrapped chunk iterator."""
        while not self._exhausted and (size < 0 or len(self._buf) < size):
            try:
                self._buf += next(self._chunks)
            except StopIteration:
                self._exhausted = True
        if size < 0:
            data, self._buf = self._buf, b""
        else:
            data, self._buf = self._buf[:size], self._buf[size:]
        return data

    def readinto(self, b: Any) -> int:
        """Fills `b` from read(); returns the number of bytes written, as required by the RawIOBase protocol."""
        data = self.read(len(b))
        n = len(data)
        b[:n] = data
        return n


def peek(stream: ReadableBinary, n: int) -> tuple[bytes, ReadableBinary]:
    """Reads up to n bytes from stream for sniffing; returns (sample, stream) where stream still yields them first."""
    sample = stream.read(n)
    return sample, _PrependStream(sample, stream)


def open_maybe_gzip(stream: ReadableBinary) -> tuple[ReadableBinary, bytes]:
    """Transparently gunzips `stream` if it starts with the gzip magic bytes; returns (stream, sample_of_content).

    Only needed for feeds where gzip *is* the document (e.g. a `.xml.gz` download link) -- gzip applied at the
    HTTP transport level (`Content-Encoding: gzip`) is already decoded for us by httpx before we see the bytes.
    """
    sample, stream = peek(stream, 4096)
    if sample[:2] == GZIP_MAGIC:
        gz_stream: ReadableBinary = gzip.GzipFile(fileobj=stream)  # type: ignore[call-overload]
        sample, stream = peek(gz_stream, 4096)
    return stream, sample


def guess_format(url: str, sample: bytes) -> str:
    """Guesses 'xml', 'csv', or 'tsv' from a URL's extension, falling back to sniffing whether sample looks like XML."""
    lowered = url.lower().split("?", 1)[0]
    if lowered.endswith((".tsv", ".tsv.gz")):
        return "tsv"
    if lowered.endswith((".csv", ".csv.gz")):
        return "csv"
    if lowered.endswith((".xml", ".xml.gz")):
        return "xml"
    stripped = sample.lstrip()
    return "xml" if stripped.startswith(b"<") else "csv"


def detect_text_encoding(sample: bytes) -> str:
    """Guesses a text encoding for a CSV/TSV sample: honors a BOM, else utf-8, else falls back to windows-1250.

    windows-1250 shows up often enough in Polish affiliate feeds (per the task spec) that silently mangling it
    via a naive utf-8 decode is not acceptable -- this is the explicit handling that requirement asks for.
    """
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    try:
        sample.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "windows-1250"


def _element_to_record(elem: "etree._Element") -> dict[str, Any]:
    """Flattens one XML record element's direct children into a {local_tag: text} dict, keeping repeats as lists."""
    record: dict[str, Any] = {}
    if elem.attrib:
        record["_attrib"] = dict(elem.attrib)
    for child in elem:
        name = etree.QName(child).localname
        text = "".join(str(part) for part in child.itertext()).strip()
        if name in record:
            existing = record[name]
            record[name] = [*existing, text] if isinstance(existing, list) else [existing, text]
        else:
            record[name] = text
    if not list(elem) and elem.text and elem.text.strip():
        record.setdefault("_text", elem.text.strip())
    return record


def iter_xml_records(stream: ReadableBinary, record_tag: str) -> Iterator[dict[str, Any]]:
    """Streams `<record_tag>` elements from an XML byte stream via iterparse, yielding one flat dict per record.

    Never builds a full DOM: each matched element is cleared (and its already-parsed preceding siblings
    dropped) immediately after being yielded, so memory stays flat regardless of feed size. Matches
    `record_tag` whether or not it sits in an XML namespace.
    """
    context = etree.iterparse(
        stream, events=("end",), tag=(record_tag, f"{{*}}{record_tag}"), recover=True, huge_tree=True
    )
    for _, elem in context:
        yield _element_to_record(elem)
        elem.clear()
        while elem.getprevious() is not None:
            parent = elem.getparent()
            if parent is None:
                break
            del parent[0]


def _dialect_with_delimiter(delimiter: str) -> type[csv.Dialect]:
    """Returns a fresh csv.Dialect subclass using `delimiter`, without mutating the shared `csv.excel` class."""

    class _FixedDelimiterDialect(csv.excel):
        pass

    _FixedDelimiterDialect.delimiter = delimiter
    return _FixedDelimiterDialect


def iter_csv_records(
    stream: ReadableBinary, *, encoding: str | None, delimiter: str | None
) -> Iterator[dict[str, Any]]:
    """Streams rows from a CSV/TSV byte stream as dicts keyed by header, auto-detecting delimiter/encoding.

    Reads lazily via `csv.DictReader` over a `TextIOWrapper`, so memory use stays proportional to one row at a
    time, not the whole file.
    """
    sample, stream = peek(stream, 4096)
    resolved_encoding = encoding or detect_text_encoding(sample)
    # TextIOWrapper's generic stub wants its buffer to satisfy an internal typeshed Protocol stricter than
    # (and not expressible in terms of) the looser ReadableBinary used elsewhere in this module. Every
    # concrete stream this module ever hands it (BytesIO, a real file object, our own RawIOBase-based
    # adapters, gzip.GzipFile) genuinely satisfies TextIOWrapper's real runtime requirements, so this cast
    # only narrows the static type for mypy's sake -- it changes no behaviour.
    text_stream = io.TextIOWrapper(cast(Any, stream), encoding=resolved_encoding, errors="replace", newline="")

    dialect: type[csv.Dialect] | str
    if delimiter is not None:
        dialect = _dialect_with_delimiter(delimiter)
    else:
        sample_text = sample.decode(resolved_encoding, errors="replace")
        try:
            dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

    reader = csv.DictReader(text_stream, dialect=dialect)
    for row in reader:
        yield {k: v for k, v in row.items() if k is not None}
