"""Project a run from the database onto disk as the files the v1 engine wrote.

The database is authoritative. These files are a *projection* of it: a compatibility and
portability layer so the legacy artifact reader (`services/artifacts/normalizer.py`), the
artifact-streaming endpoint, and anyone who copies a run folder onto a USB stick all keep
working against a run this engine produced.

Four files land in the run's workdir after every round and again at finish:

* `state.json` — the v1 shape, plus `schema_version: 2`. POSIX separators (the archived
  runs stored Windows ones and every reader has had to repair them since), UTF-8, and
  written temp-then-rename so a crash mid-write leaves the previous projection intact
  rather than a truncated file that reads as a corrupt run.
* `hypotheses/hNNN.md` — one file per hypothesis, its `body_md` verbatim.
* `research_overview.md` — written once the overview exists.
* `ideas_ranked.csv` — active hypotheses in Elo order, BOM-free UTF-8.

**This projection is deliberately lossy and must never be read back as engine state.** The
legacy shape has one `parent` string where a run has a list of `parent_ids` (a two-parent
combination projects to `"h004,h007"`), one `note` where a match has a full debate, and no
place at all for Elo before/after, `k`, judge model, per-call spend or the event log. Those
live in Postgres and are served from there. Nothing in this module reads a projected file.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from app.engine.core import INITIAL_ELO, K_INITIAL
from app.engine.store import RunNotFound, RunStore
from app.services.runs.paths import is_within, relative_posix

__all__ = [
    "IDEAS_CSV_COLUMNS",
    "IDEAS_CSV_FILENAME",
    "OVERVIEW_FILENAME",
    "SCHEMA_VERSION",
    "STATE_FILENAME",
    "ProjectionResult",
    "build_state",
    "make_projector",
    "project_run",
    "render_ideas_csv",
]

log = logging.getLogger(__name__)

SCHEMA_VERSION = 3
"""Bumped for the `health` block: what the run lost, which v2 had no field for."""

STATE_FILENAME = "state.json"
OVERVIEW_FILENAME = "research_overview.md"
IDEAS_CSV_FILENAME = "ideas_ranked.csv"
HYPOTHESES_DIRNAME = "hypotheses"

IDEAS_CSV_COLUMNS = ("hid", "title", "elo", "matches", "wins", "cluster", "status")

# A legacy match carries a one-line note; a real match carries a full judged debate. The
# debate is served from the database — this is a readable stub, not a summary.
DEBATE_NOTE_MAX = 280

# The legacy reader speaks in "a"/"b"; the matches table and the ranking schema speak in
# 1/2. The projection is the only place the two vocabularies meet.
_WINNER_SIDE = {1: "a", 2: "b"}


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    """What one projection wrote. Returned for logging and for tests to assert against."""

    workdir: Path
    state_path: Path
    files: tuple[str, ...]
    hypotheses: int
    matches: int


def project_run(
    store: RunStore,
    run_id: UUID,
    workdir: Path | str,
    *,
    engine_root: Path | str | None = None,
    reason: str = "manual",
) -> ProjectionResult:
    """Write the run's projection into `workdir`, replacing any previous one.

    `engine_root` is the directory the `file` paths in `state.json` are relative to — the
    legacy reader resolves them against it, not against the run folder. It defaults to the
    grandparent of a `.../runs/<engine_run_id>` workdir, which is the layout the launcher
    creates, and must contain the workdir either way.

    Projecting the same unchanged run twice produces byte-identical files.
    """
    # Both sides are resolved before anything is measured against them: a caller that
    # mixes a resolved root with an unresolved workdir (Windows hands out both forms)
    # would otherwise get absolute `file` paths the reader cannot confine.
    workdir = Path(workdir).resolve()
    root = (
        Path(engine_root).resolve() if engine_root is not None else _default_engine_root(workdir)
    )
    if not is_within(workdir, root):
        raise ValueError(f"workdir {workdir} is not inside engine root {root}")

    run = store.get_run(run_id)
    if run is None:
        raise RunNotFound(f"No run {run_id}")

    hypotheses = store.list_hypotheses(run_id)
    matches = store.list_matches(run_id, status="completed")
    reviews = store.list_reviews(run_id)

    hypotheses_dir = workdir / HYPOTHESES_DIRNAME
    state = build_state(
        run,
        hypotheses,
        matches,
        reviews,
        file_prefix=relative_posix(hypotheses_dir, root),
    )

    written: list[str] = []
    for hypothesis in hypotheses:
        path = hypotheses_dir / f"{hypothesis['hid']}.md"
        _write_text(path, _hypothesis_markdown(hypothesis))
        written.append(relative_posix(path, workdir))

    overview = str((run.get("engine_state") or {}).get("overview_md") or "")
    if overview.strip():
        path = workdir / OVERVIEW_FILENAME
        _write_text(path, overview if overview.endswith("\n") else overview + "\n")
        written.append(OVERVIEW_FILENAME)

    csv_path = workdir / IDEAS_CSV_FILENAME
    _write_text(csv_path, render_ideas_csv(hypotheses))
    written.append(IDEAS_CSV_FILENAME)

    # state.json goes last: it is the file every reader treats as the run's index, so it
    # should never point at hypothesis files that are not on disk yet.
    state_path = workdir / STATE_FILENAME
    _write_text(state_path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    written.append(STATE_FILENAME)

    log.debug(
        "projected run %s after %s: %d hypotheses, %d matches", run_id, reason, len(hypotheses),
        len(matches),
    )
    return ProjectionResult(
        workdir=workdir,
        state_path=state_path,
        files=tuple(written),
        hypotheses=len(hypotheses),
        matches=len(matches),
    )


def make_projector(
    store: RunStore,
    run_id: UUID,
    workdir: Path | str,
    *,
    engine_root: Path | str | None = None,
) -> Callable[[str], None]:
    """Bind a run to a projector for `Orchestrator(projector=...)`.

    The orchestrator calls this after every round and at finish with the reason as its only
    argument, and swallows anything raised — a projection failure is never allowed to fail
    a run that is otherwise fine.
    """

    def project(reason: str) -> None:
        project_run(store, run_id, workdir, engine_root=engine_root, reason=reason)

    return project


# --- state shaping ------------------------------------------------------------------------


def build_state(
    run: Mapping[str, Any],
    hypotheses: Sequence[Mapping[str, Any]],
    matches: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    *,
    file_prefix: str,
) -> dict[str, Any]:
    """Shape the v1 `state.json` document. Pure: no I/O, no clock, no randomness.

    `file_prefix` is the POSIX directory the `hNNN.md` paths are written against, relative
    to the engine root the reader will resolve them from.
    """
    config = dict(run.get("config") or {})
    latest_review = _latest_review_by_hid(reviews)
    ordered = sorted(hypotheses, key=lambda row: str(row.get("hid") or ""))

    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.get("engine_run_id") or str(run.get("id") or ""),
        "goal": run.get("question") or "",
        "config": _legacy_config(run, config),
        "engine_config": config,
        "calls_used": int(run.get("calls_used") or 0),
        "iteration": int(run.get("round") or 0),
        "next_id": _next_id(ordered),
        "hypotheses": {
            str(row["hid"]): _legacy_hypothesis(row, latest_review, file_prefix)
            for row in ordered
        },
        "matches": [_legacy_match(row) for row in matches],
        "feedback": [
            {"iter": entry.get("round"), "text": entry.get("guidance") or ""}
            for entry in run.get("feedback_history") or []
        ],
    }

    if bool((config.get("graft") or {}).get("enabled")):
        graft_state = dict(run.get("graft_state") or {})
        seed = dict(graft_state.get("pending_seed") or {})
        state["collapse"] = {
            "fired_count": int(graft_state.get("fired_count", 0)),
            "last_fired_round": graft_state.get("last_fired_round"),
        }
        state["pending_injection"] = str(seed.get("seed_framing") or "")

    # Under its own key so the v1 reader ignores it. The projection is the only thing that
    # outlives the database, and it had no field for a failure at all: a run folder copied
    # onto a USB stick presented as clean and complete however much of the run had died.
    engine_state = dict(run.get("engine_state") or {})
    losses = [dict(item) for item in engine_state.get("losses") or []]
    state["health"] = {
        "rounds_completed": int(engine_state.get("last_completed_round") or 0),
        "rounds_planned": int(config.get("rounds") or run.get("rounds_target") or 0),
        "lost_steps": losses,
        "ended_reason": engine_state.get("ended_reason"),
        "overview_written": bool(str(engine_state.get("overview_md") or "").strip()),
        "overview_skipped_reason": engine_state.get("overview_skipped_reason"),
    }

    state["created"] = run.get("created_at")
    state["updated"] = run.get("updated_at")
    return state


def render_ideas_csv(hypotheses: Sequence[Mapping[str, Any]]) -> str:
    """The ranked spreadsheet the Report tab links: active hypotheses, best Elo first.

    Only active hypotheses appear. Rejected and archived ones never finished the tournament,
    so ranking them beside the survivors would put a number on a result that does not exist;
    they stay available in full through the run's Hypotheses tab.
    """
    ranked = sorted(
        (row for row in hypotheses if row.get("status") == "active"),
        key=lambda row: (-float(row.get("elo") or 0), str(row.get("hid") or "")),
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(IDEAS_CSV_COLUMNS)
    for row in ranked:
        writer.writerow(
            [
                row.get("hid") or "",
                row.get("title") or "",
                _round_elo(row.get("elo")),
                int(row.get("matches") or 0),
                int(row.get("wins") or 0),
                row.get("cluster") or "",
                row.get("status") or "",
            ]
        )
    return buffer.getvalue()


def _legacy_config(run: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    legacy = {
        "initial_elo": INITIAL_ELO,
        "k_factor": K_INITIAL,
        "max_llm_calls": int(run.get("budget_calls") or config.get("budget_calls") or 0),
        "matches_per_round": int(config.get("matches_per_round") or 0),
        "evolve_top_k": int(config.get("evolve_top_k") or 0),
    }
    graft = dict(config.get("graft") or {})
    if graft:
        legacy["graft"] = graft
    return legacy


def _legacy_hypothesis(
    row: Mapping[str, Any],
    latest_review: Mapping[str, Mapping[str, Any]],
    file_prefix: str,
) -> dict[str, Any]:
    hid = str(row["hid"])
    review = latest_review.get(hid)
    parent_ids = [str(parent) for parent in row.get("parent_ids") or []]
    return {
        "id": hid,
        "title": row.get("title") or hid,
        "file": f"{file_prefix}/{hid}.md",
        "elo": _round_elo(row.get("elo")),
        "matches": int(row.get("matches") or 0),
        "wins": int(row.get("wins") or 0),
        "status": row.get("status") or "active",
        # The legacy reader looks for `review.verdict` and nothing else, so `review` keeps
        # its v1 shape exactly.
        "review": (
            None
            if review is None
            else {"verdict": review.get("verdict"), "note": review.get("note") or ""}
        ),
        # …and the rest of the critique goes beside it under a key the v1 reader ignores,
        # the same way `state["health"]` was added. This file is what outlives the
        # database: a run folder copied off the machine, or streamed through the artifact
        # endpoint, used to lose four of every five review fields — about 1.9k characters of
        # paid critique per hypothesis, across every reviewed hypothesis in the run.
        "review_full": (
            None
            if review is None
            else {
                "verdict": review.get("verdict"),
                "novelty_level": review.get("novelty_level"),
                "novelty_note": review.get("novelty_note") or "",
                "correctness": review.get("correctness") or "",
                "testability": review.get("testability") or "",
                "key_risk": review.get("key_risk") or "",
                "note": review.get("note") or "",
            }
        ),
        "cluster": row.get("cluster"),
        "created_iter": row.get("created_round"),
        # Lossy on purpose: two parents collapse into one comma-joined string here, and
        # `parent_ids` in the database stays the truth.
        "parent": ",".join(parent_ids) or None,
        "duplicate_of": row.get("duplicate_of"),
        "operator": row.get("operator"),
        "source": row.get("source") or "agent",
    }


def _legacy_match(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "a": row.get("hid_a"),
        "b": row.get("hid_b"),
        "winner": _WINNER_SIDE.get(row.get("winner")),
        "iter": row.get("round"),
        "note": _first_line(row.get("debate_md")),
    }


def _latest_review_by_hid(
    reviews: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Reviews arrive oldest first, so the last one written for a hid is the one shown."""
    latest: dict[str, Mapping[str, Any]] = {}
    for review in reviews:
        hid = review.get("hid")
        if hid:
            latest[str(hid)] = review
    return latest


