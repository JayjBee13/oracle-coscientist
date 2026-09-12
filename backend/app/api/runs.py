"""Run reads, served from the database.

Until this file was rewritten every read walked six artifact roots, re-parsed every
`state.json` on every request and merged two identity spaces (`imported-v2-run-…` for a run
on disk, a uuid once the database heard about it) — so a run's id changed underneath the
frontend the first time anything wrote a row for it. Now the database is the only source:
the importer puts the archived runs in it once, the supervisor writes the live ones, and
every endpoint here is a query.

Writing is two endpoints, and both hand straight off to a service: `POST /runs` to the
launcher, which creates the row and the process that executes it, and `POST /runs/{id}/
controls` to the controls table. Neither does any engine work in the request — a run takes
tens of minutes, and the whole point of the supervisor is that nothing here waits for it.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from app.api.errors import bad_request, conflict, not_found
from app.core.config import Settings, get_settings
from app.core.identity import CurrentUser
from app.db.engine_models import Run
from app.db.session import get_db
from app.engine.store import RunNotFound, RunStore
from app.schemas.dto import (
    ContextDocRef,
    HypothesisRow,
    MatchRow,
    RunDetail,
    RunGraph,
    RunList,
    RunSummary,
)
from app.schemas.launch import (
    AcceptedResponse,
    CreateRunRequest,
    NoteRequest,
    PatchRunRequest,
    RunControlRequest,
    RunControlResponse,
)
from app.services.runs import graph, reads
from app.services.runs.controls import IllegalTransition, apply_control
from app.services.runs.launcher import LaneBusy, LaunchRefused, launch_run

router = APIRouter(prefix="/api/runs", tags=["runs"])

SettingsDep = Annotated[Settings, Depends(get_settings)]
DbSession = Annotated[Session, Depends(get_db)]

# A note is only ever read at a round boundary. Queuing one on a run that will never reach
# another — stopping, finishing, or already done — would accept a write that is silently
# never applied, which is worse than refusing it.
NOTE_LIFECYCLES: frozenset[str] = frozenset({"queued", "running", "pausing", "paused"})

_SLUG_RE = re.compile(r"[^a-z0-9]+")


@router.get("", response_model=RunList)
def list_runs(
    db: DbSession,
    current: CurrentUser,
    status: str | None = None,
    harness: str | None = None,
    q: str | None = None,
    show_archived: bool = False,
    include_demo: bool = False,
    sort: Literal["recent", "oldest", "title", "calls"] = "recent",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
    mine: bool = False,
) -> RunList:
    """A page of runs, newest first.

    Demo runs are hidden unless asked for, by `include_demo` or by naming the harness:
    they are practice runs and would otherwise crowd out the scientist's real work.
    """
    return RunList.model_validate(
        reads.list_runs(
            db,
            identity=current.identity,
            mine=mine,
            status=status,
            harness=harness,
            q=q,
            show_archived=show_archived,
            include_demo=include_demo,
            sort=sort,
            page=page,
            page_size=page_size,
        )
    )


@router.post("", response_model=RunDetail, status_code=201)
def create_run(
    request: CreateRunRequest, settings: SettingsDep, db: DbSession, current: CurrentUser
) -> RunDetail:
    """Create a run and start its supervisor. Returns as soon as the process is alive.

    Always a **new** run, `from_run` included: that field copies an earlier run's question,
    prompt, config and context documents, and the run it creates starts from no hypotheses,
    no ratings and no guidance. Extending a run that already exists is
    `POST /runs/{id}/controls {action: "continue"}`, which keeps the whole idea pool.

    The 409 is the harness lane: one active run per harness, enforced by a partial unique
    index rather than a check the API could race against. Its details name the run holding
    the lane only when that run is visible to this user; otherwise they report capacity
    without exposing another workspace.
    """
    store = RunStore(settings=settings)
    try:
        if request.from_run:
            _run(db, request.from_run, current)
        launched = launch_run(
            store,
            question=request.question,
            prompt=request.prompt,
            title=request.title,
            harness=request.harness,
            from_run=request.from_run,
            context_docs=request.docs(),
            config=request.config_document(),
            settings=settings,
            owner_id=current.user.id,
            reveal_conflict=current.identity.is_admin,
        )
    except LaneBusy as exc:
        raise conflict(
            "lane_busy",
            str(exc),
            harness=exc.harness,
            **(
                {"conflicting_run_id": exc.conflicting_run_id}
                if exc.conflicting_run_id is not None
                else {}
            ),
        ) from exc
    except LaunchRefused as exc:
        raise bad_request("invalid_run_request", str(exc)) from exc

    snapshot = store.snapshot(launched.run_id)
    return RunDetail.model_validate(reads.project_detail(snapshot, source="app"))


@router.post("/{run_id}/controls", response_model=RunControlResponse)
def control_run(
    run_id: str,
    request: RunControlRequest,
    db: DbSession,
    settings: SettingsDep,
    current: CurrentUser,
) -> RunControlResponse:
    """Pause, resume, continue, stop, finish or force-stop a run (plan C4's action table).

    Cooperative actions are acknowledged, not completed: the lifecycle in the response is
    the one the run is in *now*, and it moves when the supervisor acts on the flag at its
    next step boundary.

    Two of them start a process instead of setting a flag, and both answer with the run's
    new lifecycle rather than its old one. `resume` picks a paused run back up; `continue`
    gives a run that already ended `add_rounds` more rounds, in place and keeping everything
    it learned — optionally raising its call ceiling, and optionally raising or removing the
    cost ceiling that may have been what stopped it. Both take the harness lane again, so
    both can 409 exactly as a launch does.
    """
    run = _run(db, run_id, current)
    try:
        outcome = apply_control(
            RunStore(settings=settings),
            run.id,
            request.action,
            add_rounds=request.add_rounds,
            budget_calls=request.budget_calls,
            budget_usd=request.cost_ceiling,
            settings=settings,
            reveal_conflict=current.identity.is_admin,
        )
    except IllegalTransition as exc:
        raise conflict(
            "illegal_transition",
            str(exc),
            action=exc.action,
            lifecycle=exc.lifecycle,
            allowed=list(exc.allowed),
        ) from exc
    except LaneBusy as exc:
        raise conflict(
            "lane_busy",
            str(exc),
            harness=exc.harness,
            **(
                {"conflicting_run_id": exc.conflicting_run_id}
                if exc.conflicting_run_id is not None
                else {}
            ),
        ) from exc
    except LaunchRefused as exc:
        # A continue this run cannot act on: an imported run, a round target past the
        # ceiling, a budget with no room left. Nothing was started.
        raise bad_request("invalid_run_request", str(exc)) from exc
    except RunNotFound as exc:
        raise not_found("run_not_found", "No run with that id.", run_id=run_id) from exc

    return RunControlResponse.model_validate(outcome)


@router.patch("/{run_id}", response_model=RunSummary)
def patch_run(
    run_id: str,
    request: PatchRunRequest,
    db: DbSession,
    settings: SettingsDep,
    current: CurrentUser,
) -> RunSummary:
    """Rename or archive a run. Neither field touches its lifecycle, budget or content."""
    run = _run(db, run_id, current)
    RunStore(settings=settings).update_run_fields(
        run.id, title=request.title, archived=request.archived
    )
    # The store wrote through its own session; this one's copy of the row is now stale.
    db.expire(run)
    return RunSummary.model_validate(reads.run_summary(db, run))


@router.delete("/{run_id}", status_code=204)
def delete_run(
    run_id: str, db: DbSession, settings: SettingsDep, current: CurrentUser
) -> Response:
    """Soft delete: the run disappears from every list and 404s directly; the row stays."""
    run = _run(db, run_id, current)
    RunStore(settings=settings).soft_delete(run.id)
    return Response(status_code=204)


@router.post("/{run_id}/note", response_model=AcceptedResponse, status_code=202)
def add_run_note(
    run_id: str,
    request: NoteRequest,
    db: DbSession,
    settings: SettingsDep,
    current: CurrentUser,
) -> AcceptedResponse:
    """Queue a scientist's note; the orchestrator drains it at the next round boundary."""
    run = _run(db, run_id, current)
    if run.lifecycle not in NOTE_LIFECYCLES:
        raise conflict(
            "run_not_active",
            "This run cannot take a note right now; nothing would ever read it.",
            lifecycle=run.lifecycle,
        )
    RunStore(settings=settings).add_note(run.id, request.text)
    return AcceptedResponse()


@router.get("/{run_id}", response_model=RunSummary)
def get_run(run_id: str, db: DbSession, current: CurrentUser) -> RunSummary:
    return RunSummary.model_validate(reads.run_summary(db, _run(db, run_id, current)))


@router.get("/{run_id}/detail", response_model=RunDetail)
def get_run_detail(
    run_id: str, db: DbSession, settings: SettingsDep, current: CurrentUser
) -> RunDetail:
    """Everything a run workspace needs in one round trip."""
    run = _run(db, run_id, current)
    snapshot = RunStore(settings=settings).snapshot(run.id)
    return RunDetail.model_validate(reads.project_detail(snapshot, source=run.source))


@router.get("/{run_id}/hypotheses", response_model=list[HypothesisRow])
def list_hypotheses(
    run_id: str,
    db: DbSession,
    current: CurrentUser,
    status: str | None = None,
    sort: Literal["elo", "hid", "recent"] = "elo",
) -> list[HypothesisRow]:
    run = _run(db, run_id, current)
    rows = reads.hypothesis_rows(db, run.id, status=status, sort=sort)
    return [HypothesisRow.model_validate(row) for row in rows]


@router.post(
    "/{run_id}/hypotheses/{hid}/archive", response_model=AcceptedResponse, status_code=202
)
def archive_hypothesis(
    run_id: str, hid: str, db: DbSession, settings: SettingsDep, current: CurrentUser
) -> AcceptedResponse:
    """Archive one hypothesis on the scientist's instruction — a live run or a finished one."""
    run = _run(db, run_id, current)
    try:
        RunStore(settings=settings).archive_hypothesis(run.id, hid)
    except RunNotFound as exc:
        raise not_found(
            "hypothesis_not_found",
            "No hypothesis with that id in this run.",
            run_id=run_id,
            hid=hid,
        ) from exc
    return AcceptedResponse()


@router.get("/{run_id}/graph", response_model=RunGraph)
def get_run_graph(run_id: str, db: DbSession, current: CurrentUser) -> RunGraph:
    """The run's idea genealogy — nodes, descent edges and the legend's facts, in one trip.

    Read-only and assembled from rows the run already has: what the Ideas tab draws is the
    attrition story, and fetching it as four calls would be four chances to render half a
    run.
    """
    run = _run(db, run_id, current)
    return RunGraph.model_validate(graph.build_graph(db, run))


@router.get("/{run_id}/matches", response_model=list[MatchRow])
def list_matches(
    run_id: str, db: DbSession, current: CurrentUser, round: int | None = None
) -> list[MatchRow]:
    run = _run(db, run_id, current)
    return [MatchRow.model_validate(row) for row in reads.match_rows(db, run.id, round=round)]


@router.get("/{run_id}/overview", response_class=PlainTextResponse)
def get_overview(run_id: str, db: DbSession, current: CurrentUser) -> PlainTextResponse:
    """The run's research overview, as markdown.

    Served from the database rather than the file it came from: for the twelve archived
    runs this is the whole product — the document their operator actually read.
    """
    run = _run(db, run_id, current)
    overview = (run.engine_state or {}).get("overview_md")
    if not overview:
        raise not_found(
            "overview_missing",
            "This run has not written a research overview.",
            run_id=str(run.id),
        )
    return PlainTextResponse(overview, media_type="text/markdown; charset=utf-8")


@router.get("/{run_id}/export.md")
def export_run(
    run_id: str,
    db: DbSession,
    current: CurrentUser,
    top: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    """The run as a downloadable markdown report — the Report tab's Download button."""
    run = _run(db, run_id, current)
    markdown = reads.export_markdown(db, run, limit=top)
    suffix = f"-top{top}" if top is not None else ""
    filename = f"{_slug(run.title)}-{run.engine_run_id}{suffix}.md"
    return Response(
        content=markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{run_id}/context", response_model=list[ContextDocRef])
def get_run_context(
    run_id: str, db: DbSession, settings: SettingsDep, current: CurrentUser
) -> list[ContextDocRef]:
    run = _run(db, run_id, current)
    docs = RunStore(settings=settings).get_context_docs(run.id)
    # The engine records what actually fitted the per-call cap once it starts composing
    # prompts; until then there is nothing to claim and every document reads `pending`.
    delivery = dict((run.engine_state or {}).get("context_delivery") or {})
    cap = (run.engine_state or {}).get("context_char_cap")
    return [
        ContextDocRef(
            name=doc["name"],
            chars=doc["chars"],
            delivered=delivery.get(doc["name"], "pending"),
            cap_chars=cap,
        )
        for doc in docs
    ]


def _slug(text: str, *, max_len: int = 60) -> str:
    """A filesystem/URL-safe stub of a title, for the export filename."""
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "run"


def _run(db: Session, reference: str, current: CurrentUser) -> Run:
    run = reads.find_run(db, reference, current.identity)
    if run is None:
        raise not_found("run_not_found", "No run with that id.", run_id=reference)
    return run
