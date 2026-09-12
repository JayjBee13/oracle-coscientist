"""Every read the run API serves, as plain dictionaries in the frozen DTO shapes.

The API layer above this module does routing, validation and error envelopes; it does not
know SQL. This module does not know HTTP. That split is what keeps the DTO shapes of plan
C5 checkable in one place: each builder here returns exactly the documented field list, no
more — an extra key is as much a contract break as a missing one, because the frontend's
types are generated from what these endpoints actually emit.

Two shapes are deliberately narrower than what `RunStore` returns internally:
`HypothesisRow` drops `body_md` (a leaderboard of sixty hypotheses would otherwise ship a
third of a megabyte of prose nobody rendered) and `MatchRow` names its sides `a`/`b` with
titles attached, so a match table needs no second lookup to be readable.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, defer

from app.core.identity import Identity
from app.db.engine_models import Hypothesis, Match, Review, Run, RunEvent, User
from app.engine.run_metadata import elapsed_seconds, model_level

__all__ = [
    "RUN_SORTS",
    "export_markdown",
    "find_run",
    "hypothesis_detail",
    "hypothesis_rows",
    "list_runs",
    "match_rows",
    "project_detail",
    "run_summary",
    "visible_runs",
]

# Sort keys are self-describing rather than `field:direction` pairs: there are four
# orderings a run list actually needs, and naming them keeps the query string readable.
RUN_SORTS: tuple[str, ...] = ("recent", "oldest", "title", "calls")

TERMINAL_LIFECYCLES = frozenset({"completed", "stopped", "failed", "lost"})

MAX_PAGE_SIZE = 100


# ------------------------------------------------------------------------ run lookup


def visible_runs(session: Session, identity: Identity) -> Select[tuple[Run]]:
    """The sole run-visibility predicate used by every HTTP read and write."""
    query = select(Run)
    if identity.is_admin:
        return query
    return query.join(User, Run.owner_id == User.id).where(User.username == identity.username)


def find_run(session: Session, reference: str, identity: Identity) -> Run | None:
    """Resolve a run by its uuid or, for convenience at the command line, its engine id."""
    query = visible_runs(session, identity).where(Run.deleted_at.is_(None))
    try:
        return session.execute(query.where(Run.id == UUID(reference))).scalar_one_or_none()
    except ValueError:
        return session.execute(
            query.where(Run.engine_run_id == reference)
        ).scalar_one_or_none()


def list_runs(
    session: Session,
    *,
    identity: Identity,
    mine: bool = False,
    status: str | None = None,
    harness: str | None = None,
    q: str | None = None,
    show_archived: bool = False,
    include_demo: bool = False,
    sort: str = "recent",
    page: int = 1,
    page_size: int = 25,
) -> dict[str, Any]:
    """A page of `RunSummary` plus the unpaged total, for `GET /api/runs`."""
    query = visible_runs(session, identity).where(Run.deleted_at.is_(None))
    if mine and identity.is_admin:
        query = query.join(User, Run.owner_id == User.id).where(User.username == identity.username)
    if status:
        query = query.where(Run.lifecycle == status)
    if harness:
        query = query.where(Run.harness == harness)
    elif not include_demo:
        # Demo runs are real rows in their own harness lane, but they are practice, so they
        # stay behind a visible toggle instead of padding the scientist's list.
        query = query.where(Run.harness != "demo")
    if not show_archived:
        query = query.where(Run.archived.is_(False))
    if q and q.strip():
        needle = f"%{q.strip()}%"
        query = query.where(or_(Run.title.ilike(needle), Run.question.ilike(needle)))

    total = session.execute(
        select(func.count()).select_from(query.subquery())
    ).scalar_one()

    page = max(1, page)
    page_size = max(1, min(page_size, MAX_PAGE_SIZE))
    rows = list(
        session.execute(
            _ordered(query, sort)
            # `engine_state` holds the overview markdown — up to 21KB a row, and a list of
            # fifty runs never renders a word of it.
            .options(defer(Run.engine_state), defer(Run.prompt))
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).scalars()
    )
    return {"items": run_summaries(session, rows), "total": total}


def _ordered(query: Select[tuple[Run]], sort: str) -> Select[tuple[Run]]:
    if sort == "oldest":
        return query.order_by(Run.updated_at.asc(), Run.id)
    if sort == "title":
        return query.order_by(func.lower(Run.title).asc(), Run.id)
    if sort == "calls":
        return query.order_by(Run.calls_used.desc(), Run.id)
    return query.order_by(Run.updated_at.desc(), Run.id)


# --------------------------------------------------------------------- run summaries


def run_summary(session: Session, run: Run) -> dict[str, Any]:
    return run_summaries(session, [run])[0]


def run_summaries(session: Session, runs: Sequence[Run]) -> list[dict[str, Any]]:
    """Build `RunSummary` for many runs with a fixed number of queries."""
    if not runs:
        return []
    run_ids = [run.id for run in runs]
    counts = _counts_by_run(session, run_ids)
    matches = _completed_matches_by_run(session, run_ids)
    tops = _top_by_run(session, run_ids)
    flags = _engine_flags(session, run_ids)
    health = _health_by_run(session, run_ids)
    elapsed = _elapsed_by_run(session, run_ids, {run.id: run.lifecycle for run in runs})
    owner_ids = {run.owner_id for run in runs if run.owner_id is not None}
    owners = {
        user.id: (user.display_name or user.username)
        for user in session.execute(select(User).where(User.id.in_(owner_ids))).scalars()
    }
    return [
        _summary(run, counts.get(run.id, {}), matches.get(run.id, 0), tops.get(run.id, []),
                 flags.get(run.id, {}), health.get(run.id, {}), owners.get(run.owner_id),
                 elapsed.get(run.id))
        for run in runs
    ]


def _summary(
    run: Run,
    counts: Mapping[str, int],
    completed_matches: int,
    top: Sequence[Mapping[str, Any]],
    flags: Mapping[str, Any],
    health: Mapping[str, int] | None = None,
    owner_display_name: str | None = None,
    run_elapsed_seconds: int | None = None,
) -> dict[str, Any]:
    graft_state = dict(run.graft_state or {})
    level, custom = model_level(run.config, source=run.source)
    return {
        "id": str(run.id),
        "engine_run_id": run.engine_run_id,
        "source": run.source,
        "source_version": run.source_version,
        "title": run.title,
        "question": run.question,
        "owner_display_name": owner_display_name,
        "harness": run.harness,
        "lifecycle": run.lifecycle,
        "round": run.round,
        "rounds_target": run.rounds_target,
        "calls_used": run.calls_used,
        "budget_calls": run.budget_calls,
        "spend_usd": float(run.spend_usd or 0),
        "tokens_total": (run.tokens_in or 0) + (run.tokens_out or 0),
        "model_level": level,
        "model_level_custom": custom,
        "elapsed_seconds": run_elapsed_seconds,
        "counts": {
            "active": counts.get("active", 0),
            "rejected": counts.get("rejected", 0),
            "archived": counts.get("archived", 0),
            "matches": completed_matches,
        },
        "top": list(top),
        "graft": {
            "enabled": bool((dict(run.config or {}).get("graft") or {}).get("enabled", False)),
            "fired_count": int(graft_state.get("fired_count", 0) or 0),
            "pending": bool(graft_state.get("pending_seed")),
        },
        "archived": bool(run.archived),
        "has_overview": bool(flags.get("has_overview")),
        "failed_calls": int((health or {}).get("failed_calls", 0)),
        "lost_steps": int((health or {}).get("lost_steps", 0)),
        "ended_reason": flags.get("ended_reason"),
        "overview_skipped_reason": flags.get("overview_skipped_reason"),
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }


def _elapsed_by_run(
    session: Session, run_ids: Sequence[UUID], lifecycles: Mapping[UUID, str]
) -> dict[UUID, int]:
    rows = session.execute(
        select(RunEvent.run_id, RunEvent.type, RunEvent.payload, RunEvent.ts)
        .where(
            RunEvent.run_id.in_(run_ids),
            RunEvent.type.in_(("lifecycle_changed", "run_finished")),
        )
        .order_by(RunEvent.run_id, RunEvent.seq)
    )
    grouped: dict[UUID, list[dict[str, Any]]] = {}
    for run_id, type_, payload, ts in rows:
        grouped.setdefault(run_id, []).append({"type": type_, "payload": payload, "ts": ts})
    return {
        run_id: value
        for run_id, events in grouped.items()
        if (value := elapsed_seconds(events, lifecycle=lifecycles[run_id])) is not None
    }


def _counts_by_run(session: Session, run_ids: Sequence[UUID]) -> dict[UUID, dict[str, int]]:
    rows = session.execute(
        select(Hypothesis.run_id, Hypothesis.status, func.count())
        .where(Hypothesis.run_id.in_(run_ids))
        .group_by(Hypothesis.run_id, Hypothesis.status)
    )
    counts: dict[UUID, dict[str, int]] = {}
    for run_id, status, count in rows:
        counts.setdefault(run_id, {})[status] = count
    return counts


def _completed_matches_by_run(session: Session, run_ids: Sequence[UUID]) -> dict[UUID, int]:
    rows = session.execute(
        select(Match.run_id, func.count())
        .where(Match.run_id.in_(run_ids), Match.status == "completed")
        .group_by(Match.run_id)
    )
    return dict(rows.all())


def _top_by_run(
    session: Session, run_ids: Sequence[UUID]
) -> dict[UUID, list[dict[str, Any]]]:
    """The three strongest hypotheses per run, falling back past status when none are active.

    A run whose every hypothesis was rejected — one of the archived runs rejected all
    eighteen — still has a story, and an empty top three would read as a run that produced
    nothing rather than one whose ideas all lost.
    """
    rows = list(
        session.execute(
            select(
                Hypothesis.run_id,
                Hypothesis.hid,
                Hypothesis.title,
                Hypothesis.elo,
                Hypothesis.status,
            )
            .where(Hypothesis.run_id.in_(run_ids))
            .order_by(Hypothesis.run_id, Hypothesis.elo.desc(), Hypothesis.hid)
        )
    )
    active: dict[UUID, list[dict[str, Any]]] = {}
    any_status: dict[UUID, list[dict[str, Any]]] = {}
    for run_id, hid, title, elo, status in rows:
        entry = {"hid": hid, "title": title, "elo": float(elo), "status": status}
        bucket = any_status.setdefault(run_id, [])
        if len(bucket) < 3:
            bucket.append(entry)
        if status == "active":
            live = active.setdefault(run_id, [])
            if len(live) < 3:
                live.append(entry)
    return {run_id: active.get(run_id) or any_status.get(run_id, []) for run_id in run_ids}


def _engine_flags(session: Session, run_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
    """The three `engine_state` keys a run summary needs, without loading the column.

    `list_runs` defers `engine_state` because the overview markdown lives in it — up to
    21KB a row that a list of fifty runs never renders a word of. Reading a key off the
    loaded object here would undo that deferral once per run, so the keys are extracted in
    the database instead.
    """
    rows = session.execute(
        select(
            Run.id,
            Run.engine_state.has_key("overview_md"),
            Run.engine_state["ended_reason"].astext,
            Run.engine_state["overview_skipped_reason"].astext,
        ).where(Run.id.in_(run_ids))
    )
    return {
        run_id: {
            "has_overview": bool(has_overview),
            "ended_reason": ended_reason,
            "overview_skipped_reason": skipped,
        }
        for run_id, has_overview, ended_reason, skipped in rows
    }


def _health_by_run(session: Session, run_ids: Sequence[UUID]) -> dict[UUID, dict[str, int]]:
    """Failed calls and lost steps per run, from the event log.

    Two grouped counts rather than one per run: the run list renders fifty of these, and
    the whole point of putting a failure signal on `RunSummary` is that it costs little
    enough to be there.
    """
    from app.db.engine_models import RunEvent

    health: dict[UUID, dict[str, int]] = {
        run_id: {"failed_calls": 0, "lost_steps": 0} for run_id in run_ids
    }
    failed = session.execute(
        select(RunEvent.run_id, func.count())
        .where(
            RunEvent.run_id.in_(run_ids),
            RunEvent.type == "call_finished",
            RunEvent.payload["ok"].astext == "false",
        )
        .group_by(RunEvent.run_id)
    )
    for run_id, count in failed:
        health[run_id]["failed_calls"] = int(count)
    lost = session.execute(
        select(RunEvent.run_id, func.count())
        .where(
            RunEvent.run_id.in_(run_ids),
            RunEvent.type == "contract_violation",
            # Kept in step with `store._run_health`: a violation flagged `lost: false` cost
            # the run nothing (the evolution lineage warning still adds its hypothesis), and
            # counting it here made the list and the detail disagree with the report.
            RunEvent.payload["lost"].astext.is_distinct_from("false"),
        )
        .group_by(RunEvent.run_id)
    )
    for run_id, count in lost:
        health[run_id]["lost_steps"] = int(count)
    return health


# --------------------------------------------------------------------------- detail


def project_detail(detail: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    """Trim `RunStore.snapshot` to the `RunDetail` field list.

    The store's internal hypothesis dict carries `body_md` and `seed_id`; the DTO does not,
    and the leaderboard is the one place where shipping every body would actually hurt.
    """
    projected = dict(detail)
    projected["leaderboard"] = [
        _hypothesis_row(row, novelty_level=row.get("novelty_level"))
        for row in detail.get("leaderboard", [])
    ]
    projected["rounds"] = [
        _round_summary(round_, source=source) for round_ in detail.get("rounds", [])
    ]
    return projected


def export_markdown(session: Session, run: Run, *, limit: int | None = None) -> str:
    """The run as one markdown document — the Report tab's Download button.

    Active hypotheses carry their full body, because those are what a scientist reading the
    report actually wants. ``limit`` keeps only that many highest-ranked active hypotheses;
    rejected and archived ones remain named only, so the losing majority of a tournament
    does not bury the winners in the same document.
    """
    hypotheses = list(
        session.execute(
            select(Hypothesis)
            .where(Hypothesis.run_id == run.id)
            .order_by(Hypothesis.elo.desc(), Hypothesis.hid)
        ).scalars()
    )
    active = [row for row in hypotheses if row.status == "active"]
    if limit is not None:
        active = active[:limit]
    inactive = [row for row in hypotheses if row.status != "active"]

    lines = [f"# {run.title}", "", f"**Question:** {run.question}", ""]
    lines.append(f"- Harness: {run.harness}")
    lines.append(f"- Lifecycle: {run.lifecycle}")
    lines.append(f"- Rounds: {run.round}/{run.rounds_target}")
    calls = f"{run.calls_used}/{run.budget_calls}" if run.budget_calls else str(run.calls_used)
    lines.append(f"- Calls: {calls}")
    if run.spend_usd:
        lines.append(f"- Spend: ${float(run.spend_usd):.2f}")
    lines.append("")

    overview = (run.engine_state or {}).get("overview_md")
    if overview:
        lines += ["## Research overview", "", str(overview).strip(), ""]

    lines.append("## Ranked hypotheses")
    lines.append("")
    if not active:
        lines.append(
            "_No active hypotheses — every idea this run produced was rejected or archived._"
        )
        lines.append("")
    for row in active:
        lines.append(f"### {row.title} ({row.hid})")
        lines.append(f"Elo {row.elo:.0f} · {row.matches} match(es) · {row.wins} win(s)")
        lines.append("")
        lines.append(row.body_md.strip())
        lines.append("")

    if inactive:
        lines.append("## Rejected & archived")
        lines.append("")
        for row in inactive:
            lines.append(f"- {row.title} ({row.status})")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _round_summary(round_: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    summary = dict(round_)
    # Round status is normally read off `round_started`/`round_completed` events, which
    # imported runs never had. They did finish, so reporting their rounds as still running
    # would be the false statement here.
    if source == "imported" and summary.get("status") != "completed":
        summary["status"] = "completed"
    return summary


# ----------------------------------------------------------------------- hypotheses


def hypothesis_rows(
    session: Session,
    run_id: UUID,
    *,
    status: str | None = None,
    sort: str = "elo",
) -> list[dict[str, Any]]:
    query = select(Hypothesis).where(Hypothesis.run_id == run_id)
    if status:
        query = query.where(Hypothesis.status == status)
    if sort == "hid":
        query = query.order_by(Hypothesis.hid)
    elif sort == "recent":
        query = query.order_by(Hypothesis.created_round.desc(), Hypothesis.hid)
    else:
        query = query.order_by(Hypothesis.elo.desc(), Hypothesis.hid)

    rows = list(session.execute(query).scalars())
    novelty = _latest_novelty(session, [row.id for row in rows])
    return [_hypothesis_row(row, novelty_level=novelty.get(row.id)) for row in rows]


def hypothesis_detail(
    session: Session, hypothesis_id: UUID, identity: Identity
) -> dict[str, Any] | None:
    row = session.execute(
        select(Hypothesis)
        .join(Run, Hypothesis.run_id == Run.id)
        .where(
            Hypothesis.id == hypothesis_id,
            Run.deleted_at.is_(None),
            Run.id.in_(visible_runs(session, identity).with_only_columns(Run.id)),
        )
    ).scalar_one_or_none()
    if row is None:
        return None

    reviews = list(
        session.execute(
            select(Review)
            .where(Review.hypothesis_id == hypothesis_id)
            .order_by(Review.created_at)
        ).scalars()
    )
    novelty = next(
        (review.novelty_level for review in reversed(reviews) if review.novelty_level), None
    )

    siblings = {
        sibling.hid: sibling
        for sibling in session.execute(
            select(Hypothesis).where(Hypothesis.run_id == row.run_id)
        ).scalars()
    }
    parents = [siblings[hid] for hid in row.parent_ids if hid in siblings]
    children = [
        sibling
        for sibling in siblings.values()
        if row.hid in (sibling.parent_ids or [])
    ]

    return {
        **_hypothesis_row(row, novelty_level=novelty),
        "body_md": row.body_md,
        # Prose fields are unwrapped on the way out, the same as in `store.list_reviews`.
        # The schema now refuses a JSON-encoded critique, but rows written before it did
        # are still in the database — run c4566ed2's h006 holds the literal string `{}` in
        # four of its five fields — and this endpoint is what puts them in front of the
        # scientist, under a human label, with a `?.trim()` emptiness test that reads two
        # characters of JSON as present prose.
        "reviews": [
            {
                "verdict": review.verdict,
                "novelty_level": review.novelty_level,
                "novelty_note": _prose(review.novelty_note),
                "correctness": _prose(review.correctness),
                "testability": _prose(review.testability),
                "key_risk": _prose(review.key_risk),
                "note": _prose(review.note),
                "model": review.model,
            }
            for review in reviews
        ],
        "match_history": match_rows(session, row.run_id, hid=row.hid),
        "lineage": {
            "parents": [_hypothesis_row(parent) for parent in parents],
            "children": [
                _hypothesis_row(child)
                for child in sorted(children, key=lambda item: item.hid)
            ],
        },
    }


def _prose(value: str | None) -> str | None:
    """A stored critique field as prose. `None` stays `None`; a JSON literal is unwrapped."""
    if value is None:
        return None
    from app.engine.schemas import unwrap_prose

    return unwrap_prose(value)


def _hypothesis_row(
    row: Hypothesis | Mapping[str, Any], *, novelty_level: str | None = None
) -> dict[str, Any]:
    if isinstance(row, Mapping):
        source = row
    else:
        source = {
            "id": str(row.id),
            "hid": row.hid,
            "title": row.title,
            "status": row.status,
            "elo": float(row.elo),
            "matches": row.matches,
            "wins": row.wins,
            "cluster": row.cluster,
            "duplicate_of": row.duplicate_of,
            "parent_ids": list(row.parent_ids or []),
            "operator": row.operator,
            "created_round": row.created_round,
            "source": row.source,
        }
    return {
        "id": source["id"],
        "hid": source["hid"],
        "title": source["title"],
        "status": source["status"],
        "elo": source["elo"],
        "matches": source["matches"],
        "wins": source["wins"],
        "cluster": source.get("cluster"),
        "duplicate_of": source.get("duplicate_of"),
        "parent_ids": list(source.get("parent_ids") or []),
        "operator": source.get("operator"),
        "created_round": source.get("created_round", 0),
        "source": source.get("source", "agent"),
        "novelty_level": novelty_level,
    }


def _latest_novelty(
    session: Session, hypothesis_ids: Sequence[UUID]
) -> dict[UUID, str | None]:
    if not hypothesis_ids:
        return {}
    rows = session.execute(
        select(Review.hypothesis_id, Review.novelty_level)
        .where(Review.hypothesis_id.in_(hypothesis_ids))
        .order_by(Review.created_at)
    )
    latest: dict[UUID, str | None] = {}
    for hypothesis_id, level in rows:
        if level is not None or hypothesis_id not in latest:
            latest[hypothesis_id] = level
    return latest


# --------------------------------------------------------------------------- matches


def match_rows(
    session: Session,
    run_id: UUID,
    *,
    round: int | None = None,
    hid: str | None = None,
) -> list[dict[str, Any]]:
    query = select(Match).where(Match.run_id == run_id)
    if round is not None:
        query = query.where(Match.round == round)
    if hid is not None:
        query = query.where(or_(Match.hid_a == hid, Match.hid_b == hid))
    # Imported matches all share the run's completion timestamp, so hids break the tie and
    # the order is at least stable between requests.
    rows = list(
        session.execute(
            query.order_by(Match.round, Match.ts, Match.hid_a, Match.hid_b)
        ).scalars()
    )
    titles = _titles(session, run_id)
    return [_match_row(row, titles) for row in rows]


def _match_row(row: Match, titles: Mapping[str, str]) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "round": row.round,
        "a": {"hid": row.hid_a, "title": titles.get(row.hid_a, row.hid_a)},
        "b": {"hid": row.hid_b, "title": titles.get(row.hid_b, row.hid_b)},
        "status": row.status,
        "winner": row.winner,
        "elo_a_before": _float(row.elo_a_before),
        "elo_a_after": _float(row.elo_a_after),
        "elo_b_before": _float(row.elo_b_before),
        "elo_b_after": _float(row.elo_b_after),
        "judge_model": row.judge_model,
        "debate_md": row.debate_md,
        "ts": _iso(row.ts),
    }


def _titles(session: Session, run_id: UUID) -> dict[str, str]:
    rows = session.execute(
        select(Hypothesis.hid, Hypothesis.title).where(Hypothesis.run_id == run_id)
    )
    return dict(rows.all())


# --------------------------------------------------------------------------- scalars


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _float(value: Any) -> float | None:
    return None if value is None else float(value)