def _hypothesis_markdown(row: Mapping[str, Any]) -> str:
    body = str(row.get("body_md") or "").strip()
    if not body:
        body = f"# {row.get('title') or row.get('hid')}"
    return body + "\n"


def _next_id(hypotheses: Sequence[Mapping[str, Any]]) -> int:
    highest = 0
    for row in hypotheses:
        hid = str(row.get("hid") or "")
        if hid.startswith("h") and hid[1:].isdigit():
            highest = max(highest, int(hid[1:]))
    return highest + 1


def _round_elo(value: Any) -> float:
    """One decimal place: Elo is a comparison scale, and full float noise reads as data."""
    return round(float(value or 0), 1)


def _first_line(text: str | None, limit: int = DEBATE_NOTE_MAX) -> str:
    """The debate's opening sentence, skipping its heading — legacy `note` was one line.

    A judged debate usually opens with a `# Debate` heading; projecting that as the note
    would put the word "Debate" where the reason for the verdict belongs.
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    prose = [line for line in lines if line and not line.startswith("#")]
    for line in prose or [line.lstrip("#").strip() for line in lines if line]:
        if len(line) <= limit:
            return line
        return line[: limit - 1].rstrip() + "…"
    return ""


# --- writing ------------------------------------------------------------------------------


def _default_engine_root(workdir: Path) -> Path:
    """`.../engines/runs/<engine_run_id>` → `.../engines`, the root `file` paths resolve from."""
    parent = workdir.parent
    return parent.parent if parent.name == "runs" else parent


def _write_text(path: Path, text: str) -> None:
    """UTF-8, no BOM, temp file then rename.

    The archived engines rewrote `state.json` in place on every mutation, which is why a
    killed run could leave half a document behind. `os.replace` is atomic within a
    directory on both POSIX and Windows, so a reader sees the old file or the new one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "wb") as handle:
            handle.write(text.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
