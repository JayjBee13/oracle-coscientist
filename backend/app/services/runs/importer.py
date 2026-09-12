"""Bring the archived historical runs into the database.

Twelve real research runs and three GUI-created ones exist only as files under
`archive/imported-runs/{v1,v2,gui}/`. They are the product's day-one content: without them
a fresh install opens on an empty list, and the 6.3 cleanup that deletes the original
engine folders is gated on every one of them being readable through the API.

Three rules shape everything here:

**The archive is read-only.** Nothing in this module opens a file for writing. Mojibake
repair (`â€"` → `—`, the cp1252-through-UTF-8 damage the old engines wrote) is applied to
the text on its way into a database column and never to the bytes on disk. The archived
files stay exactly as `shutil.copy2` left them, which is what makes the manifest re-verify.

**Import is idempotent.** Runs are matched by `engine_run_id`; hypotheses by
`(run_id, hid)`, so their primary keys — which the frontend puts in URLs — survive a
re-import. Matches, reviews and graft events have no natural key, so they are rebuilt for
the run in the same transaction that updates it.

**One terminal lifecycle.** Every imported run is `completed`, whatever it looked like when
its operator walked away. History has no supervisor to ask, and a second vocabulary of
half-states for runs that can never move again would be noise the UI has to explain.

What history simply does not contain, and is therefore stored NULL rather than guessed:
per-match Elo before/after and K (the old engines recorded only the winner), per-review
novelty/correctness/testability/key-risk (only a verdict and a note were kept), per-call
cost and tokens (there is no ledger, so an imported run shows "N calls recorded
(historical)" and no budget ring), and `duplicate_of` (proximity archived duplicates
destructively, leaving no pointer back to the original).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import APP_ROOT, Settings, get_settings
from app.db.engine_models import GraftEvent, Hypothesis, Match, Review, Run, User
from app.db.session import get_session_factory
from app.engine.core import HYPOTHESIS_FIELD_ORDER, compose_hypothesis_md
from app.engine.store import derive_title
from app.services.artifacts.normalizer import repair_mojibake
from app.services.runs.paths import archive_root_ref, is_within, to_posix

__all__ = [
    "ImportReport",
    "ImportedRun",
    "import_all",
    "import_run_dir",
    "normalize_legacy_hypothesis",
    "read_run_dir",
]

# (source_version, subdirectory). `gui/` holds runs the GUI launched against the v1 engine,
# so it carries v1 provenance — which is what hides the Cartographer UI on them.
IMPORT_SUBTREES: tuple[tuple[str, str], ...] = (("v1", "v1"), ("v2", "v2"), ("v1", "gui"))

# Where the app runs its own supervisors. Importing from there would turn a live run's
# workdir into a second, frozen copy of itself under a source it does not own.
EXECUTION_WORKDIR_ROOT = APP_ROOT / "engines" / "runs"

HYPOTHESIS_STATUSES = frozenset({"active", "rejected", "archived"})

# A legacy body is a run of `KEY: value` lines. Keys are upper-case and short; the value
# runs to the next key, so a wrapped paragraph stays with the field it belongs to.
_LEGACY_KEY = re.compile(r"^([A-Z][A-Z0-9 _+/&()'-]{1,44}):[ \t]*(.*)$")

# Which archived section names carry which schema field. Order is priority: a run that
# wrote both CORE AI and RATIONALE means the first by mechanism, the second by evidence.
_LEGACY_FIELD_SOURCES: dict[str, tuple[str, ...]] = {
    "title": ("TITLE",),
    "claim": ("CLAIM", "HYPOTHESIS"),
    "mechanism": ("MECHANISM", "CORE AI", "HOW IT WORKS", "RATIONALE", "WHY NOW"),
    "novelty": ("NOVELTY", "NOVELTY CLAIM"),
    "test": ("TEST", "VALIDATION", "EXPERIMENT"),
    "assumptions": ("ASSUMPTIONS", "RISKS", "KEY RISKS"),
}

_EMPTY_SECTION = re.compile(r"^\*\*[A-Za-z][A-Za-z ]*:\*\*\s*$")


class ImportRefused(ValueError):
    """An import root points somewhere the importer must never read runs from."""


@dataclass
class ImportReport:
    """What one pass over the archive did. Returned by `POST /api/admin/reimport`."""

    imported: int = 0
    created: int = 0
    updated: int = 0
    hypotheses: int = 0
    reviews: int = 0
    matches: int = 0
    graft_events: int = 0
    overviews: int = 0
    legacy_bodies: int = 0
    repaired_strings: int = 0
    warnings: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "created": self.created,
            "updated": self.updated,
            "hypotheses": self.hypotheses,
            "reviews": self.reviews,
            "matches": self.matches,
            "graft_events": self.graft_events,
            "overviews": self.overviews,
            "legacy_bodies": self.legacy_bodies,
            "repaired_strings": self.repaired_strings,
            "warnings": list(self.warnings),
            "run_ids": list(self.run_ids),
        }


@dataclass(frozen=True)
class ImportedRun:
    """One archived run directory, parsed and ready to upsert."""

    engine_run_id: str
    source_version: str
    run_dir: Path
    state: dict[str, Any]
    overview_md: str | None


# --------------------------------------------------------------------------- reading


def read_run_dir(run_dir: Path, source_version: str) -> ImportedRun | None:
    """Parse one archived run directory, or None when it holds no `state.json`."""
    state_path = run_dir / "state.json"
    if not state_path.is_file():
        return None
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(state, dict):
        return None

    overview_path = run_dir / "research_overview.md"
    overview = overview_path.read_text(encoding="utf-8") if overview_path.is_file() else None
    return ImportedRun(
        engine_run_id=str(state.get("run_id") or run_dir.name),
        source_version=source_version,
        run_dir=run_dir,
        state=state,
        overview_md=overview,
    )


def discover_archived_runs(archive_root: Path) -> Iterator[ImportedRun]:
    """Walk the three archived subtrees in a fixed order: v1, then v2, then gui."""
    _refuse_execution_root(archive_root)
    for source_version, subdir in IMPORT_SUBTREES:
        subtree = archive_root / subdir
        if not subtree.is_dir():
            continue
        for run_dir in sorted(p for p in subtree.iterdir() if p.is_dir()):
            parsed = read_run_dir(run_dir, source_version)
            if parsed is not None:
                yield parsed


def _refuse_execution_root(path: Path) -> None:
    if is_within(path, EXECUTION_WORKDIR_ROOT) or is_within(EXECUTION_WORKDIR_ROOT, path):
        raise ImportRefused(
            f"{to_posix(path)} overlaps the supervisor workdir root "
            f"{to_posix(EXECUTION_WORKDIR_ROOT)}; imports read the archive only"
        )


# --------------------------------------------------------------- the legacy body era


def normalize_legacy_hypothesis(body: str) -> dict[str, Any] | None:
    """Map a `TITLE:`/`CLAIM:`/`RATIONALE:` plaintext body onto the schema fields.

    Three runs (74 hypotheses) come from the era before the engines composed markdown:
    their bodies are flat `KEY: value` lines whose key vocabulary changed from run to run.
    Returns the six `HYPOTHESIS_FIELDS` plus an `_extra` mapping of every section that has
    no schema home — those are re-composed after the standard ones so nothing is dropped.

    Returns None for a body that is not from that era, which is how the caller decides
    whether to re-compose at all.
    """
    sections = _parse_sections(body)
    if not sections or "TITLE" not in sections:
        return None

    fields: dict[str, str] = {}
    claimed: set[str] = set()
    for name in HYPOTHESIS_FIELD_ORDER:
        for key in _LEGACY_FIELD_SOURCES.get(name, ()):
            if key in sections and key not in claimed:
                fields[name] = sections[key]
                claimed.add(key)
                break
        else:
            fields[name] = ""

    extra = {key: value for key, value in sections.items() if key not in claimed}
    fields["title"] = _clean_title(fields["title"])
    return {**fields, "_extra": extra}


def _parse_sections(body: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.splitlines():
        match = _LEGACY_KEY.match(line)
        if match:
            current = match.group(1).strip()
            sections.setdefault(current, []).append(match.group(2))
        elif current is not None:
            sections[current].append(line)
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def compose_legacy_body(fields: Mapping[str, Any]) -> str:
    """Re-compose a legacy hypothesis, keeping its unmapped sections after the schema ones."""
    extra: Mapping[str, str] = fields.get("_extra") or {}
    core = {name: fields.get(name, "") for name in HYPOTHESIS_FIELD_ORDER}
    body = compose_hypothesis_md(core)
    # A field the run never wrote composes to a bare `**Test:**` with nothing after it.
    # Dropping the line says "not recorded" more honestly than inventing filler would.
    kept = [line for line in body.splitlines() if not _EMPTY_SECTION.match(line)]
    for key, value in extra.items():
        if value:
            kept.extend(("", f"**{key.title()}:** {value}"))
    return "\n".join(_single_blank_lines(kept)).rstrip() + "\n"


def _single_blank_lines(lines: Sequence[str]) -> list[str]:
    """Collapse the gaps a dropped section leaves behind into one blank line."""
    collapsed: list[str] = []
    for line in lines:
        if not line.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(line)
    return collapsed


def _clean_title(title: str) -> str:
    """Strip the `TITLE: ` prefix and markdown heading marks the old engines left behind."""
    cleaned = title.strip()
    if cleaned.upper().startswith("TITLE:"):
        cleaned = cleaned[len("TITLE:") :].strip()
    cleaned = cleaned.lstrip("#").strip()
    return cleaned


# --------------------------------------------------------------------------- writing


def import_all(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    archive_root: Path | None = None,
) -> ImportReport:
    """Upsert every archived run. Safe to call on every startup."""
    resolved = settings or get_settings()
    root = Path(archive_root) if archive_root is not None else resolved.import_archive_root
    factory = session_factory or get_session_factory(resolved)

    report = ImportReport()
    seen: set[str] = set()
    for parsed in discover_archived_runs(Path(root)):
        if parsed.engine_run_id in seen:
            report.warnings.append(
                f"duplicate_engine_run_id:{parsed.engine_run_id}:{to_posix(parsed.run_dir)}"
            )
            continue
        seen.add(parsed.engine_run_id)
        with factory() as session:
            try:
                owner_id = session.execute(
                    insert(User)
                    .values(
                        username=resolved.local_identity_username,
                        display_name=resolved.local_identity_username,
                        is_admin=True,
                    )
                    .on_conflict_do_update(
                        index_elements=[User.username],
                        set_={"username": resolved.local_identity_username},
                    )
                    .returning(User.id)
                ).scalar_one()
                import_run_dir(
                    session,
                    parsed,
                    report,
                    archive_root=root,
                    owner_id=owner_id,
                )
                session.commit()
            except Exception:
                session.rollback()
                raise
    return report


def import_run_dir(
    session: Session,
    parsed: ImportedRun,
    report: ImportReport,
    *,
    archive_root: Path,
    owner_id: UUID | None = None,
) -> UUID:
    """Upsert one archived run and all of its children inside the caller's transaction."""
    _refuse_execution_root(parsed.run_dir)
    state = parsed.state
    repairs = _Repairs()

    goal = repairs.text(str(state.get("goal") or "")).strip()
    iteration = _as_int(state.get("iteration")) or 0
    created_at = _as_ts(state.get("created")) or datetime.now(UTC)
    updated_at = _as_ts(state.get("updated")) or created_at
    config = _as_dict(state.get("config"))
    graft_state = _graft_state(state)
    portable_root = archive_root_ref(parsed.run_dir, archive_root)

    engine_state: dict[str, Any] = {
        "imported_from": portable_root,
        "source_version": parsed.source_version,
    }
    if parsed.overview_md:
        engine_state["overview_md"] = repairs.text(parsed.overview_md)
        report.overviews += 1
    if (parsed.run_dir / "ideas_ranked.csv").is_file():
        engine_state["ideas_csv"] = "ideas_ranked.csv"

    feedback = repairs.text(str(state.get("feedback") or "")).strip()
    # History keeps only the last meta-review, so the trajectory the Report tab draws for an
    # imported run is a single point rather than one entry per round.
    feedback_history = [{"round": iteration, "guidance": feedback}] if feedback else []

    run = session.execute(
        select(Run).where(Run.engine_run_id == parsed.engine_run_id)
    ).scalar_one_or_none()
    created = run is None
    if run is None:
        run = Run(engine_run_id=parsed.engine_run_id)
        session.add(run)
    if owner_id is not None:
        run.owner_id = owner_id

    run.source = "imported"
    run.source_version = parsed.source_version
    run.title = derive_title(goal or parsed.engine_run_id)
    run.question = goal
    run.prompt = goal
    run.base_prompt_hash = None
    run.harness = _harness_for(parsed.engine_run_id)
    run.lifecycle = "completed"
    run.round = iteration
    run.rounds_target = iteration
    run.calls_used = _as_int(state.get("calls_used")) or 0
    # No ceilings: an imported run cannot spend, and a budget ring drawn against a
    # fabricated denominator would be the "$0.00" of progress bars.
    run.budget_calls = 0
    run.budget_usd = 0
    run.spend_usd = 0
    run.tokens_in = 0
    run.tokens_out = 0
    run.config = _json_safe(config)
    run.engine_state = engine_state
    run.feedback_history = _json_safe(feedback_history)
    run.graft_state = _json_safe(graft_state)
    run.pending_interventions = []
    run.root_path = portable_root
    run.control_requested = None
    run.supervisor_pid = None
    run.heartbeat_at = None
    run.error = None
    run.archived = False
    run.deleted_at = None
    # Assigned explicitly so a re-import does not stamp every historical run with today,
    # which would collapse the run list's ordering onto the import time.
    run.created_at = created_at
    run.updated_at = updated_at
    session.flush()

    counts = _import_children(session, run, parsed, repairs, report)

    report.imported += 1
    report.created += int(created)
    report.updated += int(not created)
    report.hypotheses += counts["hypotheses"]
    report.reviews += counts["reviews"]
    report.matches += counts["matches"]
    report.graft_events += counts["graft_events"]
    report.legacy_bodies += counts["legacy_bodies"]
    report.repaired_strings += repairs.count
    report.run_ids.append(str(run.id))
    return run.id


