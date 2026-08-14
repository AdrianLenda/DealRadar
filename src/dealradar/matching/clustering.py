"""Union-find clustering of cascade edges, plus the post-hoc consistency check the task spec calls
out by name: "scalenie przeszlo przechodnio A-B, B-C, ale A i C sie wykluczaja -> rozbij klaster i
zglos do match_review". Owner: A3.

Transitivity is the classic silent-failure mode in record-linkage union-find: two pairwise-safe
edges (A-B, B-C) can chain together a pair (A-C) that would have been rejected outright had it been
compared directly. This module always re-checks every pair *within* a formed cluster against the
hard gates -- not just the edges that built it -- and if a cluster fails, removes the weakest
edge on the path between the conflicting pair (lowest cascade-step rank, then lowest score) and
recomputes, repeating until every remaining cluster is internally consistent.
"""

from __future__ import annotations

import itertools
from collections import defaultdict, deque
from dataclasses import dataclass

from dealradar.matching.cascade import METHOD_RANK, Edge
from dealradar.matching.config import AttributeGateConfig
from dealradar.matching.gates import hard_gate_conflict
from dealradar.matching.records import OfferRecord


class _UnionFind:
    """Standard union-find over a fixed, known-in-advance set of integer ids."""

    def __init__(self, ids: list[int]) -> None:
        self._parent: dict[int, int] = {i: i for i in ids}

    def find(self, x: int) -> int:
        """Returns the representative (root) id of x's set, path-compressing along the way."""
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[x] != root:
            self._parent[x], x = root, self._parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        """Merges the sets containing a and b."""
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self._parent[root_b] = root_a

    def components(self, ids: list[int]) -> dict[int, list[int]]:
        """Returns {root_id: sorted member ids} for every current component over ids."""
        groups: dict[int, list[int]] = defaultdict(list)
        for i in ids:
            groups[self.find(i)].append(i)
        return {root: sorted(members) for root, members in groups.items()}


@dataclass(frozen=True)
class ClusterConflict:
    """A pair that would violate a hard gate despite having been transitively unioned; logged for review."""

    offer_a: int
    offer_b: int
    reason: str


@dataclass(frozen=True)
class ClusterResult:
    """Final, gate-consistent clusters plus the best edge touching each offer and any splits performed."""

    components: dict[int, list[int]]
    """root offer id -> sorted member offer ids."""
    best_edge_for: dict[int, Edge]
    """offer id -> the highest-rank, highest-confidence surviving edge touching it (drives match_method
    and match_confidence). Offers with no surviving edge (true singletons) are absent from this dict."""
    conflicts: list[ClusterConflict]


def _edge_key(edge: Edge) -> tuple[int, float, float]:
    """Returns a sort key ranking edges weakest-first: lowest cascade-step rank, then lowest confidence/score."""
    return (METHOD_RANK[edge.method], edge.confidence, edge.score)


def _weakest_edge_on_path(start: int, goal: int, edges: list[Edge], component: set[int]) -> Edge | None:
    """Returns the weakest edge (by _edge_key) on some path from start to goal within component; None if no path exists."""
    adjacency: dict[int, list[tuple[int, Edge]]] = defaultdict(list)
    for edge in edges:
        if edge.offer_a in component and edge.offer_b in component:
            adjacency[edge.offer_a].append((edge.offer_b, edge))
            adjacency[edge.offer_b].append((edge.offer_a, edge))

    previous: dict[int, tuple[int, Edge] | None] = {start: None}
    queue: deque[int] = deque([start])
    while queue:
        node = queue.popleft()
        if node == goal:
            break
        for neighbor, edge in adjacency[node]:
            if neighbor not in previous:
                previous[neighbor] = (node, edge)
                queue.append(neighbor)

    if goal not in previous:
        return None
    path_edges: list[Edge] = []
    cursor = goal
    while previous[cursor] is not None:
        parent, edge = previous[cursor]  # type: ignore[misc]
        path_edges.append(edge)
        cursor = parent
    return min(path_edges, key=_edge_key)


def build_clusters(
    offer_ids: list[int], edges: list[Edge], lookup: dict[int, OfferRecord], attr_cfg: AttributeGateConfig
) -> ClusterResult:
    """Unions offer_ids by edges, splits any cluster with an internal hard-gate conflict, and returns the result.

    Deterministic given deterministic input ordering: offer_ids and edges must already be sorted
    (callers pass records/edges built from an `ORDER BY id` query and cascade steps in a fixed
    order), so tie-breaks in `_weakest_edge_on_path` and `min()` resolve the same way every run.
    """
    active_edges = list(edges)
    conflicts: list[ClusterConflict] = []

    while True:
        uf = _UnionFind(offer_ids)
        for edge in active_edges:
            uf.union(edge.offer_a, edge.offer_b)
        components = uf.components(offer_ids)

        violation: tuple[int, int, str] | None = None
        for members in components.values():
            if len(members) < 2:
                continue
            for a_id, b_id in itertools.combinations(members, 2):
                reason = hard_gate_conflict(lookup[a_id], lookup[b_id], attr_cfg)
                if reason is not None:
                    violation = (a_id, b_id, reason)
                    break
            if violation is not None:
                break

        if violation is None:
            final_components = components
            break

        a_id, b_id, reason = violation
        conflicts.append(ClusterConflict(a_id, b_id, reason))
        component_set = set(components[uf.find(a_id)])
        weak_edge = _weakest_edge_on_path(a_id, b_id, active_edges, component_set)
        if weak_edge is None:
            # Should not happen (a_id and b_id are in the same component, so some path connects
            # them), but fail safe rather than loop forever if it ever does.
            break
        active_edges.remove(weak_edge)

    best_edge_for: dict[int, Edge] = {}
    for edge in active_edges:
        for endpoint in (edge.offer_a, edge.offer_b):
            current = best_edge_for.get(endpoint)
            if current is None or _edge_key(edge) > _edge_key(current):
                best_edge_for[endpoint] = edge

    return ClusterResult(components=final_components, best_edge_for=best_edge_for, conflicts=conflicts)
