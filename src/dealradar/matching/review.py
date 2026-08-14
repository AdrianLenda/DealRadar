"""In-memory view of `match_review` used to steer the cascade -- human ground truth wins. Owner: A3.

Two effects on matching, both intentional:
- A pair labeled `different` or `conflict` is a permanent "do not merge", regardless of what any
  cascade step's score says on a later run. This is the feedback loop ARCHITEKTURA.md section 10
  describes ("kolejka match_review do recznego oznaczenia ... ~15 min tygodniowo").
- A pair labeled `same` is force-merged with method="manual", confidence=1.0, bypassing the hard
  gates -- a human resolving an ambiguous pair is definitionally more trustworthy than the algorithm
  that flagged it as ambiguous in the first place.
"""

from __future__ import annotations

from dataclasses import dataclass, field


def _pair_key(offer_a: int, offer_b: int) -> tuple[int, int]:
    """Returns (offer_a, offer_b) sorted so the same unordered pair always hashes the same way."""
    return (offer_a, offer_b) if offer_a <= offer_b else (offer_b, offer_a)


@dataclass(frozen=True)
class ReviewOverrides:
    """Ground-truth pairs pulled from match_review: forced merges and forbidden merges."""

    forced_same: frozenset[tuple[int, int]] = field(default_factory=frozenset)
    forbidden: frozenset[tuple[int, int]] = field(default_factory=frozenset)

    def forbids(self, offer_a: int, offer_b: int) -> bool:
        """Returns True iff (offer_a, offer_b) is labeled 'different' or 'conflict' in match_review."""
        return _pair_key(offer_a, offer_b) in self.forbidden

    def forces_same(self, offer_a: int, offer_b: int) -> bool:
        """Returns True iff (offer_a, offer_b) is labeled 'same' in match_review."""
        return _pair_key(offer_a, offer_b) in self.forced_same

    @staticmethod
    def from_rows(rows: list[tuple[int, int, str | None]]) -> "ReviewOverrides":
        """Builds a ReviewOverrides from (offer_a, offer_b, label) rows; rows with label None/unlabeled are ignored."""
        forced: set[tuple[int, int]] = set()
        forbidden: set[tuple[int, int]] = set()
        for offer_a, offer_b, label in rows:
            key = _pair_key(offer_a, offer_b)
            if label == "same":
                forced.add(key)
            elif label in ("different", "conflict"):
                forbidden.add(key)
        return ReviewOverrides(forced_same=frozenset(forced), forbidden=frozenset(forbidden))