def _import_children(
    session: Session,
    run: Run,
    parsed: ImportedRun,
    repairs: _Repairs,
    report: ImportReport,
) -> dict[str, int]:
    state = parsed.state
    existing = {
        row.hid: row
        for row in session.execute(
            select(Hypothesis).where(Hypothesis.run_id == run.id)
        ).scalars()
    }

    legacy_bodies = 0
    kept: set[str] = set()
    reviews = 0
    for raw in _ordered_hypotheses(state):
        hid = str(raw.get("id") or "").strip()
        if not hid:
            report.warnings.append(f"hypothesis_without_id:{parsed.engine_run_id}")
            continue
        kept.add(hid)

        body, was_legacy = _body_for(parsed.run_dir, hid, repairs)
        legacy_bodies += int(was_legacy)
        status = str(raw.get("status") or "active")
        if status not in HYPOTHESIS_STATUSES:
            report.warnings.append(
                f"unknown_hypothesis_status:{parsed.engine_run_id}:{hid}:{status}"
            )
            status = "active"

        row = existing.get(hid)
        if row is None:
            row = Hypothesis(run_id=run.id, hid=hid)
            session.add(row)
        row.title = _clean_title(repairs.text(str(raw.get("title") or hid)))
        row.body_md = body
        row.status = status
        row.elo = _as_float(raw.get("elo")) or 1200.0
        row.matches = _as_int(raw.get("matches")) or 0
        row.wins = _as_int(raw.get("wins")) or 0
        row.cluster = _optional(raw.get("cluster"))
        row.duplicate_of = None
        row.parent_ids = _parent_ids(raw.get("parent"))
        row.operator = None
        row.seed_id = None
        row.created_round = _as_int(raw.get("created_iter")) or 0
        row.source = "agent"
        session.flush()

        session.execute(delete(Review).where(Review.hypothesis_id == row.id))
        review = _as_dict(raw.get("review"))
        verdict = str(review.get("verdict") or "").strip().lower()
        if verdict:
            session.add(
                Review(
                    hypothesis_id=row.id,
                    verdict=verdict,
                    note=repairs.text(str(review.get("note") or "")) or None,
                )
            )
            reviews += 1

    for hid, row in existing.items():
        if hid not in kept:
            session.delete(row)

    matches = _import_matches(session, run, state, repairs)
    graft_events = _import_graft_events(session, run, state)
    session.flush()
    return {
        "hypotheses": len(kept),
        "reviews": reviews,
        "matches": matches,
        "graft_events": graft_events,
        "legacy_bodies": legacy_bodies,
    }


