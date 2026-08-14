"""Cascade step 3's text-similarity signals: TF-IDF char n-grams + RapidFuzz + attribute agreement. Owner: A3.

Deterministic and reproducible by construction (task hard rule 5 / ARCHITEKTURA.md section 10): no
neural embeddings, no language model, nothing that depends on external state. `TfidfVectorizer` is
fit fresh on each blocking group ("Jeden wektoryzator na blok, nie na cala baze" -- task spec) so the
vocabulary is always a pure function of the offers being compared right now.
"""

from __future__ import annotations

import numpy as np
from rapidfuzz import fuzz
from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-untyped]
from sklearn.metrics.pairwise import cosine_similarity  # type: ignore[import-untyped]

from dealradar.matching.config import AttributeGateConfig, FuzzyStepConfig
from dealradar.matching.gates import attribute_agreement_score
from dealradar.matching.records import OfferRecord


def tfidf_cosine_matrix(titles: list[str]) -> np.ndarray:
    """Returns an NxN cosine-similarity matrix over titles, from one char 3-5-gram TF-IDF vectorizer fit on titles.

    `titles` should be every title_norm in a single blocking group (never the whole offer table --
    that is exactly the N^2 blowup blocking exists to prevent). Falls back to an all-zero matrix
    when there are fewer than 2 titles or the vectorizer would see an empty vocabulary (e.g. every
    title_norm in the block is an empty string), since TfidfVectorizer raises on an empty vocabulary.
    """
    n = len(titles)
    if n < 2 or not any(t.strip() for t in titles):
        return np.zeros((n, n))
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5))
    try:
        tfidf_matrix = vectorizer.fit_transform(titles)
    except ValueError:
        # All titles reduced to nothing the vectorizer could tokenize (e.g. only whitespace/punctuation).
        return np.zeros((n, n))
    result: np.ndarray = cosine_similarity(tfidf_matrix)
    return result


def pairwise_tfidf_cosine(title_a: str | None, title_b: str | None) -> float:
    """Returns the TF-IDF char n-gram cosine similarity between exactly two titles (vectorizer fit on just the two).

    Used outside the real blocking-group cascade (step 3 fits one vectorizer per whole block, not
    per pair -- see cascade.py) for one-off pairwise scoring: the eval dataset builder and the
    evaluation threshold sweep, where there is no natural "block" to fit a shared vectorizer on.
    """
    matrix = tfidf_cosine_matrix([title_a or "", title_b or ""])
    return float(matrix[0][1])


def title_similarity(title_a: str | None, title_b: str | None) -> float:
    """Returns RapidFuzz token_set_ratio between two titles, rescaled to 0..1; 0.0 if either is missing."""
    if not title_a or not title_b:
        return 0.0
    return float(fuzz.token_set_ratio(title_a, title_b)) / 100.0


def combined_score(
    tfidf_cosine: float,
    a: OfferRecord,
    b: OfferRecord,
    cfg: FuzzyStepConfig,
    attr_cfg: AttributeGateConfig,
) -> float:
    """Returns the weighted combination of TF-IDF cosine, token_set_ratio, and attribute agreement (0..1).

    This is the "podobienstwo rozmyte" measure from ARCHITEKTURA.md section 7 -- the three signals
    the task spec requires, combined with the weights configured in config/matching.yaml. Callers
    are expected to have already rejected the pair via `gates.hard_gate_conflict` (gate G2) before
    scoring it; this function does not itself enforce the gate.
    """
    token_ratio = title_similarity(a.title_norm, b.title_norm)
    attr_score = attribute_agreement_score(a.attrs, b.attrs, attr_cfg)
    return cfg.weight_tfidf * tfidf_cosine + cfg.weight_token_set_ratio * token_ratio + cfg.weight_attr_agreement * attr_score
