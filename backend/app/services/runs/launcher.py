"""Create a run and start the process that executes it.

`POST /runs` is the only place money starts being spent, so this module is where the
decisions that cannot be changed afterwards get made and written down:

* **The model table is resolved now, not at call time.** A run stores the exact
  role → model → effort table it will use in its immutable `config`. Resolving it per call
  would mean a run that started on Opus finishes on whatever the default became after a
  restart, and half its Elo ratings would come from a different judge. This is also where
  the model allowlist is enforced: `resolve_model_table` refuses anything below the Opus
  floor, and the refusal happens before a run row exists rather than after a call is paid
  for.
* **The workdir layout is fixed.** `<runs_root>/<engine_run_id>/`, because the artifact
  projection resolves a run's engine root as the parent of `runs/` and refuses anything
  else. The supervisor's cwd is that directory and every file it writes lands under it.
* **Secrets go through the environment.** The child gets the connection string in its
  environment and a uuid on its command line. Nothing else.
* **The lane is checked, then rechecked by the database.** The reconciler runs first so a
  crashed supervisor's stale lane does not block a launch; the partial unique index is the
  real gate, and its `IntegrityError` is what becomes the 409 — a check-then-insert would
  have a race in it.

The spawn itself follows the plan's Windows rules: `sys.executable`,
`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW`, stdin from `DEVNULL` and
both output streams into the run's own log file. A supervisor attached to the API server's
pipes would die with the next backend restart and fill its buffer in the meantime.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.engine.core import RunConfig
from app.engine.models import PROVIDER_HARNESS, PROVIDERS, ModelPolicyError
from app.engine.runners import MODEL_TIERS, resolve_model_table
from app.engine.spawn import python_executable, record_spawn
from app.engine.store import RunStore, derive_title, new_engine_run_id
from app.services.runs.paths import live_root_ref, to_posix
from app.services.settings.models import ModelSettings, stored_model_settings

__all__ = [
    "GROUNDING_DEPTHS",
    "RUNNERS",
    "SUPERVISOR_LOG",
    "LaneBusy",
    "LaunchRefused",
    "LaunchResult",
    "base_prompt_hash",
    "lane_busy",
    "launch_run",
    "resolve_config",
    "spawn_supervisor",
]

log = logging.getLogger(__name__)

BACKEND_ROOT = Path(__file__).resolve().parents[3]

SUPERVISOR_LOG = "supervisor.log"

RUNNERS: tuple[str, ...] = ("claude", "codex", "demo")
GROUNDING_DEPTHS: tuple[str, ...] = ("shallow", "standard", "deep")

MAX_ROUNDS = 25
MAX_BATCH = 40
MAX_MATCHES = 100


class LaunchRefused(ValueError):
    """The request cannot become a run: a missing prompt, a config outside its bounds.

    Also raised by `controls.continue` for the requests that cannot become *more* run — an
    imported run with no model table to spawn against, a round target past the ceiling, a
    budget with no room left for another call. Same envelope, because it is the same answer
    to the caller: nothing was started, and the message says what to change.
    """


class LaneBusy(RuntimeError):
    """Another run already holds this harness lane."""

    def __init__(
        self,
        harness: str,
        conflicting_run_id: str | None,
        *,
        title: str | None = None,
        owner_display_name: str | None = None,
    ) -> None:
        self.harness = harness
        self.conflicting_run_id = conflicting_run_id
        label = harness.capitalize()
        if title and owner_display_name:
            message = f"{label} lane busy: {owner_display_name}'s run *{title}*."
        elif title:
            message = f"{label} lane busy: run *{title}*."
        else:
            message = f"Shared {label} capacity is currently in use. Try again later."
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class LaunchResult:
    run_id: UUID
    engine_run_id: str
    workdir: Path
    pid: int
    harness: str


# --------------------------------------------------------------------------- the config


def resolve_config(
    config: Mapping[str, Any] | None, *, defaults: ModelSettings | None = None
) -> dict[str, Any]:
    """Normalise a request's config into the immutable document a run stores.

    Unknown keys are dropped and missing ones defaulted, so what lands in the database is
    exactly plan C5's shape whatever the caller sent — and `model_table` is added, which is
    the whole point: it freezes the models for the life of the run.

    **The three model fields inherit the persisted system default when the request does not
    state them**, field by field. The top bar writes that default; the wizard reads it,
    shows it, and sends back whatever the scientist changed. A request that says nothing
    therefore launches exactly what the top bar promised it would, and one that names a tier
    keeps the operator's per-role choices around it — those are deliberate decisions about
    individual steps, not decoration on a tier.

    This is also the only place a model id is checked against the allowlist before money is
    spent. A `ModelPolicyError` becomes a `LaunchRefused` verbatim, because its message
    names the specific trap — `claude-fable-5` silently resolving to Opus 5 is not something
    a caller can diagnose from "invalid model".
    """
    stated = dict(config or {})
    stated.setdefault("workflow", "adaptive")
    inherited = defaults if defaults is not None else stored_model_settings()
    if not stated.get("provider"):
        stated["provider"] = inherited.provider
    if not stated.get("model_tier"):
        stated["model_tier"] = inherited.tier
    if stated.get("model_overrides") is None:
        stated["model_overrides"] = inherited.overrides

    try:
        resolved = RunConfig.from_mapping(stated)
        _validate(resolved)
        document = resolved.to_dict()
        document["model_table"] = resolve_model_table(
            resolved.provider, resolved.model_tier, overrides=resolved.model_overrides
        )
    except ModelPolicyError as exc:
        raise LaunchRefused(str(exc)) from exc
    return document


def _validate(config: RunConfig) -> None:
    if config.workflow not in ("adaptive", "tournament"):
        raise LaunchRefused("workflow must be adaptive or tournament")
    if not 1 <= config.rounds <= MAX_ROUNDS:
        raise LaunchRefused(f"rounds must be between 1 and {MAX_ROUNDS}, got {config.rounds}")
    if not 1 <= config.generation_batch <= MAX_BATCH:
        raise LaunchRefused(
            f"generation_batch must be between 1 and {MAX_BATCH}, got {config.generation_batch}"
        )
    if not 0 <= config.matches_per_round <= MAX_MATCHES:
        raise LaunchRefused(
            f"matches_per_round must be between 0 and {MAX_MATCHES}, "
            f"got {config.matches_per_round}"
        )
    if config.evolve_top_k < 0:
        raise LaunchRefused(f"evolve_top_k cannot be negative, got {config.evolve_top_k}")
    # The call ceiling is the governor and is always required — it is what stops a runaway
    # loop. The dollar and wall-clock ceilings are optional; a run without either is the
    # normal case, because the calls this engine makes are not billed per token. When one
    # *is* given it has to be a real ceiling, since zero-or-less would silently read back
    # as "no ceiling" and quietly do the opposite of what was asked.
    if config.budget_calls <= 0:
        raise LaunchRefused("budget_calls must be greater than 0")
    if config.budget_usd is not None and config.budget_usd <= 0:
        raise LaunchRefused("budget_usd must be greater than 0 when it is set")
    if config.wall_clock_minutes is not None and config.wall_clock_minutes <= 0:
        raise LaunchRefused("wall_clock_minutes must be greater than 0 when it is set")
    # The tier itself was already normalised (and refused if it is not a tier) by
    # `RunConfig.from_mapping`; this catches a tier that exists but was dropped from the
    # table, which would otherwise resolve to an empty override set and look fine.
    if config.model_tier not in MODEL_TIERS:
        raise LaunchRefused(
            f"unknown model_tier {config.model_tier!r}; expected one of {MODEL_TIERS}"
        )
    if config.runner not in RUNNERS:
        raise LaunchRefused(f"unknown runner {config.runner!r}; expected one of {RUNNERS}")
    if config.provider not in PROVIDERS:
        raise LaunchRefused(
            f"unknown provider {config.provider!r}; expected one of {PROVIDERS}"
        )
    if config.grounding_depth not in GROUNDING_DEPTHS:
        raise LaunchRefused(
            f"unknown grounding_depth {config.grounding_depth!r}; "
            f"expected one of {GROUNDING_DEPTHS}"
        )


def base_prompt_hash(prompt: str) -> str:
    """Identify the prompt two runs share, ignoring whitespace they do not.

    `/compare` uses this to tell an A/B of one prompt from two unrelated runs put side by
    side, so it has to survive a trailing newline and an editor's re-wrap.
    """
    normalised = " ".join(prompt.split())
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- launching


def launch_run(
    store: RunStore,
    *,
    question: str | None = None,
    prompt: str | None = None,
    title: str | None = None,
    harness: str = "claude",
    from_run: str | None = None,
    context_docs: Sequence[Mapping[str, str]] | None = None,
    config: Mapping[str, Any] | None = None,
    settings: Settings | None = None,
    owner_id: UUID | None = None,
    reveal_conflict: bool = False,
) -> LaunchResult:
    """Create the run row, its workdir and its supervisor. Returns once the child is alive.

    The lifecycle stays `queued` here. Moving it to `running` is the supervisor's first act,
    which is what makes "queued for longer than a minute with a dead pid" a diagnosable
    state rather than an ambiguous one.

    This always makes a **new** run, `from_run` included — see `_inherit` for what that
    copies and, more importantly, what it does not. Adding rounds to a run that already
    exists is the `continue` control action in `services/runs/controls.py`, which keeps the
    run's whole idea pool instead of starting a fresh one.
    """
    resolved_settings = settings or get_settings()

    if from_run:
        question, prompt, config, context_docs = _inherit(
            store, from_run, question, prompt, config, context_docs
        )
    question = (question or "").strip()
    prompt = (prompt or "").strip() or question
    if not question:
        raise LaunchRefused("a run needs a question")
    if not prompt:
        raise LaunchRefused("a run needs a prompt")

    document = resolve_config(config)
    harness = _align_harness(harness, document)

    # Before the lane check, not only on startup: a supervisor that died while the backend
    # stayed up would otherwise wedge its lane until the next restart. Imported inside the
    # function because controls imports this module back, for resume.
    from app.services.runs.controls import reconcile_runs

    reconcile_runs(store)

    # The id is minted here rather than by the store because the workdir is named after it
    # and has to be recorded in the same insert. The directory itself is only created once
    # the insert succeeds, so a launch refused by the lane leaves nothing behind.
    engine_run_id = new_engine_run_id()
    workdir = resolved_settings.workdir_for(engine_run_id)

    try:
        run_id = store.create_run(
            question=question,
            prompt=prompt,
            title=(title or "").strip() or derive_title(question),
            harness=harness,
            config=document,
            engine_run_id=engine_run_id,
            base_prompt_hash=base_prompt_hash(prompt),
            context_docs=list(context_docs or []),
            root_path=live_root_ref(engine_run_id),
            lifecycle="queued",
            owner_id=owner_id,
        )
    except IntegrityError as exc:
        raise lane_busy(
            store,
            harness,
            requester_owner_id=owner_id,
            reveal_conflict=reveal_conflict,
        ) from exc

    try:
        workdir.mkdir(parents=True, exist_ok=True)
        pid = spawn_supervisor(run_id, settings=resolved_settings, workdir=workdir)
    except Exception as exc:  # noqa: BLE001 — a run that never started must say so
        log.exception("could not start a supervisor for run %s", run_id)
        _fail_to_start(store, run_id, exc)
        raise

    # Stamping the pid and a fresh heartbeat here, rather than waiting for the child to do
    # it, is what makes force-stop work during the seconds a Python interpreter takes to
    # boot — and what stops the reconciler seeing a brand-new run as an orphan.
    store.heartbeat(run_id, pid=pid)
    log.info("launched run %s (%s) as pid %s in %s", run_id, engine_run_id, pid, workdir)
    return LaunchResult(
        run_id=run_id,
        engine_run_id=engine_run_id,
        workdir=workdir,
        pid=pid,
        harness=harness,
    )


def _align_harness(harness: str, document: dict[str, Any]) -> str:
    """Keep the harness, the runner and the provider telling the same story.

    Demo runs occupy their own lane on purpose: practising the interface while a real run
    is in flight is a thing people do, and the two never contend for anything. What must
    not happen is a run labelled `demo` that quietly spends money, so the fields are
    reconciled here instead of trusted to agree.

    For a real run the lane follows the **config's provider**, not the request's `harness`.
    The lane exists to stop two runs contending for one CLI, and a mixed-provider run drives
    mostly the CLI its untouched rows use — which is what `provider` names. A run whose table
    is entirely Codex would otherwise sit in the Claude lane and block a Claude run for no
    reason. (Solo-operator semantics: one lane per CLI, revisit if that changes.)
    """
    if harness == "demo" or str(document.get("runner") or "claude") == "demo":
        document["runner"] = "demo"
        return "demo"
    lane = PROVIDER_HARNESS[str(document.get("provider") or "anthropic")]
    document["runner"] = lane
    return lane


def _inherit(
    store: RunStore,
    from_run: str,
    question: str | None,
    prompt: str | None,
    config: Mapping[str, Any] | None,
    context_docs: Sequence[Mapping[str, str]] | None,
) -> tuple[str, str, dict[str, Any], list[Mapping[str, str]]]:
    """"Run again": inherit the question, prompt, config and context of an earlier run.

    This copies the *setup* and starts the science over. The clone begins with no
    hypotheses, every rating back at 1200, no clusters and no meta-review guidance — which
    is exactly what it is for: asking the same question again from a clean slate, to see
    whether the answer reproduces, or to change one setting and compare the two runs.

    It is emphatically not "carry on where that one left off". That is the `continue`
    control action, which adds rounds to the run itself and keeps everything it learned. A
    scientist who liked a Quick run and wants to go deeper wants `continue`; one who wants a
    second independent sample of the same question wants this. Picking the wrong one is the
    kind of mistake that costs a full run's worth of calls, so the two are named apart
    everywhere they appear.

    Anything the caller supplied wins, key by key for the config, so "same run but three
    rounds instead of two" is one field rather than a re-entered form — and *only* what they
    supplied, which is why `CreateRunRequest.config_document()` dumps `exclude_unset`. A dump
    of every field cannot be told apart from a request that stated every field, so the merge
    below would write the request model's defaults over the whole inherited setup and a
    clone would reproduce today's system default instead of the run it names.

    `model_table` is deliberately *not* inherited — it is re-resolved from the tier, so a
    clone made after a model id changed gets the current table rather than a stale one.
    """
    try:
        source_id = UUID(from_run)
    except ValueError:
        raise LaunchRefused(f"from_run must be a run uuid, got {from_run!r}") from None
    source = store.get_run(source_id)
    if source is None or source.get("deleted_at"):
        raise LaunchRefused(f"no run {from_run} to copy")

    inherited = {k: v for k, v in dict(source.get("config") or {}).items() if k != "model_table"}
    # Every config written before providers existed is an Anthropic one. Spelling that out
    # here rather than letting it fall through to the *current* system default is the whole
    # difference between "ask this question again" and "ask this question again on whatever
    # the top bar happens to say today" — a clone reproduces a setup, so an absent field is
    # what that run used, not what a new run would get.
    inherited.setdefault("provider", "anthropic")
    inherited.setdefault("workflow", "tournament")
    inherited.update(_stated(config))
    docs = list(context_docs or []) or [
        {"name": doc["name"], "text": doc["content"]}
        for doc in store.get_context_docs(source_id)
    ]
    return (
        (question or source.get("question") or ""),
        (prompt or source.get("prompt") or ""),
        inherited,
        docs,
    )


INHERITABLE_NULLS: tuple[str, ...] = ("provider", "model_tier", "model_overrides")
"""The three fields on which a stated `null` is not a value but an abstention.