def _import_matches(
    session: Session, run: Run, state: Mapping[str, Any], repairs: _Repairs
) -> int:
    session.execute(delete(Match).where(Match.run_id == run.id))
    count = 0
    for raw in _as_list(state.get("matches")):
        entry = _as_dict(raw)
        hid_a, hid_b = str(entry.get("a") or ""), str(entry.get("b") or "")
        if not hid_a or not hid_b:
            continue
        side = str(entry.get("winner") or "").strip().lower()
        winner = {"a": 1, "b": 2}.get(side)
        session.add(
            Match(
                run_id=run.id,
                round=_as_int(entry.get("iter")) or 0,
                hid_a=hid_a,
                hid_b=hid_b,
                status="completed" if winner else "skipped",
                winner=winner,
                # Elo before/after and K are NULL by necessity: the old engines stored the
                # winner and nothing else, so the rating curve is not reconstructable.
                debate_md=repairs.text(str(entry.get("note") or "")) or None,
                judge_model=None,
                # Individual matches were never timestamped. The run's own last-updated
                # time is the truthful answer to "when was this recorded".
                ts=run.updated_at,
            )
        )
        count += 1
    return count


def _import_graft_events(session: Session, run: Run, state: Mapping[str, Any]) -> int:
    session.execute(delete(GraftEvent).where(GraftEvent.run_id == run.id))
    collapse = _as_dict(state.get("collapse"))
    last_fired = _as_int(collapse.get("last_fired_iter"))
    count = 0
    for raw in _as_list(collapse.get("history")):
        entry = _as_dict(raw)
        round_ = _as_int(entry.get("iter"))
        if round_ is None:
            continue
        session.add(
            GraftEvent(
                run_id=run.id,
                round=round_,
                n_clusters=_as_int(entry.get("n_clusters")),
                hhi=_as_float(entry.get("hhi")),
                signals=_json_safe(
                    {
                        "n_active": _as_int(entry.get("n_active")),
                        "clusters": [str(c) for c in _as_list(entry.get("clusters"))],
                    }
                ),
                # The archived history records how many times a graft fired and the last
                # round it did, never a per-round vote count, so votes stays NULL.
                votes=None,
                fired=round_ == last_fired,
                abstained_reason=None,
            )
        )
        count += 1
    return count


