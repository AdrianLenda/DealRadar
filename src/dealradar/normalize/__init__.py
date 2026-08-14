"""Etap 2: normalize (raw_offer -> offer). Owner: A2.

Public contract (see prompty/A2-normalizator.md and ARCHITEKTURA.md sections 4-5):

- `normalize_batch(session, since)` -- the exact function named in the task spec: reads raw_offer, writes
  `offer` (accepted) and `rejected_offer` (rejected, with a reason -- see `report.RejectedOffer`), returns a
  `NormalizeReport`.
- `run_normalize(session, since=None)` -- thin alias of the above, for callers that don't need since to be
  mandatory.
- `fill_run_report(session, report, since)` -- the one function cli.py's `normalize` command calls; adapts
  a NormalizeReport onto A0's shared `RunReport`/`run_log` machinery.
- `SourceNormalizer` -- the per-source protocol (`source_name` + `to_offer`); `GenericNormalizer` is the one
  concrete, YAML-config-driven implementation this package ships.
"""

from __future__ import annotations

from dealradar.normalize.pipeline import (
    GenericNormalizer,
    SourceNormalizer,
    fill_run_report,
    normalize_batch,
    run_normalize,
)
from dealradar.normalize.report import NormalizeReport, RejectedOffer, RejectReason, SourceCoverage

__all__ = [
    "GenericNormalizer",
    "NormalizeReport",
    "RejectReason",
    "RejectedOffer",
    "SourceCoverage",
    "SourceNormalizer",
    "fill_run_report",
    "normalize_batch",
    "run_normalize",
]
