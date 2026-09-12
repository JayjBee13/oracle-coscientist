"""Comparison analytics between two runs, computed from the database.

Ported from the artifact-walking `api/comparisons.py`, with the statistically invalid part
removed. That endpoint merged both runs' hypotheses into one Elo-ranked leaderboard — but
Elo is only meaningful inside the tournament that produced it, so a 1300 from a run of six
hypotheses and a 1300 from a run of fifty are not the same number and ranking them against
each other invents a result. Comparison here is directional deltas plus title-matched
movement; the two ranked lists the compare page shows come from each run's own
`GET /runs/{id}/hypotheses`, normalized within its own run.

The saved comparison groups are gone with it. A comparison is two run ids, which is what a
URL already is; the tables stay until Revision B drops them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.engine_models import GraftEvent, Match, Run
from app.services.runs import reads

__all__ = ["SameRunComparison", "compare_runs"]


class SameRunComparison(ValueError):
    """A run compared against itself. Every delta would be zero and mean nothing."""


def compare_runs(session: Session, baseline: Run, challenger: Run) -> dict[str, Any]:
    """Build `CompareAnalytics` for two runs."""
    if baseline.id == challenger.id:
        raise SameRunComparison(str(baseline.id))

    base_rows = reads.hypothesis_rows(session, baseline.id)
    challenger_rows = reads.hypothesis_rows(session, challenger.id)

    return {
        "baseline": reads.run_summary(session, baseline),
        "challenger": reads.run_summary(session, challenger),
        # Imported runs have no recorded prompt hash, so this is False for all of them and
        # the page warns rather than implying the two runs answered the same question.
        "shared_prompt": bool(
            baseline.base_prompt_hash
            and challenger.base_prompt_hash
            and baseline.base_prompt_hash == challenger.base_prompt_hash
        ),
        "deltas": _deltas(session, baseline, challenger, base_rows, challenger_rows),
        "movement": _movement(base_rows, challenger_rows),
        "graft_summary": {
            "baseline": _graft(session, baseline),
            "challenger": _graft(session, challenger),
        },
    }


def _deltas(
    session: Session,
    baseline: Run,
    challenger: Run,
    base_rows: Sequence[Mapping[str, Any]],
    challenger_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """The metrics that survive being compared across two independent tournaments.

    Absolute Elo is not among them. `Elo spread` is: the distance from a run's best
    hypothesis to its median one is a property of that run's own tournament, so comparing
    two spreads says something real about how sharply each run separated its ideas.
    """
    base_matches = _completed_matches(session, baseline.id)
    challenger_matches = _completed_matches(session, challenger.id)
    return [
        _delta("Active hypotheses", _count(base_rows, "active"),
               _count(challenger_rows, "active"), "up"),
        _delta("Rejected hypotheses", _count(base_rows, "rejected"),
               _count(challenger_rows, "rejected"), "flat"),
        _delta("Matches judged", base_matches, challenger_matches, "flat"),
        _delta("Rounds", baseline.round, challenger.round, "flat"),
        _delta("Model calls", baseline.calls_used, challenger.calls_used, "down"),
        _delta("Distinct clusters", _clusters(base_rows), _clusters(challenger_rows), "flat"),
        _delta("Elo spread", _elo_spread(base_rows), _elo_spread(challenger_rows), "up"),
    ]


def _delta(metric: str, base: float, challenger: float, direction_hint: str) -> dict[str, Any]:
    return {
        "metric": metric,
        "base": float(base),
        "challenger": float(challenger),
        "direction_hint": direction_hint,
    }


def _movement(
    base_rows: Sequence[Mapping[str, Any]], challenger_rows: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Where the challenger's leading ideas sat in the baseline, matched by title.

    Titles are the only identity two independent runs share — `hid`s are per-run counters,
    so h003 in one run has nothing to do with h003 in another.
    """
    previous = {
        _key(row["title"]): index + 1
        for index, row in enumerate(base_rows)
        if row["title"].strip()
    }
    movement = []
    for index, row in enumerate(challenger_rows[:10]):
        rank = index + 1
        previous_rank = previous.get(_key(row["title"]))
        movement.append(
            {
                "hid": row["hid"],
                "title": row["title"],
                "rank": rank,
                "previous_rank": previous_rank,
                "delta": None if previous_rank is None else previous_rank - rank,
            }
        )
    return movement


def _graft(session: Session, run: Run) -> dict[str, Any] | None:
    """Null for runs where the concept does not apply, so the UI can hide it entirely.

    Rendering zeros for a v1 run would claim its diversity injection never fired, when in
    fact that engine had no such thing.
    """
    if run.source_version != "v2":
        return None
    collapse_events = session.execute(
        select(func.count()).select_from(GraftEvent).where(GraftEvent.run_id == run.id)
    ).scalar_one()
    graft_state = dict(run.graft_state or {})
    return {
        "enabled": bool((dict(run.config or {}).get("graft") or {}).get("enabled", False)),
        "fired_count": int(graft_state.get("fired_count", 0) or 0),
        "collapse_events": int(collapse_events),
    }


def _completed_matches(session: Session, run_id: UUID) -> int:
    return session.execute(
        select(func.count())
        .select_from(Match)
        .where(Match.run_id == run_id, Match.status == "completed")
    ).scalar_one()


def _count(rows: Sequence[Mapping[str, Any]], status: str) -> int:
    return sum(1 for row in rows if row["status"] == status)


def _clusters(rows: Sequence[Mapping[str, Any]]) -> int:
    return len({row["cluster"] for row in rows if row.get("cluster")})


def _elo_spread(rows: Sequence[Mapping[str, Any]]) -> float:
    active = sorted(
        (row["elo"] for row in rows if row["status"] == "active"), reverse=True
    )
    if len(active) < 2:
        return 0.0
    return round(active[0] - active[len(active) // 2], 1)


def _key(title: str) -> str:
    return " ".join(title.split()).lower()