# --------------------------------------------------------------------------- helpers


class _Repairs:
    """Counts how many strings the mojibake repair actually changed on the way in."""

    def __init__(self) -> None:
        self.count = 0

    def text(self, value: str) -> str:
        repaired = repair_mojibake(value)
        if repaired != value:
            self.count += 1
        return repaired


def _body_for(run_dir: Path, hid: str, repairs: _Repairs) -> tuple[str, bool]:
    """Read a hypothesis body, repairing text and re-composing the legacy plaintext era."""
    path = run_dir / "hypotheses" / f"{hid}.md"
    if not path.is_file():
        return "", False
    raw = repairs.text(path.read_text(encoding="utf-8"))
    legacy = normalize_legacy_hypothesis(raw)
    if legacy is None:
        return raw, False
    return compose_legacy_body(legacy), True


def _ordered_hypotheses(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = state.get("hypotheses")
    if isinstance(raw, dict):
        rows = [_as_dict(value) for value in raw.values()]
    else:
        rows = [_as_dict(value) for value in _as_list(raw)]
    return sorted(rows, key=lambda row: str(row.get("id") or ""))


def _graft_state(state: Mapping[str, Any]) -> dict[str, Any]:
    collapse = _as_dict(state.get("collapse"))
    if not collapse:
        return {}
    pending = state.get("pending_injection")
    graft: dict[str, Any] = {
        "fired_count": _as_int(collapse.get("fired_count")) or 0,
        "last_fired_round": _as_int(collapse.get("last_fired_iter")),
        "collapse_history": len(_as_list(collapse.get("history"))),
    }
    if isinstance(pending, str) and pending.strip():
        graft["pending_seed"] = {"seed_framing": pending.strip()}
    return graft


def _harness_for(engine_run_id: str) -> str:
    """Historical runs were driven by Claude, except one Codex smoke the GUI slugged.

    The slug is the only surviving record of which CLI ran a GUI-created run, and only the
    literal `-codex-` token is trusted — a title that merely mentions codex stays `claude`.
    """
    return "codex" if "-codex-" in engine_run_id else "claude"


def _parent_ids(value: Any) -> list[str]:
    """`parent` was one free-text field; a two-parent evolution wrote `"h005,h002"`."""
    if not value:
        return []
    return [part.strip() for part in str(value).split(",") if part.strip()]


def _as_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_int(value: Any) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _optional(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
