"""Hard gates and gate G2 (attribute agreement) -- ARCHITEKTURA.md section 7 / failure mode F2. Owner: A3.

These checks are shared by every cascade step (task spec: "Bramki obowiazuja takze krok 1 i 2 -- ten
sam EAN dla nowego i poleasingowego egzemplarza to dwa rozne produkty z punktu widzenia ceny") and by
the post-clustering consistency check in clustering.py. There is exactly one place a pair is allowed
to be merged despite a gate: an explicit human "same" label in match_review (see review.py) -- an
algorithmic gate can be wrong about ambiguous title text, a human ground-truth label overrides it.
"""

from __future__ import annotations

from dealradar.matching.config import AttributeGateConfig
from dealradar.matching.records import OfferRecord


def attribute_gate_conflict(attrs_a: dict[str, float], attrs_b: dict[str, float], cfg: AttributeGateConfig) -> str | None:
    """Returns a reason string if a shared numeric attribute conflicts (gate G2), else None.

    Zero tolerance for `cfg.exact_keys` (capacity, resolution, counts -- "512GB vs 1TB" must never
    merge). +/- `cfg.tolerance_pct_keys[key]` relative tolerance for physical measurements that
    legitimately round differently between shops. A key present on only one side is not a conflict
    -- gate G2 only fires when *both* offers state a value and they disagree (Z4: absence is not
    a value).
    """
    for key in cfg.exact_keys:
        value_a = attrs_a.get(key)
        value_b = attrs_b.get(key)
        if value_a is not None and value_b is not None and value_a != value_b:
            return f"attribute {key!r} conflicts (exact): {value_a!r} vs {value_b!r}"

    for key, tolerance in cfg.tolerance_pct_keys.items():
        value_a = attrs_a.get(key)
        value_b = attrs_b.get(key)
        if value_a is None or value_b is None:
            continue
        base = max(abs(value_a), abs(value_b), 1e-9)
        relative_diff = abs(value_a - value_b) / base
        if relative_diff > tolerance:
            return f"attribute {key!r} conflicts (tolerance {tolerance:.0%}): {value_a!r} vs {value_b!r}"

    return None


def hard_gate_conflict(a: OfferRecord, b: OfferRecord, cfg: AttributeGateConfig) -> str | None:
    """Returns a reason string if a and b can never be merged under any cascade step's hard gates, else None.

    The three hard gates (task spec): different `condition` class, different bundle-ness (or
    different `bundle_size` when both are bundles), and gate G2's attribute conflict.
    """
    if a.condition is not None and b.condition is not None and a.condition != b.condition:
        return f"condition differs: {a.condition!r} vs {b.condition!r}"

    if a.is_bundle != b.is_bundle:
        return f"is_bundle differs: {a.is_bundle!r} vs {b.is_bundle!r}"

    if a.is_bundle and b.is_bundle and a.bundle_size is not None and b.bundle_size is not None:
        if a.bundle_size != b.bundle_size:
            return f"bundle_size differs: {a.bundle_size!r} vs {b.bundle_size!r}"

    return attribute_gate_conflict(a.attrs, b.attrs, cfg)


def attribute_agreement_score(attrs_a: dict[str, float], attrs_b: dict[str, float], cfg: AttributeGateConfig) -> float:
    """Returns the fraction (0..1) of shared numeric attributes that agree; 0.5 (neutral) if none are shared.

    Used as the third similarity signal in cascade step 3. By the time this runs, `attribute_gate_conflict`
    has already rejected the pair if any shared attribute disagreed outright, so this mostly measures
    *how close* tolerance-band attributes are, not whether exact-key attributes match (they always do,
    or the pair would already be gone).
    """
    scores: list[float] = []

    for key in cfg.exact_keys:
        value_a = attrs_a.get(key)
        value_b = attrs_b.get(key)
        if value_a is not None and value_b is not None:
            scores.append(1.0 if value_a == value_b else 0.0)

    for key, tolerance in cfg.tolerance_pct_keys.items():
        value_a = attrs_a.get(key)
        value_b = attrs_b.get(key)
        if value_a is None or value_b is None:
            continue
        base = max(abs(value_a), abs(value_b), 1e-9)
        relative_diff = abs(value_a - value_b) / base
        if tolerance <= 0:
            scores.append(1.0 if relative_diff == 0 else 0.0)
        else:
            scores.append(max(0.0, 1.0 - relative_diff / tolerance))

    if not scores:
        return 0.5
    return sum(scores) / len(scores)
