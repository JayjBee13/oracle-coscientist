"""The prompt workshop: one bounded model call, a state machine, and nothing else.

A workshop is a scientist's rough question turned into two research goals they can choose
between, refine, and finally edit by hand. Everything here is shaped by what the previous
implementation got wrong (all three are covered by regression tests):

* **A GET never writes.** The old workshop reconciled state on read: fetching a workshop
  could mark it failed, so two identical reads returned different answers and the read path
  needed a process table to consult. Here the only writer of a workshop's outcome is the
  thread that made the call — including when it times out, which it detects itself. Reads
  are selects.
* **No pid, no heartbeat, no background registry.** A workshop is a single call with a hard
  timeout, so liveness bookkeeping buys nothing: either the thread finishes and writes an
  answer, or it hits `timeout_s` and writes `failed`. The module-global set of in-flight
  workshops the old code kept (which broke the moment a second worker process existed) has
  no replacement because it has no job.
* **Nothing is computed that the DTO cannot carry.** The old refiner produced a `warnings`
  list and dropped it on the floor, and rejected any prompt containing `[1]` or `[source]`
  as an unfilled placeholder. There are no heuristics over the model's text here at all:
  the schema is enforced by the CLI and re-validated, the chosen prompt is stored verbatim,
  and a failure reaches the scientist as `error` on the workshop itself.

The call is deliberately singular. Workshops have no run budget, so the cap on spend *is*
the one bounded call — a contract violation ends the workshop with a diagnosis rather than
silently buying a second call, and the scientist can start another workshop for free.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import APP_ROOT, Settings, get_settings
from app.db.engine_models import Workshop, WorkshopOption
from app.db.session import get_session_factory
from app.engine.claude_runner import ClaudeCliRunner
from app.engine.prompts import build_context_block
from app.engine.runners import AgentRunner, FakeRunner, resolve_model_table, role_config
from app.engine.schemas import validate_role_output
from app.services.settings.models import stored_model_settings
from app.services.workshop.prompts import MERGE, WORKSHOP_SYSTEM_PROMPT, workshop_prompt

__all__ = [
    "HARNESSES",
    "OPTIONS_PER_ROUND",
    "QUESTION_MIN_CHARS",
    "WORKSHOP_ROLE",
    "WORKSHOP_TIMEOUT_S",
    "OptionNotFound",
    "WorkshopInputError",
    "WorkshopNotFound",
    "WorkshopService",
    "WorkshopStateError",
    "default_runner_factory",
]

log = logging.getLogger(__name__)

WORKSHOP_ROLE = "workshop"

WORKSHOP_TIMEOUT_S = 180.0
"""Hard ceiling on one workshop call. A scientist is waiting on this one, in front of it."""

_TIMEOUT_GRACE_S = 15.0
"""Slack over `timeout_s` before the service gives up on its own.

The runner is given the same deadline and kills its process tree at it, which produces a
far better diagnosis than "the thread stopped answering" — so this waits a little longer
than the runner does, rather than racing it. Proportional to the budget (capped), because a
test that sets a 50ms timeout should not then wait fifteen seconds for it.
"""

OPTIONS_PER_ROUND = 2
QUESTION_MIN_CHARS = 10
HARNESSES: tuple[str, ...] = ("claude", "demo")

_RECOMMENDED_KEYS: tuple[str, ...] = (
    "rounds",
    "budget_calls",
    "matches_per_round",
    "grounding_depth",
)


class WorkshopNotFound(LookupError):
    """No workshop with that id."""


class OptionNotFound(LookupError):
    """That option id does not belong to this workshop, or is no longer choosable."""


class WorkshopStateError(RuntimeError):
    """The workshop is not in a state where this action makes sense."""

    def __init__(self, action: str, state: str, expected: str) -> None:
        super().__init__(f"Cannot {action} a workshop that is {state!r}; it must be {expected}.")
        self.action = action
        self.state = state
        self.expected = expected


class WorkshopInputError(ValueError):
    """The request cannot be honoured as written. Carries the code the API reports."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ------------------------------------------------------------------------ runner choice


