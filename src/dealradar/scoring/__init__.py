"""Etap 4-5: price history + deal_score, and the 1-10 quality score. Owners: A4 (history.py, deal.py), A5 (quality.py).

Originally empty — A0 only reserved this package. See ARCHITEKTURA.md sections 8-9 and
prompty/A4-historia-i-deal-score.md / prompty/A5-ocena-jakosci.md for the contracts implemented here
(`snapshot_prices`, `score_deals`, `price_series`, `score_quality`, `build_peer_group`).

A4 re-exports its own public surface below (additive only — A5's `quality.py` re-exports land here too,
independently, when that work starts; nothing here should need to change to accommodate it).
"""

from __future__ import annotations

from dealradar.scoring.deal import DealReport, score_deals
from dealradar.scoring.history import (
    CompactionReport,
    ScoringConfig,
    SnapshotReport,
    compact_price_history,
    load_scoring_config,
    price_series,
    snapshot_prices,
)

__all__ = [
    "CompactionReport",
    "DealReport",
    "ScoringConfig",
    "SnapshotReport",
    "compact_price_history",
    "load_scoring_config",
    "price_series",
    "score_deals",
    "snapshot_prices",
]
