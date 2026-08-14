"""Loads config/matching.yaml into validated pydantic models. Owner: A3.

Acceptance thresholds for the matcher live in YAML, not in code (task spec: "Progi akceptacji trzymaj
w config/matching.yaml, nie w kodzie") so they can be retuned from `dealradar match --eval` output
without a code change. This module never touches `dealradar.config` (A0-owned) -- matching.yaml is
its own file, loaded independently, because AppConfig has `extra="forbid"` and is not ours to extend.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from dealradar.errors import ConfigError

DEFAULT_MATCHING_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "matching.yaml"


class EanStepConfig(BaseModel):
    """Thresholds for cascade step 1 (exact EAN)."""

    model_config = ConfigDict(extra="forbid")

    confidence: float = Field(ge=0.0, le=1.0)
    conflict_title_sim_min: float = Field(ge=0.0, le=1.0)


class BrandMpnStepConfig(BaseModel):
    """Thresholds for cascade step 2 (brand + MPN)."""

    model_config = ConfigDict(extra="forbid")

    confidence: float = Field(ge=0.0, le=1.0)
    mpn_min_length: int = Field(ge=1)


class FuzzyStepConfig(BaseModel):
    """Thresholds and signal weights for cascade step 3 (blocking + fuzzy text + attribute agreement)."""

    model_config = ConfigDict(extra="forbid")

    price_bucket_pct: float = Field(gt=0.0, le=1.0)
    weight_tfidf: float = Field(ge=0.0, le=1.0)
    weight_token_set_ratio: float = Field(ge=0.0, le=1.0)
    weight_attr_agreement: float = Field(ge=0.0, le=1.0)
    accept_threshold: float = Field(ge=0.0, le=1.0)
    min_confidence: float = Field(ge=0.0, le=1.0)
    max_confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "FuzzyStepConfig":
        """Rejects signal weights that do not sum to ~1.0; returns self unchanged otherwise."""
        total = self.weight_tfidf + self.weight_token_set_ratio + self.weight_attr_agreement
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"fuzzy step signal weights must sum to 1.0, got {total}")
        return self


class AttributeGateConfig(BaseModel):
    """Gate G2 tolerances: which numeric attribute keys must match exactly vs. within a percentage band."""

    model_config = ConfigDict(extra="forbid")

    exact_keys: list[str] = Field(default_factory=list)
    tolerance_pct_keys: dict[str, float] = Field(default_factory=dict)


class ClusterConfig(BaseModel):
    """Cluster-shape sanity thresholds."""

    model_config = ConfigDict(extra="forbid")

    max_size_warn: int = Field(ge=1)


class EvalConfig(BaseModel):
    """Parameters for building the starter, stratified match_review evaluation dataset.

    `score_bands` and `target_band_share` correspond positionally (band i's target share is the
    i-th value of target_band_share, in YAML insertion order) rather than by re-parsing the string
    keys back into numbers -- the keys are documentation for a human reading the YAML, not a lookup
    the loader relies on.
    """

    model_config = ConfigDict(extra="forbid")

    candidate_pairs_min: int = Field(ge=1)
    score_bands: list[tuple[float, float]]
    target_band_share: dict[str, float]

    @model_validator(mode="after")
    def _bands_and_shares_correspond(self) -> "EvalConfig":
        """Rejects a score_bands/target_band_share length mismatch; returns self unchanged otherwise."""
        if len(self.score_bands) != len(self.target_band_share):
            raise ValueError(
                f"eval.score_bands has {len(self.score_bands)} entries but "
                f"eval.target_band_share has {len(self.target_band_share)}; they must correspond 1:1"
            )
        return self


class MatchingConfig(BaseModel):
    """The fully parsed and validated contents of config/matching.yaml."""

    model_config = ConfigDict(extra="forbid")

    ean: EanStepConfig
    brand_mpn: BrandMpnStepConfig
    fuzzy: FuzzyStepConfig
    attribute_gate: AttributeGateConfig
    cluster: ClusterConfig
    eval: EvalConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    """Reads and parses one YAML file into a dict; raises ConfigError if it is missing or not a mapping."""
    if not path.exists():
        raise ConfigError(f"missing required config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ConfigError(f"expected a YAML mapping at the top level of {path}, got {type(data).__name__}")
    return data


def load_matching_config(path: Path | None = None) -> MatchingConfig:
    """Loads and validates config/matching.yaml into a MatchingConfig; raises ConfigError on any problem."""
    target = path or DEFAULT_MATCHING_CONFIG_PATH
    raw = _read_yaml(target)
    try:
        return MatchingConfig.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError, normalized to our own error type
        raise ConfigError(f"invalid matching configuration at {target}: {exc}") from exc