def _no_budget_guard() -> None:
    """A workshop has no run budget to check: the single bounded call is the whole cap."""
    return None


def _use_fake(harness: str, settings: Settings) -> bool:
    """Whether this workshop is answered by `FakeRunner` rather than the CLI.

    `ENGINE_RUNNER` wins when set, so a developer can hold the whole app on the fake
    without editing settings; otherwise a demo workshop is always fake and everything else
    follows `REAL_HARNESS_ENABLED`. The default install therefore has a working workshop
    out of the box instead of a launch that fails with `real_harness_disabled`.
    """
    if harness == "demo":
        return True
    choice = (os.environ.get("ENGINE_RUNNER") or "").strip().lower()
    if choice in {"fake", "demo"}:
        return True
    if choice in {"claude", "real"}:
        return False
    return not settings.real_harness_enabled


def default_runner_factory(settings: Settings | None = None) -> Callable[[str], AgentRunner]:
    """The runner a workshop of this harness gets, resolved per call.

    A router rather than one CLI, for the same reason a run gets one: the workshop row comes
    out of the system default's model table, and that table may name either provider's model.
    Both lanes are registered and neither is constructed until a call needs it, so a workshop
    on Claude never resolves `codex.exe`.
    """
    resolved = settings or get_settings()

    def factory(harness: str) -> AgentRunner:
        if _use_fake(harness, resolved):
            return FakeRunner(seed="workshop")
        workdir = APP_ROOT / ".dev" / "workshops"
        workdir.mkdir(parents=True, exist_ok=True)

        from app.engine.codex_runner import CodexCliRunner
        from app.engine.routing import ProviderRouter

        return ProviderRouter(
            {
                "anthropic": lambda: ClaudeCliRunner(
                    workdir=workdir, budget_guard=_no_budget_guard
                ),
                "openai": lambda: CodexCliRunner(
                    workdir=workdir, budget_guard=_no_budget_guard
                ),
            },
            default_provider="openai" if harness == "codex" else "anthropic",
        )

    return factory


# ------------------------------------------------------------------------------ service


