"""Raw-SQL read/write helpers for the matcher, mirroring dealradar.db's "no ORM, thin SQL" style. Owner: A3.

Every write here is idempotent on rerun: `match_review` conflict rows are de-duplicated by
(offer_a, offer_b, label) before insert, `product_attr` rows are fully replaced (delete + reinsert)
rather than accumulated, and `offer.product_id`/`match_confidence`/`match_method` are plain UPDATEs
keyed by offer id. Nothing here uses `dealradar.db`'s helpers directly (they are shaped around the
`Offer` pydantic model's full field set, which the matcher does not construct) but the connection
comes from the same `Session` every other stage uses.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from dealradar.matching.cascade import EanConflict
from dealradar.matching.clustering import ClusterConflict
from dealradar.matching.records import OfferRecord
from dealradar.matching.review import ReviewOverrides


def load_offer_records(session: Session) -> list[OfferRecord]:
    """Returns every `offer` row as an OfferRecord (with attrs extracted from title_norm), ordered by id.

    Ordering by id is load-bearing: every deterministic tie-break in the cascade/clustering modules
    assumes records arrive in a fixed, reproducible order.
    """
    rows = session.execute(
        text(
            """
            SELECT id, source_id, title, title_norm, brand, brand_norm, mpn, mpn_norm, ean,
                   category_id, condition, is_bundle, bundle_size, price_total,
                   product_id, match_confidence, match_method, evidence
            FROM offer
            ORDER BY id
            """
        )
    ).mappings().all()
    return [OfferRecord.from_row(dict(row)) for row in rows]


def load_review_overrides(session: Session) -> ReviewOverrides:
    """Returns the current match_review-derived ReviewOverrides (forced 'same' and forbidden 'different'/'conflict' pairs)."""
    rows = session.execute(text("SELECT offer_a, offer_b, label FROM match_review WHERE label IS NOT NULL")).all()
    return ReviewOverrides.from_rows([(int(r[0]), int(r[1]), r[2]) for r in rows])


def load_all_match_review_rows(session: Session) -> list[tuple[int, int, str | None]]:
    """Returns every match_review row as (offer_a, offer_b, label), including unlabeled ones, for evaluate()."""
    rows = session.execute(text("SELECT offer_a, offer_b, label FROM match_review")).all()
    return [(int(r[0]), int(r[1]), r[2]) for r in rows]


def _match_review_pair_exists(session: Session, offer_a: int, offer_b: int, label: str) -> bool:
    """Returns True if a match_review row for this unordered pair and label already exists."""
    result = session.execute(
        text(
            """
            SELECT 1 FROM match_review
            WHERE label = :label
              AND ((offer_a = :a AND offer_b = :b) OR (offer_a = :b AND offer_b = :a))
            LIMIT 1
            """
        ),
        {"a": offer_a, "b": offer_b, "label": label},
    ).first()
    return result is not None


def log_ean_conflicts(session: Session, conflicts: list[EanConflict], observed_at: datetime) -> int:
    """Inserts one match_review 'conflict' row per not-yet-logged EAN conflict; returns the number inserted."""
    inserted = 0
    for conflict in conflicts:
        if _match_review_pair_exists(session, conflict.offer_a, conflict.offer_b, "conflict"):
            continue
        session.execute(
            text(
                """
                INSERT INTO match_review (offer_a, offer_b, combined_score, method, label, labeled_at)
                VALUES (:a, :b, NULL, 'ean', 'conflict', :labeled_at)
                """
            ),
            {"a": conflict.offer_a, "b": conflict.offer_b, "labeled_at": observed_at.isoformat()},
        )
        inserted += 1
    return inserted


def log_cluster_conflicts(session: Session, conflicts: list[ClusterConflict], observed_at: datetime) -> int:
    """Inserts one match_review 'conflict' row per not-yet-logged transitivity split; returns the number inserted."""
    inserted = 0
    for conflict in conflicts:
        if _match_review_pair_exists(session, conflict.offer_a, conflict.offer_b, "conflict"):
            continue
        session.execute(
            text(
                """
                INSERT INTO match_review (offer_a, offer_b, combined_score, method, label, labeled_at)
                VALUES (:a, :b, NULL, 'manual', 'conflict', :labeled_at)
                """
            ),
            {"a": conflict.offer_a, "b": conflict.offer_b, "labeled_at": observed_at.isoformat()},
        )
        inserted += 1
    return inserted


def match_review_pair_exists(session: Session, offer_a: int, offer_b: int) -> bool:
    """Returns True if any match_review row already exists for this unordered pair, regardless of label."""
    result = session.execute(
        text(
            """
            SELECT 1 FROM match_review
            WHERE (offer_a = :a AND offer_b = :b) OR (offer_a = :b AND offer_b = :a)
            LIMIT 1
            """
        ),
        {"a": offer_a, "b": offer_b},
    ).first()
    return result is not None


def insert_eval_pair(
    session: Session, offer_a: int, offer_b: int, combined_score: float, method: str, label: str, labeled_at: datetime | None
) -> None:
    """Inserts one starter-evaluation-set row into match_review. Caller is responsible for de-duplication."""
    session.execute(
        text(
            """
            INSERT INTO match_review (offer_a, offer_b, combined_score, method, label, labeled_at)
            VALUES (:a, :b, :combined_score, :method, :label, :labeled_at)
            """
        ),
        {
            "a": offer_a,
            "b": offer_b,
            "combined_score": combined_score,
            "method": method,
            "label": label,
            "labeled_at": labeled_at.isoformat() if labeled_at is not None else None,
        },
    )


def create_product(
    session: Session,
    canonical_title: str,
    brand: str | None,
    mpn: str | None,
    ean: str | None,
    category_id: int | None,
    created_at: datetime,
) -> int:
    """Inserts a new product row; returns its new id."""
    result = session.execute(
        text(
            """
            INSERT INTO product (canonical_title, brand, mpn, ean, category_id, created_at)
            VALUES (:canonical_title, :brand, :mpn, :ean, :category_id, :created_at)
            RETURNING id
            """
        ),
        {
            "canonical_title": canonical_title,
            "brand": brand,
            "mpn": mpn,
            "ean": ean,
            "category_id": category_id,
            "created_at": created_at.isoformat(),
        },
    )
    return int(result.scalar_one())


def update_product(
    session: Session,
    product_id: int,
    canonical_title: str,
    brand: str | None,
    mpn: str | None,
    ean: str | None,
    category_id: int | None,
) -> None:
    """Updates an existing product row's descriptive fields in place; product.id and created_at never change."""
    session.execute(
        text(
            """
            UPDATE product
            SET canonical_title = :canonical_title, brand = :brand, mpn = :mpn, ean = :ean, category_id = :category_id
            WHERE id = :id
            """
        ),
        {
            "id": product_id,
            "canonical_title": canonical_title,
            "brand": brand,
            "mpn": mpn,
            "ean": ean,
            "category_id": category_id,
        },
    )


def replace_product_attrs(session: Session, product_id: int, attrs: dict[str, float]) -> None:
    """Replaces every product_attr row for product_id with the given key -> value_num attrs (extracted_from='title')."""
    session.execute(text("DELETE FROM product_attr WHERE product_id = :product_id"), {"product_id": product_id})
    for key, value in attrs.items():
        session.execute(
            text(
                """
                INSERT INTO product_attr (product_id, "key", value_num, value_text, unit, extracted_from)
                VALUES (:product_id, :key, :value_num, NULL, NULL, 'title')
                """
            ),
            {"product_id": product_id, "key": key, "value_num": value},
        )


def update_offer_match(session: Session, offer_id: int, product_id: int, confidence: float | None, method: str) -> None:
    """Sets offer.product_id/match_confidence/match_method for one offer."""
    session.execute(
        text(
            """
            UPDATE offer SET product_id = :product_id, match_confidence = :confidence, match_method = :method
            WHERE id = :id
            """
        ),
        {"id": offer_id, "product_id": product_id, "confidence": confidence, "method": method},
    )
