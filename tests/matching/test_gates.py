"""Unit tests for the hard gates and gate G2 (attribute agreement) -- ARCHITEKTURA.md F2 / Z5."""

from __future__ import annotations

from dealradar.matching.config import AttributeGateConfig
from dealradar.matching.gates import attribute_agreement_score, attribute_gate_conflict, hard_gate_conflict
from dealradar.matching.records import OfferRecord

GATE_CFG = AttributeGateConfig(
    exact_keys=["capacity_gb"],
    tolerance_pct_keys={"weight_kg": 0.02},
)


def _record(**overrides: object) -> OfferRecord:
    base: dict[str, object] = dict(
        id=1, source_id=1, title="t", title_norm="t", brand="B", brand_norm="b", mpn=None, mpn_norm=None,
        ean=None, category_id=1, condition="new", is_bundle=False, bundle_size=None, price_total=1000,
        product_id=None, match_confidence=None, match_method=None, attrs={},
    )
    base.update(overrides)
    return OfferRecord(**base)  # type: ignore[arg-type]


def test_g2_rejects_conflicting_exact_attribute() -> None:
    a = _record(attrs={"capacity_gb": 512.0})
    b = _record(id=2, attrs={"capacity_gb": 1024.0})
    assert attribute_gate_conflict(a.attrs, b.attrs, GATE_CFG) is not None


def test_g2_accepts_matching_exact_attribute() -> None:
    a = _record(attrs={"capacity_gb": 512.0})
    b = _record(id=2, attrs={"capacity_gb": 512.0})
    assert attribute_gate_conflict(a.attrs, b.attrs, GATE_CFG) is None


def test_g2_ignores_attribute_present_on_only_one_side() -> None:
    a = _record(attrs={"capacity_gb": 512.0})
    b = _record(id=2, attrs={})
    assert attribute_gate_conflict(a.attrs, b.attrs, GATE_CFG) is None


def test_g2_tolerance_key_within_band_passes() -> None:
    a = _record(attrs={"weight_kg": 1.00})
    b = _record(id=2, attrs={"weight_kg": 1.01})  # 1% diff, tolerance is 2%
    assert attribute_gate_conflict(a.attrs, b.attrs, GATE_CFG) is None


def test_g2_tolerance_key_beyond_band_fails() -> None:
    a = _record(attrs={"weight_kg": 1.00})
    b = _record(id=2, attrs={"weight_kg": 1.10})  # 10% diff, tolerance is 2%
    assert attribute_gate_conflict(a.attrs, b.attrs, GATE_CFG) is not None


def test_hard_gate_rejects_condition_mismatch() -> None:
    a = _record(condition="new")
    b = _record(id=2, condition="refurb")
    assert hard_gate_conflict(a, b, GATE_CFG) is not None


def test_hard_gate_allows_unknown_condition_on_either_side() -> None:
    a = _record(condition=None)
    b = _record(id=2, condition="refurb")
    assert hard_gate_conflict(a, b, GATE_CFG) is None


def test_hard_gate_rejects_bundle_mismatch() -> None:
    a = _record(is_bundle=False)
    b = _record(id=2, is_bundle=True, bundle_size=2)
    assert hard_gate_conflict(a, b, GATE_CFG) is not None


def test_hard_gate_rejects_bundle_size_mismatch() -> None:
    a = _record(is_bundle=True, bundle_size=2)
    b = _record(id=2, is_bundle=True, bundle_size=4)
    assert hard_gate_conflict(a, b, GATE_CFG) is not None


def test_hard_gate_allows_matching_bundle_size() -> None:
    a = _record(is_bundle=True, bundle_size=2)
    b = _record(id=2, is_bundle=True, bundle_size=2)
    assert hard_gate_conflict(a, b, GATE_CFG) is None


def test_attribute_agreement_score_neutral_when_no_shared_keys() -> None:
    assert attribute_agreement_score({}, {}, GATE_CFG) == 0.5


def test_attribute_agreement_score_full_when_exact_match() -> None:
    assert attribute_agreement_score({"capacity_gb": 512.0}, {"capacity_gb": 512.0}, GATE_CFG) == 1.0