class WorkshopService:
    """Create, refine and choose workshops. Construct freely — it holds no workshop state.

    The thread map is for tests and shutdown only: `wait` is never on a request path, and no
    read consults it. A workshop's truth is its row.
    """

    def __init__(
        self,
        runner_factory: Callable[[str], AgentRunner] | None = None,
        session_factory: sessionmaker[Session] | None = None,
        *,
        settings: Settings | None = None,
        timeout_s: float = WORKSHOP_TIMEOUT_S,
    ) -> None:
        self._settings = settings or get_settings()
        self._session_factory = session_factory or get_session_factory(self._settings)
        self._runner_factory = runner_factory or default_runner_factory(self._settings)
        self._timeout_s = timeout_s
        self._threads: dict[UUID, threading.Thread] = {}
        self._lock = threading.Lock()

    # --- commands ---------------------------------------------------------------------

    def create(
        self,
        question: str,
        *,
        harness: str = "claude",
        context_docs: Sequence[Mapping[str, Any]] | None = None,
        owner_id: UUID | None = None,
    ) -> dict[str, Any]:
        """Open a workshop and start its call. Returns immediately, in state `refining`."""
        question = _clean_question(question)
        harness = _clean_harness(harness)
        docs = _clean_context_docs(context_docs)

        with self._tx() as session:
            workshop = Workshop(
                question=question,
                state="refining",
                harness=harness,
                context_docs=docs,
                owner_id=owner_id,
            )
            session.add(workshop)
            session.flush()
            workshop_id = workshop.id

        prompt = workshop_prompt(question, context=build_context_block(docs))
        dto = self._get_unscoped(workshop_id)
        self._start(workshop_id, prompt, harness)
        return dto

    def refine(
        self,
        workshop_id: UUID,
        *,
        base: str,
        note: str = "",
        owner_id: UUID | None = None,
        is_admin: bool = True,
    ) -> dict[str, Any]:
        """Reject the current pair and ask again, building on one option or merging both."""
        note = (note or "").strip()
        with self._tx() as session:
            workshop = self._locked_visible(session, workshop_id, owner_id, is_admin)
            if workshop.state != "options_ready":
                raise WorkshopStateError("refine", workshop.state, "'options_ready'")

            options = self._options(session, workshop_id)
            live = [option for option in options if not option.rejected and not option.chosen]
            base_option = None
            if base != MERGE:
                base_option = _match_option(live, base)
                if base_option is None:
                    raise OptionNotFound(f"No live option {base!r} on this workshop.")

            history = [
                {
                    "strategy": option.strategy,
                    "prompt": option.prompt,
                    "note": option.note,
                }
                for option in options
                if option.rejected
            ]
            for option in live:
                option.rejected = True
                if note:
                    option.note = note
                history.append(
                    {"strategy": option.strategy, "prompt": option.prompt, "note": note}
                )

            prompt = workshop_prompt(
                workshop.question,
                context=build_context_block(workshop.context_docs or []),
                base=MERGE if base_option is None else _option_payload(base_option),
                note=note,
                history=history,
            )
            workshop.state = "refining"
            workshop.error = None
            harness = workshop.harness

        dto = self.get(workshop_id, owner_id=owner_id, is_admin=is_admin)
        self._start(workshop_id, prompt, harness)
        return dto

    def choose(
        self,
        workshop_id: UUID,
        *,
        option_id: str,
        final_prompt: str = "",
        owner_id: UUID | None = None,
        is_admin: bool = True,
    ) -> dict[str, Any]:
        """Settle on one option. The scientist's edit wins, stored exactly as they typed it.

        Whatever `final_prompt` contains is the prompt: no placeholder detection, no
        trimming of the body, no re-wrapping. A goal that cites `[1]` or quotes `[source]`
        is a normal goal.
        """
        with self._tx() as session:
            workshop = self._locked_visible(session, workshop_id, owner_id, is_admin)
            if workshop.state != "options_ready":
                raise WorkshopStateError("choose from", workshop.state, "'options_ready'")

            options = self._options(session, workshop_id)
            option = _match_option(
                [candidate for candidate in options if not candidate.rejected], option_id
            )
            if option is None:
                raise OptionNotFound(f"No live option {option_id!r} on this workshop.")

            prompt = final_prompt if final_prompt.strip() else option.prompt
            option.prompt = prompt
            option.chosen = True
            workshop.state = "chosen"

        return {"prompt": prompt}

    # --- reads (never write) ----------------------------------------------------------

    def get(
        self, workshop_id: UUID, *, owner_id: UUID | None = None, is_admin: bool = True
    ) -> dict[str, Any]:
        """The Workshop DTO. Pure select: calling it twice changes nothing."""
        with self._read() as session:
            query = select(Workshop).where(Workshop.id == workshop_id)
            if not is_admin:
                query = query.where(Workshop.owner_id == owner_id)
            workshop = session.execute(query).scalar_one_or_none()
            if workshop is None:
                raise WorkshopNotFound(f"No workshop {workshop_id}")
            return _workshop_payload(workshop, self._options(session, workshop_id))

    def _get_unscoped(self, workshop_id: UUID) -> dict[str, Any]:
        return self.get(workshop_id, is_admin=True)

    def wait(self, workshop_id: UUID, timeout: float | None = None) -> bool:
        """Join this workshop's call thread. For tests and shutdown, never for a request."""
        with self._lock:
            thread = self._threads.get(workshop_id)
        if thread is None:
            return True
        thread.join(timeout if timeout is not None else self._deadline() + 5)
        return not thread.is_alive()

    # --- the call ---------------------------------------------------------------------

    def _start(self, workshop_id: UUID, prompt: str, harness: str) -> None:
        thread = threading.Thread(
            target=self._work,
            args=(workshop_id, prompt, harness),
            name=f"workshop-{workshop_id}",
            daemon=True,
        )
        with self._lock:
            self._threads[workshop_id] = thread
        thread.start()

    def _work(self, workshop_id: UUID, prompt: str, harness: str) -> None:
        """One call, off the request thread. Every exit from here writes a terminal state."""
        try:
            result = asyncio.run(self._call(prompt, harness))
        except TimeoutError:
            self._fail(
                workshop_id,
                "workshop_timeout",
                f"The workshop did not answer within {self._timeout_s:g} seconds.",
            )
            return
        except Exception as exc:  # noqa: BLE001 - a crashed thread must still be diagnosable
            log.exception("workshop %s failed", workshop_id)
            self._fail(workshop_id, "workshop_failed", f"{type(exc).__name__}: {exc}")
            return

        if not result.ok:
            self._fail(
                workshop_id,
                "workshop_call_failed",
                result.error or "The model call failed without saying why.",
                detail=result.raw_tail,
            )
            return

        problem = validate_role_output(WORKSHOP_ROLE, result.data)
        if problem is not None:
            self._fail(workshop_id, "contract_violation", problem, detail=result.raw_tail)
            return

        options = list((result.data or {}).get("options") or [])[:OPTIONS_PER_ROUND]
        self._store(workshop_id, options)

    async def _call(self, prompt: str, harness: str):
        runner = self._runner_factory(harness)
        row = _model_row()
        cfg = replace(
            role_config(
                WORKSHOP_ROLE,
                model=row["model"],
                effort=row["effort"],
                system_prompt=WORKSHOP_SYSTEM_PROMPT,
            ),
            timeout_s=self._timeout_s,
        )
        try:
            return await asyncio.wait_for(
                runner.run_role(WORKSHOP_ROLE, prompt, cfg), timeout=self._deadline()
            )
        finally:
            await runner.aclose()

    def _deadline(self) -> float:
        return self._timeout_s + min(_TIMEOUT_GRACE_S, self._timeout_s * 0.1)

    # --- outcome ----------------------------------------------------------------------

    def _store(self, workshop_id: UUID, options: Sequence[Mapping[str, Any]]) -> None:
        with self._tx() as session:
            workshop = self._locked(session, workshop_id, missing_ok=True)
            if workshop is None:
                log.warning("workshop %s vanished before its options landed", workshop_id)
                return
            existing = self._options(session, workshop_id)
            ordinal = max((option.ordinal for option in existing), default=-1) + 1
            for offset, option in enumerate(options):
                session.add(
                    WorkshopOption(
                        workshop_id=workshop_id,
                        ordinal=ordinal + offset,
                        prompt=str(option.get("prompt") or ""),
                        strategy=str(option.get("strategy") or ""),
                        optimizes_for=_text(option.get("optimizes_for")),
                        excludes=_text(option.get("excludes")),
                        rationale=_text(option.get("rationale")),
                        recommended_settings=_recommended(option.get("recommended_settings")),
                    )
                )
            workshop.state = "options_ready"
            workshop.error = None

    def _fail(
        self, workshop_id: UUID, code: str, message: str, *, detail: str | None = None
    ) -> None:
        """Record why this workshop cannot go on. Only ever fails a workshop still refining."""
        log.warning("workshop %s failed: %s (%s)", workshop_id, message, code)
        with self._tx() as session:
            workshop = self._locked(session, workshop_id, missing_ok=True)
            if workshop is None or workshop.state != "refining":
                return
            error: dict[str, Any] = {"code": code, "message": message}
            if detail:
                error["detail"] = detail[-2000:]
            workshop.state = "failed"
            workshop.error = error

    # --- persistence ------------------------------------------------------------------

    @contextmanager
    def _tx(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def _read(self) -> Iterator[Session]:
        """A session that is never committed, so a read cannot become a write by accident."""
        session = self._session_factory()
        try:
            yield session
        finally:
            session.rollback()
            session.close()

    def _locked(
        self, session: Session, workshop_id: UUID, *, missing_ok: bool = False
    ) -> Workshop | None:
        """The workshop row, held for the transaction so two refines cannot interleave."""
        workshop = session.execute(
            select(Workshop).where(Workshop.id == workshop_id).with_for_update()
        ).scalar_one_or_none()
        if workshop is None and not missing_ok:
            raise WorkshopNotFound(f"No workshop {workshop_id}")
        return workshop

    def _locked_visible(
        self,
        session: Session,
        workshop_id: UUID,
        owner_id: UUID | None,
        is_admin: bool,
    ) -> Workshop:
        query = select(Workshop).where(Workshop.id == workshop_id)
        if not is_admin:
            query = query.where(Workshop.owner_id == owner_id)
        workshop = session.execute(query.with_for_update()).scalar_one_or_none()
        if workshop is None:
            raise WorkshopNotFound(f"No workshop {workshop_id}")
        return workshop

    @staticmethod
    def _options(session: Session, workshop_id: UUID) -> list[WorkshopOption]:
        return list(
            session.execute(
                select(WorkshopOption)
                .where(WorkshopOption.workshop_id == workshop_id)
                .order_by(WorkshopOption.ordinal, WorkshopOption.created_at)
            ).scalars()
        )


# --------------------------------------------------------------------------- shaping


def _model_row() -> dict[str, str]:
    """The workshop line of the **system default's** model table.

    A workshop runs before a run exists, so it has no tier of its own to read — it inherits
    the same persisted default a new run would, which is what makes the top bar's promise
    ("this is what the system runs on") true of the first call a scientist ever makes rather
    than only of runs. Reading a hard-coded tier here instead is how the editor comes to say
    one thing while the workshop quietly does another.

    Unreadable settings fall back to the built-in default rather than failing: see
    `services/settings/models.stored_model_settings`.
    """
    default = stored_model_settings()
    for row in resolve_model_table(
        default.provider, default.tier, overrides=default.overrides
    ):
        if row["role"] == WORKSHOP_ROLE:
            return row
    raise KeyError("the model table has no workshop role")  # pragma: no cover


def _clean_question(question: str) -> str:
    text = (question or "").strip()
    if len(text) < QUESTION_MIN_CHARS:
        raise WorkshopInputError(
            "question_too_short",
            f"Describe the question in at least {QUESTION_MIN_CHARS} characters so the "
            "workshop has something to work with.",
        )
    return text


def _clean_harness(harness: str) -> str:
    text = (harness or "claude").strip().lower()
    if text not in HARNESSES:
        raise WorkshopInputError(
            "unknown_harness", f"Unknown harness {harness!r}; expected one of {HARNESSES}."
        )
    return text


def _clean_context_docs(docs: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """Context documents as stored: name, text and the size the Settings tab reports."""
    cleaned: list[dict[str, Any]] = []
    for doc in docs or []:
        text = str(doc.get("text") or doc.get("content") or "")
        if not text.strip():
            continue
        name = str(doc.get("name") or f"document {len(cleaned) + 1}")
        cleaned.append({"name": name, "text": text, "chars": len(text)})
    return cleaned


def _match_option(options: Sequence[WorkshopOption], option_id: str) -> WorkshopOption | None:
    wanted = str(option_id).strip()
    return next((option for option in options if str(option.id) == wanted), None)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _recommended(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: value[key] for key in _RECOMMENDED_KEYS if key in value}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _option_payload(option: WorkshopOption) -> dict[str, Any]:
    return {
        "id": str(option.id),
        "ordinal": option.ordinal,
        "prompt": option.prompt,
        "strategy": option.strategy,
        "optimizes_for": option.optimizes_for,
        "excludes": option.excludes,
        "rationale": option.rationale,
        "recommended_settings": dict(option.recommended_settings or {}),
        "chosen": option.chosen,
        "rejected": option.rejected,
        "note": option.note,
    }


def _workshop_payload(
    workshop: Workshop, options: Sequence[WorkshopOption]
) -> dict[str, Any]:
    return {
        "id": str(workshop.id),
        "question": workshop.question,
        "state": workshop.state,
        "harness": workshop.harness,
        "options": [_option_payload(option) for option in options],
        "error": workshop.error,
        "created_at": _iso(workshop.created_at),
    }