`RunConfigIn` documents all three the same way: `null` means "whatever the system default
says". `resolve_config` reads them exactly like that. For a clone the thing being inherited
from is the *source run*, not the top bar, so a `null` on one of them has to leave the
inherited value standing rather than erase it.

Every other field is left exactly as sent, `null` included — on `budget_usd` a `null` is a
request in its own right ("no ceiling"), and treating it as an abstention would silently put
a stopped run's old ceiling back on the clone."""


def _stated(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """What the caller actually asked for, ready to merge over an inherited config.

    Two things get filtered out, and they are the same thing seen from two sides: a field the
    caller did not mention (already absent, because `config_document` dumps `exclude_unset`),
    and a field they mentioned with the value that means "I have no opinion". The wizard
    posts its whole draft, and a draft cloned from a run written before `provider` existed has
    nothing to put in that field — so it sends `null` and would otherwise erase the very
    inheritance it asked for.
    """
    return {
        key: value
        for key, value in dict(config or {}).items()
        if not (value is None and key in INHERITABLE_NULLS)
    }


def lane_busy(
    store: RunStore,
    harness: str,
    *,
    requester_owner_id: UUID | None = None,
    reveal_conflict: bool = False,
) -> LaneBusy:
    """Describe the run holding a lane only when the requester may see it.

    Shared with `controls.continue`, which reclaims a lane the same way a launch takes one
    — by writing a lane lifecycle and letting the partial unique index decide.
    """
    holder = store.lane_holder(harness)
    can_see_holder = bool(
        holder is not None
        and (
            reveal_conflict
            or (
                requester_owner_id is not None
                and holder["owner_id"] == str(requester_owner_id)
            )
        )
    )
    return LaneBusy(
        harness,
        holder["id"] if can_see_holder else None,
        title=holder["title"] if can_see_holder else None,
        owner_display_name=holder["owner_display_name"] if can_see_holder else None,
    )


def _fail_to_start(store: RunStore, run_id: UUID, exc: BaseException) -> None:
    """Record a run that could not be started, so it is diagnosable rather than stuck."""
    from app.engine.events import EventType, EventWriter

    error = {"type": type(exc).__name__, "message": str(exc)[:2000], "where": "launcher"}
    try:
        store.set_lifecycle(run_id, "failed", error=error)
        EventWriter(store, run_id).emit(EventType.RUN_FAILED, {"error": error})
    except Exception:  # noqa: BLE001
        log.exception("could not record the failed launch of run %s", run_id)


# ------------------------------------------------------------------------------- spawn


def spawn_supervisor(run_id: UUID, *, settings: Settings, workdir: Path) -> int:
    """Start a detached supervisor for a run and return its pid.

    Detached in the Windows sense: its own process group (so a Ctrl-C in the console that
    started the backend does not reach it), no console window, and no inherited pipes.
    Output goes to `supervisor.log` in the run's own directory, appended — a resumed run
    keeps the history of the attempt before it.
    """
    argv = [
        python_executable(),
        "-m",
        "app.engine.supervisor",
        "--run-id",
        str(run_id),
    ]
    _assert_no_secrets(argv)

    log_path = workdir / SUPERVISOR_LOG
    record_spawn({"ts": time.time(), "argv": list(argv), "cwd": to_posix(workdir)})

    with open(log_path, "ab", buffering=0) as handle:
        handle.write(
            f"\n=== supervisor start {time.strftime('%Y-%m-%d %H:%M:%S')} "
            f"run={run_id} ===\n".encode()
        )
        process = subprocess.Popen(  # noqa: S603 — argv is ours, and asserted above
            argv,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=supervisor_env(settings),
            creationflags=_creation_flags(),
            start_new_session=os.name != "nt",
            close_fds=True,
        )
    return process.pid


def supervisor_env(settings: Settings) -> dict[str, str]:
    """The child's environment: ours, plus the settings it must not read off a command line.

    Only what the supervisor actually resolves is forwarded explicitly — the connection
    string and the runs root — because those are the two that differ between a test run
    and a real one, and a child that silently read the `.env` file instead would write test
    data into the operator's database.
    """
    env = dict(os.environ)
    env["DATABASE_URL"] = settings.database_url
    env["COSCIENTIST_RUNS_ROOT"] = str(settings.runs_root)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(BACKEND_ROOT), *([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])]
    )
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _creation_flags() -> int:
    if os.name != "nt":  # pragma: no cover — this project runs on Windows
        return 0
    return (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )


def _assert_no_secrets(argv: Sequence[str]) -> None:
    """A command line is public. Connection strings and tokens travel in the environment."""
    for index, item in enumerate(argv):
        if "://" in item:
            raise LaunchRefused(
                f"supervisor argv[{index}] contains '://' — the DSN goes in the environment"
            )
