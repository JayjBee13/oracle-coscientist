"""One run's idea genealogy: which ideas were born, from what, and which survived.

The Ideas tab draws a graph, and a graph assembled from four endpoints would be four
chances to render half a run. This module answers it in one query: the nodes, the descent
edges between them, and the handful of facts a legend needs in order to tell the truth
about the run it is describing.

Two decisions here are load-bearing and easy to undo by accident.

**Ordering.** Rows come back ordered by `hid`, and the response preserves that order. Both
frontend layouts are deterministic functions of their input order — the organic one seeds
its PRNG per node index, the layered one spreads a band by position — so an unstable order
would move nodes between page loads for no reason the reader could see.

**Honesty about what history recorded.** Twelve imported runs predate `parent_ids` and have
no lineage at all; four never ran clustering; one rejected every idea it produced. Rather
than inventing a spine for them, `meta` states plainly whether there is any lineage and
whether any cluster label exists, so the view can say "lineage was not recorded" instead of
rendering a convincing but empty canvas.

That honesty needs *two* flags rather than one, and the difference between them is the
difference between two true sentences. `has_lineage` says whether any idea in this run
descended from another. `lineage_recorded` says whether the engine that produced the run
was capable of saying so. A modern run whose first round has not been evolved yet has
`lineage_recorded=True, has_lineage=False` — "nothing has descended from anything" — while
a pre-`parent_ids` import has both false, which is "we do not know". Collapsing them into
one flag made the view tell every un-evolved app run that its lineage was lost.

Nodes carry only what the canvas draws. `body_md` is deliberately absent: sixty hypotheses
would ship a third of a megabyte of prose behind a picture that renders none of it, and the
detail rail already has `GET /api/hypotheses/{id}` for the one idea the reader clicked.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, defer

from app.db.engine_models import Hypothesis, Run

__all__ = ["build_graph"]


def build_graph(session: Session, run: Run) -> dict[str, Any]:
    """Nodes and edges for one run, ordered so the layout is reproducible."""
    rows = list(
        session.execute(
            select(Hypothesis)
            .where(Hypothesis.run_id == run.id)
            .options(defer(Hypothesis.body_md))
            .order_by(Hypothesis.hid)
        ).scalars()
    )
    known = {row.hid for row in rows}
    leader = _leader(rows)

    nodes = [_node(row, is_leader=row.hid == leader) for row in rows]
    edges = [
        {"parent": parent, "child": row.hid, "operator": row.operator}
        for row in rows
        for parent in (row.parent_ids or [])
        # A dangling hid — a parent archived out of a different run, or a free-text
        # `parent` field that named something the import never saw — would otherwise
        # become an edge to a node no layout can place.
        if parent in known
    ]
    return {
        "run_id": run.id,
        "nodes": nodes,
        "edges": edges,
        "meta": _meta(rows, edges, recorded=_lineage_recorded(run, rows)),
    }


def _lineage_recorded(run: Run, rows: list[Hypothesis]) -> bool:
    """Whether the engine behind this run was able to record descent at all.

    Every run this app launches writes `parent_ids`, whether or not a round ever evolved
    anything. Only the archived imports predate the column, and the one signal they leave
    is that not a single row names a parent — so for them the question is answered by the
    data rather than by the version, which also keeps an import that *did* record descent
    from being described as if it had not.
    """
    if run.source != "imported":
        return True
    return any(row.parent_ids for row in rows)


def _leader(rows: list[Hypothesis]) -> str | None:
    """The hid currently leading the run, or `None` when nothing is still standing.

    Only `active` ideas can lead: a rejected or merged one is out of the tournament, and
    crowning it to avoid an empty highlight would misreport the run — one archived run
    rejected all eighteen of its ideas, and that is a research outcome, not a gap.

    Ties go to the lowest hid, which is the earliest idea. Before a run's first match every
    hypothesis sits on the same starting rating, so without a stable tie-break the highlight
    would wander between page loads.
    """
    active = [row for row in rows if row.status == "active"]
    if not active:
        return None
    return max(active, key=lambda row: (row.elo, _descending(row.hid))).hid


def _descending(hid: str) -> tuple[int, ...]:
    """Sort key that makes `max` prefer the lexicographically smallest hid."""
    return tuple(-ord(char) for char in hid)


def _node(row: Hypothesis, *, is_leader: bool) -> dict[str, Any]:
    return {
        "hid": row.hid,
        "id": row.id,
        "title": row.title,
        "status": row.status,
        "elo": float(row.elo),
        "matches": row.matches,
        "wins": row.wins,
        "created_round": row.created_round,
        "operator": row.operator,
        "cluster": row.cluster,
        "duplicate_of": row.duplicate_of,
        "source": row.source,
        "is_leader": is_leader,
    }


def _meta(
    rows: list[Hypothesis], edges: list[dict[str, Any]], *, recorded: bool
) -> dict[str, Any]:
    # `elo_min`/`elo_max` scale node size *within this run only*. Elo is not comparable
    # across runs — a 1240 in a four-idea run is not the 1240 of a sixty-idea one — which is
    # why the range travels with the graph rather than being a constant the frontend knows.
    elos = [float(row.elo) for row in rows] or [0.0]
    # Round 0 is real: the oldest imported runs recorded no iteration at all. The layered
    # view still needs a band to put them in, so the floor is one round.
    rounds = max(1, max((row.created_round for row in rows), default=1))
    return {
        "rounds": rounds,
        "has_lineage": bool(edges),
        "lineage_recorded": recorded,
        "has_clusters": any(row.cluster for row in rows),
        "elo_min": min(elos),
        "elo_max": max(elos),
        "node_count": len(rows),
    }
