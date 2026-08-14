"""Etap 3: match (offer -> product clusters). Owner: A3.

Public API -- everything downstream code (cli.py's `match` command) should need:

    match_offers(session, full=False) -> MatchReport   # etap 3: EAN -> brand+MPN -> fuzzy cascade
    evaluate(session) -> EvalReport                     # precision/recall vs match_review ground truth
    build_eval_dataset(session, config, observed_at) -> EvalDatasetReport   # starter labeled pairs

See ARCHITEKTURA.md section 7 and prompty/A3-matcher.md for the contract, and the A3 final report
for the precision/recall numbers this implementation achieves on its own synthetic fixtures.
"""

from __future__ import annotations

from dealradar.matching.config import MatchingConfig, load_matching_config
from dealradar.matching.eval_dataset import EvalDatasetReport, build_eval_dataset
from dealradar.matching.service import EvalReport, MatchReport, ThresholdRow, evaluate, match_offers

__all__ = [
    "MatchingConfig",
    "load_matching_config",
    "MatchReport",
    "EvalReport",
    "ThresholdRow",
    "match_offers",
    "evaluate",
    "EvalDatasetReport",
    "build_eval_dataset",
]
