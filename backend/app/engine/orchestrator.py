"""The supervisor loop: deterministic code driving probabilistic workers.

One `Orchestrator` owns one run from launch to a terminal lifecycle. It is the only writer
of that run's state, and it holds these invariants — carried over from the archived
`SKILL.md` supervisor principles, which were prose instructions to a model and are now
assertions in code:

* **The orchestrator owns every mutation.** Roles return structured data; nothing an agent
  says becomes state until this module has validated it and written it through `RunStore`.
  No agent tracks a score, allocates an id, or writes a file.
* **`emit` has exactly one writer.** Role calls run in parallel, but their results come back
  to this coroutine, which records and emits them one at a time. `seq` order therefore
  matches commit order, which is what makes SSE resume correct.
* **Work is resumable per unit, never per round.** Every step either persists its plan
  before executing it (the round's matches) or derives its remaining work from the database
  (hypotheses lacking a review). A run killed mid-round and restarted re-executes nothing
  it already paid for.
* **Spend most compute on verification.** The loop is biased toward reflection and ranking:
  a firehose of confident, ungrounded ideas is the failure mode this design exists to catch.
* **The budget is a wall, not a suggestion.** Two calls are reserved at run start for the
  overview, every other step checks the ceiling before it spends, and hitting either
  ceiling ends the run *gracefully* — with a report — rather than by stopping mid-sentence.
* **Stopping still writes the report.** Stop, finish and budget exhaustion all reach the
  same ending: an overview composed from whatever exists. That is the promise the Confirm
  step makes to the scientist, so it is enforced here rather than trusted to a caller.

The round is plan C3 step for step: interventions, sharded generation, reflection,
proximity, collapse check, the persisted pair plan and its tournament, evolution, and the
meta-review that steers the next round.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.engine.core import (
    ABSTAIN_COOLDOWN,
    ABSTAIN_NO_CLUSTERS,
    ABSTAIN_PARTIAL_CLUSTERS,
    ABSTAIN_STALE_CLUSTERS,
    CONCENTRATED_POOL_SHARE,
    GENERATION_SHARD_SIZE,
    OVERVIEW_RESERVED_CALLS,
    PARALLEL_CALLS,
    CollapseSnapshot,
    HypRow,
    Pair,
    RunConfig,
    TopShare,
    collapse_signals,
    compose_hypothesis_md,
    make_pairs,
    meeting_key,
    presentation_order,
    presentation_swap,
    should_fire_graft,
    top_share,
    winner_side,
)
from app.engine.events import EventType, EventWriter
from app.engine.models import effort_at_least
from app.engine.prompts import (
    CONTEXT_CHAR_CAP,
    ContextBlock,
    RoundContext,
    Seed,
    build_context_block,
    cartographer_prompt,
    evolution_prompt,
    exploration_directive,
    generation_prompt,
    meta_review_prompt,
    overview_prompt,
    proximity_prompt,
    ranking_prompt,
    reflection_prompt,
    render_system_prompt,
    repair_suffix,
    source_strategy_prompt,
)
from app.engine.runners import AgentRunner, RoleConfig, RoleResult, role_config
from app.engine.schemas import hypothesis_fields, unwrap_prose, validate_role_output
from app.engine.store import BudgetExhausted, RunStore

__all__ = ["MAX_EXISTING_TITLES", "Orchestrator", "RunHalted", "run_engine"]

log = logging.getLogger(__name__)

TERMINAL_LIFECYCLES: frozenset[str] = frozenset({"completed", "stopped", "failed", "lost"})

MAX_EXISTING_TITLES = 80
"""Titles shown to generation as EXISTING. Older ones fall off: the prompt must stay a
prompt, and a title from round 1 is already represented in the pool it produced."""

TOP_TIER = 5
"""Ranking escalates to high effort when both sides are inside the top this many."""

OVERVIEW_TOP_K = 5
"""Hypotheses the report is shown in full. The rest of the ranked pool is summarised.

Named rather than inlined because the number is now *stated in the prompt* — the cut used
to be an unannounced `[:5]` under a header reading "these played matches", which reads as
though the five listed are the ones that played. Fourteen of run c4566ed2's hypotheses had
played matches."""

MAX_EVOLUTION_ATTEMPTS = 2
"""How many times a round's evolution call may be attempted across resumes before the step
is written off. Without a cap, leaving a failed step unmarked so a resume retries it would
let a deterministically failing call be re-attempted forever."""

RATE_LIMIT_COOLDOWN = 30.0

MAX_CALL_ATTEMPTS = 2
"""Attempts one scheduled unit gets. One try, and one retry on different terms."""

TIMEOUT_RETRY_FACTOR = 2.0
"""What a retried timeout gets instead of the deadline it already broke.

Retrying a deadline breach on identical terms cannot change P(success); it only spends the
wall clock and a budget call a second time. Run c4566ed2 proves the arithmetic: 5 units
timed out, 10 attempts were spent, and the 2 that recovered did so only by landing inside
the same wall — the other 6 attempts burned 42 minutes of a 52-minute run for nothing, and
the run then hit its cost ceiling before it could write a report.
"""

CALL_TELEMETRY_FIELDS: tuple[str, ...] = (
    # Which CLI actually ran this call, at what effort, and whether the effort it ran at is
    # the one the table asked for. A run may mix providers per step, so "which model" no
    # longer implies "which lane" to a reader of the Activity tab — and an effort clamped to
    # a model's own ladder is a call that did not run on the terms the tier states.
    "provider",
    "effort",
    "effort_clamped",
    "model_verified",
    "permission_denials",
    "web_searches",
    "tool_uses",
    "num_turns",
    "subtype",
    "stop_reason",
    "terminal_reason",
    "api_error_status",
    "exit_code",
    "cli_version",
    # How close this call came to its ceiling, and whether it went over and was rescued.
    # A run whose calls are all finishing at 0.95x is one configuration change away from
    # losing entire steps, and c4566ed2 gave no warning of that at all.
    "timeout_s",
    "near_timeout",
    "salvaged_after_timeout",
    "usage_estimated",
)
"""What a runner reported about a call that is worth keeping on the event.

Two of these are the ones a run is judged on. `permission_denials` because a denial is the
failure that used to be invisible — the June 2026 incident was fixed by looking at the
right flag, and this is where anybody can check that it stayed fixed. `web_searches`
because a grounded role that searched nothing is producing ungrounded ideas while claiming
otherwise; it is counted from the transcript, not from the provider's server-side counter,
which does not see a search the CLI ran locally. The rest are the diagnosis a failed call
needs. Deliberately an allowlist: the runner's own summary carries the whole transcript's
worth of detail, and an event payload is capped at 8KB."""


def _unit_note(loss: Mapping[str, Any]) -> str:
    """" (shard 1/2)", or nothing when the unit only restates the role it belongs to."""
    unit = str(loss.get("unit") or "").strip()
    role = str(loss.get("role") or "")
    if not unit or unit == role or unit.startswith(f"{role}:"):
        return ""
    return f" ({unit})"


class RunHalted(RuntimeError):
    """The run row went away (deleted, or the kill switch pulled). Stop immediately."""


class _Directive(StrEnum):
    CONTINUE = "continue"
    PAUSE = "pause"
    STOP = "stop"
    FINISH = "finish"


@dataclass(frozen=True, slots=True)
class _Unit:
    """One scheduled role call: what to send, and what it is about."""

    key: str
    prompt: str
    cfg: RoleConfig
    payload: Any = None
    validate: Callable[[dict[str, Any]], str | None] | None = None


@dataclass(frozen=True, slots=True)
class _Outcome:
    """What a unit produced once retries and validation are done."""

    unit: _Unit
    data: dict[str, Any] | None
    error: str | None
    telemetry: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.data is not None


async def run_engine(
    run_id: UUID,
    store: RunStore,
    runner: AgentRunner,
    **kwargs: Any,
) -> str:
    """Drive one run to a terminal lifecycle and return it. The supervisor's entry point."""
    return await Orchestrator(store, runner, run_id, **kwargs).run()


class Orchestrator:
    """Drives one run. Construct, `await run()`, discard."""

    def __init__(
        self,
        store: RunStore,
        runner: AgentRunner,
        run_id: UUID,
        *,
        projector: Callable[[str], None] | None = None,
        rate_limit_cooldown: float = RATE_LIMIT_COOLDOWN,
    ) -> None:
        self._store = store
        self._runner = runner
        self._run_id = run_id
        self._events = EventWriter(store, run_id)
        self._projector = projector
        self._rate_limit_cooldown = rate_limit_cooldown

        self._run: dict[str, Any] = {}
        self._config = RunConfig()
        self._goal = ""
        self._models: dict[str, tuple[str, str]] = {}
        self._system_prompts: dict[str, str] = {}
        self._context = ContextBlock()

        self._pause_requested = False
        self._stop_requested = False
        self._finish_requested = False
        self._budget_exhausted = False
        self._end_reason = "rounds_done"
        """Why the run ended, as opposed to what it ended as.

        `_drive` reaches `_finish("completed")` from three unrelated causes and used to
        record the same payload for all of them, so the run's own permanent record could
        not distinguish a run that did everything asked from one the budget cut short."""

        self._losses: list[dict[str, Any]] = []
        """Steps this run did not complete: `{round, role, unit, error}`, in order.

        Held in memory *and* on the run, because the overview prompt needs it in one piece
        and the report is the surface where a reader most needs to meet it."""

        self._deadline: float | None = None
        self._out_of_time = False
        self._ready = asyncio.Event()
        self._ready.set()
        self._background: set[asyncio.Task[None]] = set()

    # --- lifecycle --------------------------------------------------------------------

    async def run(self) -> str:
        """Run to completion. Returns the terminal lifecycle it left the run in."""
        self._reload()
        lifecycle = self._run["lifecycle"]
        if lifecycle in TERMINAL_LIFECYCLES:
            return lifecycle  # a re-launch of a finished run is a no-op, not an error

        self._prepare()
        try:
            return await self._drive()
        except RunHalted:
            log.warning("run %s halted: the run row is gone", self._run_id)
            raise
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as exc:  # noqa: BLE001 — the supervisor's last line of defence
            log.exception("run %s failed", self._run_id)
            self._fail(exc)
            raise
        finally:
            for task in list(self._background):
                task.cancel()

    async def _drive(self) -> str:
        self._clear_stale_control()
        self._set_lifecycle("running")
        self._start_clock()

        start = int(self._engine_state().get("last_completed_round", 0)) + 1
        for number in range(start, max(self._config.rounds, 0) + 1):
            directive = self._boundary()
            if directive is _Directive.PAUSE:
                return self._pause()
            if directive is _Directive.STOP:
                return await self._finish("stopped")

            await self._round(number)

            if self._pause_requested:
                return self._pause()
            if self._stop_requested:
                return await self._finish("stopped")
            if self._budget_exhausted or self._finish_requested:
                break

        return await self._finish("completed")

    def _prepare(self) -> None:
        """Freeze everything the run's config decides, before the first call is made."""
        config = dict(self._run.get("config") or {})
        model_table = config.get("model_table")
        if not model_table:
            raise ValueError(
                f"run {self._run_id} has no resolved model_table in its config; the launcher "
                "must resolve one at create time so a resumed run uses the models it started with"
            )
        self._config = RunConfig.from_mapping(config)
        self._models = {
            str(row["role"]): (str(row["model"]), str(row["effort"])) for row in model_table
        }
        self._goal = (self._run.get("prompt") or self._run.get("question") or "").strip()
        self._system_prompts = {
            role: render_system_prompt(role, grounding_depth=self._config.grounding_depth)
            for role in (
                "framing",
                "verification",
                "synthesis",
                "challenge",
                "generation",
                "reflection",
                "proximity",
                "ranking",
                "evolution",
                "meta_review",
                "overview",
                "cartographer",
            )
        }
        self._context = build_context_block(self._store.get_context_docs(self._run_id))
        self._record_context_delivery()
        self._losses = [dict(item) for item in self._engine_state().get("losses") or []]

    def _record_context_delivery(self) -> None:
        """Put what happened to the scientist's documents where the scientist can see it.

        `build_context_block` has always computed `included`/`truncated`/`omitted` and
        nothing has ever read them: the sole trace of a document that did not fit was one
        `log.info` line in `supervisor.log`. The product accepts five documents of 200,000
        characters and delivers 20,000 to any call, so a scientist could attach a megabyte,
        watch the Settings tab report its full character count back to them, and never learn
        that 96% of it reached no prompt in the run.
        """
        delivery = self._context.delivery()
        self._set_engine_state(
            context_delivery=delivery,
            context_char_cap=CONTEXT_CHAR_CAP,
        )
        if not self._context.lossy:
            return
        self._events.emit(
            EventType.CONTEXT_TRUNCATED,
            {
                "cap": CONTEXT_CHAR_CAP,
                "included": list(self._context.included),
                "truncated": list(self._context.truncated),
                "omitted": list(self._context.omitted),
            },
        )

    def _pause(self) -> str:
        """Park the run at a step boundary. No overview: a paused run is not over."""
        self._set_lifecycle("pausing")
        self._store.set_control(self._run_id, None)
        self._set_lifecycle("paused")
        return "paused"

    async def _finish(self, lifecycle: str) -> str:
        """Every ending goes through here, and every ending writes the report."""
        self._set_lifecycle("stopping" if lifecycle == "stopped" else "finishing")
        self._set_engine_state(ended_reason=self._end_reason)
        await self._write_overview()
        self._store.set_control(self._run_id, None)
        self._set_lifecycle(lifecycle)

        summary = self._store.snapshot(self._run_id)["run"]
        counts = summary["counts"]
        self._events.emit(
            EventType.RUN_FINISHED,
            {
                "lifecycle": lifecycle,
                # What it ended *as* and why it ended are different facts, and only the
                # first of them used to be recorded. Two runs whose payloads were
                # byte-identical had ended for entirely different reasons.
                "reason": self._end_reason,
                # From the event log, not from this process's in-memory list: a run that
                # was resumed loses everything the earlier supervisor recorded here, and
                # the run list, the header and `store._run_health` all read the log.
                "lost_steps": int(summary.get("lost_steps") or 0),
                "failed_calls": int(summary.get("failed_calls") or 0),
                "rounds_completed": int(self._engine_state().get("last_completed_round", 0)),
                "hypotheses": counts["active"] + counts["rejected"] + counts["archived"],
                "matches": counts["matches"],
                "calls_used": summary["calls_used"],
                "has_overview": summary["has_overview"],
            },
        )
        self._project("finish")
        return lifecycle

    def _fail(self, exc: BaseException) -> None:
        error = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        try:
            self._store.set_lifecycle(self._run_id, "failed", error=error)
            self._events.emit(EventType.LIFECYCLE_CHANGED, {"lifecycle": "failed"})
            self._events.emit(EventType.RUN_FAILED, {"error": error})
        except Exception:  # noqa: BLE001 — the run row may be gone; nothing left but the log
            log.exception("could not record the failure of run %s", self._run_id)

    # --- the round --------------------------------------------------------------------

    async def _round(self, number: int) -> None:
        if self._config.workflow == "adaptive":
            from app.engine.research import ResearchController

            await ResearchController(self).checkpoint(number)
            return
        notes = self._drain_interventions(number)
        self._store.set_round(self._run_id, number)
        self._events.emit(EventType.ROUND_STARTED, {"round": number}, round=number)

        guidance_round, guidance = self._latest_guidance()
        context = RoundContext(
            goal=self._goal,
            guidance=guidance,
            guidance_round=guidance_round,
            round=number,
            scientist_notes=notes,
            context=self._context,
            seed=Seed.from_mapping((self._run.get("graft_state") or {}).get("pending_seed")),
        )

        added = await self._generate(number, context)
        if self._interrupted():
            return
        # Reflection runs before proximity, deliberately, and the cost of that order is
        # real: a hypothesis proximity is about to archive as a duplicate has already been
        # given a full grounded critique. Two of run c4566ed2's calls went that way, ~$0.74
        # of $8.87 of reflection spend. The order stands because the reviews of the losers
        # are what the meta-review reads to see *what kind* of idea the round produced —
        # clustering first would leave it looking only at champions — and because a
        # duplicate's critique is still stored against the row a scientist may un-archive.
        # `_reflect`'s `duplicate_of is None` clause guards the cross-round case only.
        reviewed = await self._reflect(number, context)
        if self._interrupted():
            return
        await self._cluster(number)
        if self._interrupted():
            return
        await self._collapse_check(number)
        if self._interrupted():
            return
        completed = await self._tournament(number)
        if self._interrupted():
            return
        added += await self._evolve(number, context)
        if self._interrupted():
            return
        # No interruption check after the meta-review: its guidance is recorded, so the
        # round *is* complete. Closing it here means a pause pressed during the last call
        # of a round resumes at the next round rather than replaying a finished one.
        await self._meta_review(number, notes)

        self._events.emit(
            EventType.ROUND_COMPLETED,
            {
                "round": number,
                "hypotheses_added": added,
                "reviews": reviewed,
                "matches_completed": completed,
            },
            round=number,
        )
        self._set_engine_state(last_completed_round=number)
        self._project("round")

    def _drain_interventions(self, number: int) -> tuple[str, ...]:
        """Apply the scientist's queued notes and archivals, in sequence, at the boundary.

        Notes are persisted against the round before they are used, so a run killed after
        claiming them does not lose the guidance it already took off the queue.
        """
        claimed = self._store.claim_interventions(self._run_id)
        fresh = [
            str(item.get("text") or "").strip()
            for item in claimed
            if item.get("kind") == "note" and str(item.get("text") or "").strip()
        ]
        state = self._engine_state()
        by_round = dict(state.get("round_notes") or {})
        claimed_here = [*by_round.get(str(number), []), *fresh]
        if fresh:
            by_round[str(number)] = claimed_here
            self._set_engine_state(round_notes=by_round)
        # Every note the scientist has given this run so far, not only the ones this round
        # claimed. A note is rendered as "SCIENTIST GUIDANCE … outranks everything else in
        # this prompt" and was then dropped the moment the round that claimed it ended, so
        # a standing instruction — "never propose X" — was obeyed once and silently
        # forgotten. Nothing in the loop carries it forward on the scientist's behalf.
        notes = [
            note
            for key in sorted(by_round, key=lambda item: int(item))
            if int(key) < number
            for note in by_round[key]
        ] + claimed_here

        for item in claimed:
            if item.get("kind") == "note":
                self._events.emit(
                    EventType.NOTE_ADDED, {"text": item.get("text", "")}, round=number
                )
            elif item.get("kind") == "hypothesis_archived":
                self._events.emit(
                    EventType.HYPOTHESIS_ARCHIVED, {"hid": item.get("hid")}, round=number
                )
        return tuple(notes)

    # --- step 1: generation ------------------------------------------------------------

    async def _generate(self, number: int, context: RoundContext) -> int:
        """Sharded generation: ceil(batch/3) calls, three angles, one shared seed.

        Sharding is not only about wall-clock. One call asked for eight hypotheses returns
        eight variations on whichever idea it thought of first; three calls with different
        directives return three genuinely different starting points.

        A pending divergence seed goes to *every* shard of the round. The seed exists
        because the pool collapsed — seeding one shard in three would leave two thirds of
        the batch in the rut it was raised to escape.
        """
        batch = max(0, self._config.generation_batch)
        if batch == 0:
            return 0
        shards = self._shard_sizes(batch)
        done = set(self._round_mark(number, "generation_shards", []))
        existing = self._existing_rows()
        seed_id = context.seed.seed_id if context.seed else None

        units = [
            _Unit(
                key=f"generation:{number}:{index}",
                prompt=generation_prompt(
                    context,
                    count=size,
                    # Rotated by round, and claimed in an order that is not the order the
                    # directives are written in. Rotation alone still gave round 1 the two
                    # scale settings and withheld the adversarial reframe from the one
                    # round that sets the run's ceiling, at every batch size below 7 —
                    # which is 25 of 29 real runs. See `prompts.DIRECTIVE_ORDER`.
                    directive=exploration_directive(index, number),
                    existing=existing,
                ),
                cfg=self._role_cfg(
                    "generation",
                    round=number,
                    unit=f"shard {index + 1}/{len(shards)}",
                    effort="high" if number == 1 else None,
                ),
                payload=index,
            )
            for index, size in enumerate(shards)
            if index not in done
        ]
        if not units:
            # The seed is cleared here as well as at the bottom, because this early return
            # is reachable on a re-entry into a round whose generation already finished —
            # a pause, a stop-and-continue, or a kill after the post-generation
            # `_interrupted()` check. `_round` rebuilds the context from `pending_seed`
            # every time, so a seed left standing here is consumed by a second generation
            # wave and `seed_id` stops naming one wave.
            self._clear_seed(context, done, shards)
            return 0

        requested = sum(shards[unit.payload] for unit in units)
        added = 0
        failed: list[str] = []
        for outcome in await self._run_calls(units, round=number):
            if not outcome.ok:
                failed.append(outcome.unit.cfg.unit or outcome.unit.key)
                continue
            for item in outcome.data.get("hypotheses", []):
                added += 1
                self._add_hypothesis(item, number, seed_id=seed_id)
            done.add(outcome.unit.payload)
            self._mark_round(number, generation_shards=sorted(done))

        if added < requested:
            # The shortfall is a fact about the *round*, and nothing used to state it.
            # `added` was read in exactly one place — a number in the round_completed
            # payload — and never compared with what was asked for, so a wave in which
            # every shard died was indistinguishable from one that was never scheduled.
            self._events.emit(
                EventType.STEP_FAILED,
                {
                    "role": "generation",
                    "round": number,
                    "requested": requested,
                    "produced": added,
                    "units": failed,
                },
                round=number,
            )

        self._clear_seed(context, done, shards)
        return added

    def _clear_seed(self, context: RoundContext, done: set[int], shards: Sequence[int]) -> None:
        if context.seed is not None and len(done) == len(shards):
            self._store.set_graft_state(self._run_id, {"pending_seed": None})
            self._reload()

    def _existing_rows(self) -> list[dict[str, Any]]:
        """What generation and evolution are told is already here, bounded but not by age.

        The bound used to be a pure recency tail, which drops the run's strongest ideas
        first: on a long run the round-1 hypotheses still leading the tournament are the
        first titles generation stops being warned about, while archived duplicates from
        last round are kept. Active rows are kept by rating and the retired ones by
        recency, so the list a generator most needs — what is currently winning — survives
        the trim.
        """
        rows = self._store.list_hypotheses(self._run_id)
        if len(rows) <= MAX_EXISTING_TITLES:
            return rows
        live = sorted(
            (row for row in rows if row["status"] == "active"),
            key=lambda row: (-float(row["elo"]), row["hid"]),
        )[:MAX_EXISTING_TITLES]
        room = MAX_EXISTING_TITLES - len(live)
        gone = [row for row in rows if row["status"] != "active"][-room:] if room > 0 else []
        keep = {row["hid"] for row in (*live, *gone)}
        return [row for row in rows if row["hid"] in keep]

    def _shard_sizes(self, batch: int) -> list[int]:
        """Split the batch into ceil(batch/3) shards as evenly as the count allows."""
        count = math.ceil(batch / GENERATION_SHARD_SIZE)
        base, extra = divmod(batch, count)
        return [base + (1 if index < extra else 0) for index in range(count)]

    def _add_hypothesis(
        self,
        item: Mapping[str, Any],
        number: int,
        *,
        seed_id: str | None = None,
        parent_ids: Sequence[str] = (),
        operator: str | None = None,
    ) -> dict[str, Any]:
        fields = hypothesis_fields(item)
        row = self._store.add_hypothesis(
            self._run_id,
            title=fields["title"] or "untitled",
            body_md=compose_hypothesis_md(fields),
            created_round=number,
            parent_ids=list(parent_ids),
            operator=operator,
            seed_id=seed_id,
        )
        self._events.emit(
            EventType.HYPOTHESIS_ADDED,
            {
                "hid": row["hid"],
                "title": row["title"],
                "round": number,
                "source": row["source"],
                "operator": operator,
                "parent_ids": list(parent_ids),
                "seed_id": seed_id,
            },
            round=number,
        )
        return row

    # --- step 2: reflection ------------------------------------------------------------

    async def _reflect(self, number: int, context: RoundContext) -> int:
        """Review every hypothesis nobody has reviewed yet — this round's and last round's
        evolved variants alike. The resume unit is the *missing review*, so a killed run
        never pays twice for the same critique.

        The round's `context` reaches this step for two things the reviewer used to be
        denied. The **scientist's notes**, because this is the one role with the power to
        delete a hypothesis and the notes are rendered as outranking everything else in
        every other prompt — a mid-run constraint steered what got produced and not what
        got kept, so the gate went on passing ideas the scientist had just ruled out. And
        each hypothesis' **parents**, because `novelty` is what this role decides and a
        variant restating the idea it was bred from is the one kind of non-novelty this
        loop manufactures for itself.
        """
        pending = [
            row
            for row in self._store.list_hypotheses_without_review(self._run_id)
            # `duplicate_of` is the second clause and it is not redundant: a resumed round
            # would otherwise pay for a full grounded, high-effort, web-searching critique
            # of an idea a previous round's proximity step has already retired. Reflection
            # is the most expensive call in the loop.
            if row["status"] == "active" and row["duplicate_of"] is None
        ]
        if not pending:
            return 0

        by_hid = {row["hid"]: row for row in self._store.list_hypotheses(self._run_id)}
        units = [
            _Unit(
                key=f"reflection:{row['hid']}",
                prompt=reflection_prompt(
                    self._goal,
                    row,
                    self._context,
                    parents=[
                        by_hid[hid] for hid in (row.get("parent_ids") or []) if hid in by_hid
                    ],
                    scientist_notes=context.scientist_notes,
                ),
                cfg=self._role_cfg("reflection", round=number, unit=row["hid"]),
                payload=row,
            )
            for row in pending
        ]

        reviewed: list[str] = list(self._round_mark(number, "reviewed", []))
        count = 0

        def record(outcome: _Outcome) -> None:
            nonlocal count
            if not outcome.ok:
                return
            row = outcome.unit.payload
            novelty = outcome.data.get("novelty") or {}
            # Unwrapped on the way in. The schema now refuses a JSON-encoded critique and
            # earns a repair re-ask, but a model that answers `{"summary": "…"}` twice
            # would otherwise still put unreadable text in the field the scientist reads
            # first — and in every ranking prompt, and in the meta-review.
            self._store.record_review(
                UUID(row["id"]),
                verdict=outcome.data["verdict"],
                novelty_level=novelty.get("level"),
                novelty_note=unwrap_prose(novelty.get("note")),
                correctness=unwrap_prose(outcome.data.get("correctness")),
                testability=unwrap_prose(outcome.data.get("testability")),
                key_risk=unwrap_prose(outcome.data.get("key_risk")),
                note=unwrap_prose(outcome.data.get("note")),
                model=outcome.unit.cfg.model,
                retire=self._config.workflow != "adaptive",
            )
            count += 1
            reviewed.append(row["hid"])
            self._mark_round(number, reviewed=reviewed)
            self._events.emit(
                EventType.REVIEW_RECORDED,
                {
                    "hid": row["hid"],
                    "verdict": outcome.data["verdict"],
                    "novelty_level": novelty.get("level"),
                },
                round=number,
            )

        await self._run_calls(units, round=number, on_result=record)
        return count

    # --- step 3: proximity -------------------------------------------------------------

    async def _cluster(self, number: int) -> None:
        """Label the pool by idea, then retire the duplicates a label reveals.

        Non-champions keep their row and gain a `duplicate_of` pointer, so a proximity call
        that over-merged can be undone. The archived engine deleted them.

        **The resume unit is the pool, not the round.** It used to be a boolean, which is
        the only step mark in the loop that does not name what it covers, and the mismatch
        is not theoretical: run c4566ed2's round 2 ran twice. The first pass lost both
        generation shards, clustered the six hypotheses that existed, and wrote
        `proximity: true`. The second pass generated h007–h012 and reviewed all six — and
        then walked straight past this step and the tournament, because the round was
        marked. Six of twelve active rows entered round 3 with `cluster = NULL`, the round's
        collapse snapshot was computed over a pool that was half unlabelled (`hhi 0.042` =
        6·(1/12)²), and nothing said so. The mark now carries the hids it covers and the
        step re-runs when the active pool has grown past them, the same shape as
        `generation_shards` and `reviewed`.
        """
        active = self._store.list_hypotheses(self._run_id, statuses=["active"])
        if self._round_mark(number, "proximity", False):
            covered = set(self._round_mark(number, "proximity_hids", []))
            if not covered or not {row["hid"] for row in active} - covered:
                return
            log.info(
                "run %s re-running round %d proximity: %d active hypotheses the round's "
                "clustering never saw",
                self._run_id,
                number,
                len({row["hid"] for row in active} - covered),
            )
        if len(active) < 2:
            self._mark_round(
                number,
                proximity=True,
                proximity_hids=[row["hid"] for row in active],
                proximity_failed=False,
            )
            return

        unit = _Unit(
            key=f"proximity:{number}",
            prompt=proximity_prompt(self._goal, active),
            cfg=self._role_cfg("proximity", round=number),
        )
        outcomes = await self._run_calls([unit], round=number)
        if not outcomes or not outcomes[0].ok:
            # Marked as attempted *and failed*. Every carried-over hypothesis keeps last
            # round's label, so the pool is unlabelled-but-looks-labelled — and the
            # collapse detector, which runs next, would read a bit-identical cluster set
            # as a plateau with a zero birth rate. That is two of its votes, and its
            # quorum is two.
            self._mark_round(
                number,
                proximity=True,
                proximity_failed=True,
                proximity_hids=[row["hid"] for row in active],
            )
            return

        known = {row["hid"]: row for row in active}
        clusters = {
            hid: str(label)
            for hid, label in (outcomes[0].data.get("clusters") or {}).items()
            if hid in known
        }
        # The coarse reading, recorded and nothing more. It is kept in engine_state rather
        # than on the hypothesis row because it is a measurement on trial: the family
        # taxonomy that separated the collapsed run from the healthy one came from the
        # user's own prompt, not from this agent, so what this agent actually emits has to
        # be watched before a column, an index or a decision is built on it. Labels are
        # merged, not replaced — proximity only ever sees the *active* pool, and an idea
        # archived as a duplicate this round was still an idea this round produced.
        families = {
            hid: str(label)
            for hid, label in (outcomes[0].data.get("families") or {}).items()
            if hid in known and str(label).strip()
        }
        if families:
            self._set_engine_state(
                family_labels={**(self._engine_state().get("family_labels") or {}), **families}
            )
        duplicates = self._duplicates(clusters, known)
        applied = self._store.apply_clusters(self._run_id, clusters, duplicates)
        self._events.emit(
            EventType.CLUSTER_APPLIED,
            {
                "round": number,
                "n_clusters": len(set(clusters.values())),
                "labelled": applied["labelled"],
                # `active` beside `labelled`, because a map covering half the pool used to
                # look identical to a complete one in the event stream, and the ids the
                # agent omitted silently kept whatever label a previous round gave them.
                "active": len(known),
                "duplicates": applied["duplicates"],
            },
            round=number,
        )
        missing = sorted(set(known) - set(clusters))
        if missing:
            self._events.emit(
                EventType.STEP_FAILED,
                {
                    "role": "proximity",
                    "round": number,
                    "requested": len(known),
                    "produced": len(clusters),
                    "units": missing[:20],
                },
                round=number,
            )
        self._mark_round(
            number,
            proximity=True,
            proximity_failed=False,
            proximity_hids=sorted(known),
            # What this call actually said, kept whole. `apply_clusters` archives every
            # non-champion straight after, so by the time anything else looks at the pool
            # each surviving label has exactly one member and the concentration of the
            # ideas the round produced is no longer readable off the rows. See
            # `_collapse_check`.
            proximity_clusters=dict(clusters),
            # A round re-clustered after a resume needs its collapse reading re-taken: the
            # one already recorded was computed over the half-pool this call has just
            # replaced.
            graft=False,
        )

    @staticmethod
    def _duplicates(
        clusters: Mapping[str, str], known: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, str]:
        """Map each non-champion onto its cluster's champion.

        Judged members first, then Elo, then hid — because clustering runs *before* the
        tournament, so at this moment Elo is not a measurement. In round 1 every
        hypothesis sits at exactly 1200 with no matches and the key collapsed to the
        lowest hid, meaning the survivor was whichever shard emitted it first. In later
        rounds it was worse than arbitrary: a newcomer entering at 1200 outranked a
        veteran that had actually lost a judged match at 1184, so the engine archived the
        tested idea and kept the untested one. Run 526ee9aa did exactly that — h001
        (1184.0, 1 match) was archived in favour of h015, which the tournament went on to
        rate 1168.1.
        """
        members: dict[str, list[str]] = {}
        for hid, label in clusters.items():
            members.setdefault(label, []).append(hid)

        def rank(hid: str) -> tuple[int, float, str]:
            row = known[hid]
            return (
                0 if int(row.get("matches") or 0) > 0 else 1,
                -float(row["elo"]),
                hid,
            )

        duplicates: dict[str, str] = {}
        for hids in members.values():
            if len(hids) < 2:
                continue
            champion = min(hids, key=rank)
            duplicates.update({hid: champion for hid in hids if hid != champion})
        return duplicates

    # --- step 4: collapse check --------------------------------------------------------

    async def _collapse_check(self, number: int) -> None:
        """Measure the pool's diversity every round; call the Cartographer only if it fires.

        The measurement is not gated on `graft.enabled` and must not become gated on it
        again. The graft ships disabled, `enabled` was the first line of this method, and
        the consequence was that no real run in the app's history has a single
        `graft_events` row or one entry of `collapse_history` — the diversity of the idea
        pool, the thing the whole organ exists to watch, was measured only by runs that had
        already turned the intervention on. Every signal here is arithmetic over rows that
        are already loaded, so measuring always costs nothing; `enabled` now gates exactly
        one thing, the Cartographer call, which is the only part that spends a model call.
        """
        graft = self._config.graft
        if self._round_mark(number, "graft", False):
            return

        if self._round_mark(number, "proximity_failed", False):
            # A round whose clustering did not happen has no cluster measurement. Reusing
            # last round's would manufacture both of the signals the detector fires on, so
            # the snapshot is not appended and the decision abstains with a reason.
            self._store.record_graft(
                self._run_id, number, fired=False, abstained_reason=ABSTAIN_STALE_CLUSTERS
            )
            self._events.emit(
                EventType.GRAFT_ABSTAINED,
                {
                    "round": number,
                    "reason": ABSTAIN_STALE_CLUSTERS,
                    "votes": 0,
                    "n_clusters": None,
                    "hhi": None,
                },
                round=number,
            )
            self._mark_round(number, graft=True)
            return

        active = self._store.list_hypotheses(self._run_id, statuses=["active"])
        rows = self._measured_pool(number, active)
        state = self._engine_state()
        history = [
            CollapseSnapshot.from_dict(item)
            for item in state.get("collapse_history") or []
            if int(item.get("round") or 0) != number
        ]
        signals = collapse_signals(number, rows, history, graft)
        # Keyed by round rather than appended blindly: a round re-clustered after a resume
        # takes its reading again, and two snapshots for one round would both distort the
        # plateau window and make the history disagree with itself.
        self._set_engine_state(
            collapse_history=sorted(
                [
                    *(
                        item
                        for item in state.get("collapse_history") or []
                        if int(item.get("round") or 0) != number
                    ),
                    signals.snapshot.to_dict(),
                ],
                key=lambda item: int(item.get("round") or 0),
            )
        )
        recorded = {**signals.to_dict(), "family_flow": self._family_flow(number).to_dict()}

        graft_state = dict(self._run.get("graft_state") or {})
        decision = should_fire_graft(
            signals, graft, number, graft_state.get("last_fired_round")
        )

        if not decision.fire:
            self._store.record_graft(
                self._run_id,
                number,
                fired=False,
                n_clusters=decision.n_clusters,
                hhi=decision.hhi,
                signals=recorded,
                votes=decision.votes,
                abstained_reason=decision.abstained_reason,
            )
            # A quorum that was not met is the healthy case — the pool is still diverse —
            # and a disabled organ is not news either. Only the two diagnoses a scientist
            # would want to see reach the event stream.
            if decision.abstained_reason in (
                ABSTAIN_NO_CLUSTERS,
                ABSTAIN_PARTIAL_CLUSTERS,
                ABSTAIN_COOLDOWN,
            ):
                self._events.emit(
                    EventType.GRAFT_ABSTAINED,
                    {
                        "round": number,
                        "reason": decision.abstained_reason,
                        "votes": decision.votes,
                        "n_clusters": decision.n_clusters,
                        "hhi": decision.hhi,
                    },
                    round=number,
                )
            self._mark_round(number, graft=True)
            return

        champions = self._champions(active)
        unit = _Unit(
            key=f"cartographer:{number}",
            prompt=cartographer_prompt(self._goal, champions),
            cfg=self._role_cfg("cartographer", round=number),
        )
        outcomes = await self._run_calls([unit], round=number)
        if not outcomes or not outcomes[0].ok:
            self._store.record_graft(
                self._run_id,
                number,
                fired=False,
                n_clusters=decision.n_clusters,
                hhi=decision.hhi,
                signals=recorded,
                votes=decision.votes,
                abstained_reason="cartographer_failed",
            )
            self._mark_round(number, graft=True)
            return

        data = outcomes[0].data
        seed_id = f"seed-r{number}"
        self._store.record_graft(
            self._run_id,
            number,
            fired=True,
            n_clusters=decision.n_clusters,
            hhi=decision.hhi,
            signals=recorded,
            votes=decision.votes,
            source_domain=data["source_domain"],
            skeleton=data["skeleton"],
            seed_framing=data["seed_framing"],
            seed_id=seed_id,
        )
        self._events.emit(
            EventType.GRAFT_FIRED,
            {
                "round": number,
                "votes": decision.votes,
                "n_clusters": decision.n_clusters,
                "hhi": decision.hhi,
                "source_domain": data["source_domain"],
                "seed_id": seed_id,
            },
            round=number,
        )
        self._mark_round(number, graft=True)
        self._reload()

    def _measured_pool(
        self, number: int, active: Sequence[Mapping[str, Any]]
    ) -> list[HypRow]:
        """The population the collapse votes are read off: what proximity said, pre-dedup.

        This is the correction that makes the detector able to fire at all. The check runs
        *after* `apply_clusters` has archived every non-champion, which guarantees one
        member per surviving label — so with `n` survivors each in their own cluster,
        `herfindahl` is `n·(1/n)² = 1/n` and the `hhi ≥ 0.5` vote needs `n ≤ 2`, while
        `n_clusters == n_active` makes the zero-slack plateau vote need a pool that does
        not grow at all. Quorum is two of three: on a *healthy* proximity call the detector
        could not reach it by arithmetic. Run c4566ed2 round 3 recorded
        `n_active 19, n_clusters 19, hhi 0.053` — and 0.053 is 1/19 exactly, which is the
        signature of the artefact rather than a reading of the pool.

        `_pool_concentration` already documents this dedup artefact and counts the retired
        rows for evolution's benefit; `collapse_signals` was never given the same treatment.
        The label map proximity returned for this round is recorded by `_cluster`, so the
        measurement is over the ideas the round actually held, before dedup removed the
        evidence of any concentration in them. Rounds recorded before that map existed —
        and rounds where proximity did not run — fall back to the active pool.
        """
        recorded = self._round_mark(number, "proximity_clusters", None)
        if not recorded:
            return [
                HypRow(
                    hid=row["hid"],
                    elo=float(row["elo"]),
                    matches=row["matches"],
                    wins=row["wins"],
                    cluster=row["cluster"],
                )
                for row in active
            ]
        seen = self._round_mark(number, "proximity_hids", []) or list(recorded)
        return [HypRow(hid=hid, cluster=recorded.get(hid)) for hid in seen]

    def _family_flow(self, number: int) -> TopShare:
        """Top-family share over the ideas this round *created* — the flow, not the stock.

        The stock reading is the one that failed. Replayed over every non-demo run, the
        cumulative-pool measures never fire: on `319f3128`, the one run with documented
        collapse, family HHI over the whole pool peaks at 0.137 while the round-by-round
        flow runs .05, .25, .58, .55, .25, .50, 1.00 — because a run collapses by producing
        the same kind of idea *next*, not by having produced them before.

        Every hypothesis born this round counts, whatever became of it: one rejected in
        review or archived as a duplicate is still evidence of what the generator reached
        for. The collapse check sits between proximity and the tournament, so what this sees
        is the round's *generated* batch — the round's evolved variants are not born until
        two steps later and would in any case be unlabelled, since proximity has already
        run. That matches what the audit measured. This is recorded and read; nothing acts
        on it.
        """
        labels = self._engine_state().get("family_labels") or {}
        born = [
            row["hid"]
            for row in self._store.list_hypotheses(self._run_id)
            if int(row.get("created_round") or 0) == number
        ]
        return top_share([labels.get(hid) for hid in born])

    @staticmethod
    def _champions(active: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        best: dict[str, dict[str, Any]] = {}
        for row in sorted(active, key=lambda item: (-float(item["elo"]), item["hid"])):
            best.setdefault(row["cluster"] or row["hid"], dict(row))
        return list(best.values())

    # --- step 5: the tournament --------------------------------------------------------

    async def _tournament(self, number: int) -> int:
        """Plan the round's matches, persist the plan, then judge what is still planned.

        The plan is written before a single match runs. A resumed round replays it rather
        than re-pairing from moved standings — which would both change the tournament and
        re-run matches already paid for.

        A resumed round that *generated more hypotheses* is the case the replay used to get
        wrong. The plan was written against the pool of the abandoned pass, so the rows the
        second pass added were absent from it and the step returned 0 without a word: in run
        c4566ed2's round 2, h007–h012 were generated and reviewed on the second pass and
        then judged nothing at all, and `round_completed` recorded `matches_completed: 0`
        against a round the ladder reports as having played four. The existing plan is
        honoured unchanged and *extended* for the newcomers, which is the same promise —
        never re-pair, never re-run — applied per unit rather than per round.
        """
        planned = self._store.list_matches(self._run_id, round=number)
        planned = self._extend_plan(number, planned)
        if not planned:
            return 0

        pending = [row for row in planned if row["status"] == "planned"]
        if not pending:
            return 0

        rows = {row["hid"]: row for row in self._store.list_hypotheses(self._run_id)}
        reviews = self._reviews_by_hid()
        meetings = self._prior_meetings(number)
        leaders = [
            row["hid"]
            for row in sorted(
                (row for row in rows.values() if row["status"] == "active"),
                key=lambda row: (-float(row["elo"]), row["hid"]),
            )[:TOP_TIER]
        ]
        final_round = number >= self._config.rounds

        units: list[_Unit] = []
        for match in pending:
            hid_a, hid_b = match["hid_a"], match["hid_b"]
            if hid_a not in rows or hid_b not in rows:
                continue
            pair = Pair(
                hid_a=hid_a,
                hid_b=hid_b,
                swapped=presentation_swap(
                    hid_a, hid_b, meetings.get(meeting_key(hid_a, hid_b), 0)
                ),
            )
            first, second = presentation_order(pair)
            # C1: the judge gets more thinking where the result matters most — a match
            # between two leaders, or any match in the round that decides the ranking.
            escalate = final_round or (hid_a in leaders and hid_b in leaders)
            units.append(
                _Unit(
                    key=f"ranking:{match['id']}",
                    prompt=ranking_prompt(
                        self._goal, rows[first], rows[second], reviews=reviews
                    ),
                    cfg=self._role_cfg(
                        "ranking",
                        round=number,
                        unit=f"{first} vs {second}",
                        effort="high" if escalate else None,
                    ),
                    payload=(match, pair),
                )
            )
        if not units:
            return 0

        completed = 0

        def record(outcome: _Outcome) -> None:
            nonlocal completed
            match, pair = outcome.unit.payload
            if not outcome.ok:
                # A match nobody could judge is recorded as skipped: no Elo moves, and the
                # unit is closed so a resume does not pay for it again.
                self._store.record_match(
                    UUID(match["id"]), winner=None, judge_model=outcome.unit.cfg.model
                )
                return
            side = winner_side(pair, int(outcome.data["winner"]))
            settled = self._store.record_match(
                UUID(match["id"]),
                winner=1 if side == "a" else 2,
                debate_md=outcome.data.get("debate"),
                judge_model=outcome.unit.cfg.model,
                tokens_in=0,
                tokens_out=0,
            )
            completed += 1
            self._events.emit(
                EventType.MATCH_COMPLETED,
                {
                    "match_id": settled["id"],
                    "round": number,
                    "hid_a": settled["hid_a"],
                    "hid_b": settled["hid_b"],
                    "winner": settled["winner"],
                    "elo_a_before": settled["elo_a_before"],
                    "elo_a_after": settled["elo_a_after"],
                    "elo_b_before": settled["elo_b_before"],
                    "elo_b_after": settled["elo_b_after"],
                    "k": settled["k"],
                },
                round=number,
            )

        await self._run_calls(units, round=number, on_result=record)
        return completed

    def _extend_plan(
        self, number: int, planned: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        """The round's plan, written if absent and extended if the pool outgrew it.

        Extension is deliberately narrow, and the narrowness is the whole safety argument.
        The mark `tournament_pool` records the active pool the plan was drawn from, so the
        step can tell the two re-entries apart: one *inside* the same pass, where every
        hypothesis was already available to `make_pairs` and `matches_per_round` has already
        had its say, and one after a second generation wave, where hypotheses exist that the
        plan could not have known about. Only the second extends, and it extends only for
        those hypotheses, under the round's own `matches_per_round` ceiling again — each
        wave of ideas gets one tournament, which is what per-unit resume means here.

        A round planned before this mark existed is left alone: without knowing what the
        plan was drawn from, "not in the plan" cannot be told from "not selected".
        """
        active = self._store.list_hypotheses(self._run_id, statuses=["active"])
        standings = [
            HypRow(
                hid=row["hid"],
                elo=float(row["elo"]),
                matches=row["matches"],
                wins=row["wins"],
                cluster=row["cluster"],
            )
            for row in active
        ]
        rng = random.Random(f"{self._run['engine_run_id']}:{number}")
        if not planned:
            pairs = make_pairs(
                standings,
                self._config.matches_per_round,
                rng,
                prior_meetings=self._prior_meetings(number),
            )
            if not pairs:
                return []
            self._mark_round(number, tournament_pool=[row["hid"] for row in active])
            return self._store.plan_matches(
                self._run_id, number, [(pair.hid_a, pair.hid_b) for pair in pairs]
            )

        drawn_from = set(self._round_mark(number, "tournament_pool", []))
        if not drawn_from:
            return list(planned)
        newcomers = [row for row in standings if row.hid not in drawn_from]
        if not newcomers:
            return list(planned)

        scheduled = {hid for row in planned for hid in (row["hid_a"], row["hid_b"])}
        extra = make_pairs(
            newcomers,
            self._config.matches_per_round,
            rng,
            prior_meetings=self._prior_meetings(number),
        )
        additions = [
            (pair.hid_a, pair.hid_b)
            for pair in extra
            if pair.hid_a not in scheduled and pair.hid_b not in scheduled
        ]
        if not additions:
            return list(planned)
        log.info(
            "run %s extending the round %d match plan by %d: %d active hypotheses were "
            "created after the round's plan was written",
            self._run_id,
            number,
            len(additions),
            len(newcomers),
        )
        self._mark_round(number, tournament_pool=[row["hid"] for row in active])
        return self._store.plan_matches(self._run_id, number, additions, extend=True)

    def _prior_meetings(self, before_round: int) -> Counter[tuple[str, str]]:
        """How often each pair has already met, counting completed rounds only.

        Excluding the current round keeps the presentation order stable across a resume:
        the same planned match is shown in the same order whether or not its neighbours in
        the round have already been judged.
        """
        meetings: Counter[tuple[str, str]] = Counter()
        for match in self._store.list_matches(self._run_id):
            if match["round"] < before_round and match["status"] == "completed":
                meetings[meeting_key(match["hid_a"], match["hid_b"])] += 1
        return meetings

    # --- step 6: evolution -------------------------------------------------------------

    @staticmethod
    def _rank_key(row: Mapping[str, Any]) -> tuple[int, float, str]:
        """Standings order: hypotheses that played first, then rating, then hid.

        The same key the leaderboard and the overview's ranked/unranked split already use,
        and for the same reason: a hypothesis that never played sits at the untouched 1200
        default and outranks every hypothesis that competed and lost. Evolution sorted on
        raw Elo, so at run c4566ed2's round-3 evolution h017–h021 (1200, no matches) sat
        above six hypotheses with judged results — which governs both the head slice and
        the order the outsider search scans.
        """
        return (
            0 if int(row.get("matches") or 0) > 0 else 1,
            -float(row.get("elo") or 0.0),
            str(row.get("hid") or ""),
        )

    @staticmethod
    def _parents(ranked: Sequence[Mapping[str, Any]], top_k: int) -> list[dict[str, Any]]:
        """Top `k-1` by Elo, plus the best hypothesis from a cluster none of them occupy.

        Raw top-k is a positive feedback loop: the tournament promotes whatever the pool is
        already full of, evolution breeds from exactly those, and their offspring enter the
        next tournament. Reserving the last slot for the leading *outsider* costs one place
        in the ranking and is the only structural pressure in the loop pointing the other
        way.

        It is a floor rather than a lever, and honestly so. When proximity has just run,
        every surviving hypothesis holds a distinct label — it archives the non-champions of
        a shared one — so top-k already spans k clusters and this changes nothing. It bites
        in the cases where that invariant does not hold: a proximity call that failed and
        left last round's labels standing, or one that returned a map covering only part of
        the pool, both of which the run health record shows happening. An unlabelled row
        counts as its own cluster, so an unlabelled pool is raw top-k unchanged.
        """
        if top_k <= 0 or not ranked:
            return []
        if top_k == 1 or len(ranked) <= top_k:
            return [dict(row) for row in ranked[:top_k]]

        head = list(ranked[: top_k - 1])
        represented = {(row.get("cluster") or row["hid"]) for row in head}
        outsider = next(
            (
                row
                for row in ranked[top_k - 1 :]
                if (row.get("cluster") or row["hid"]) not in represented
            ),
            None,
        )
        chosen = [*head, outsider if outsider is not None else ranked[top_k - 1]]
        return [dict(row) for row in chosen]

    @staticmethod
    def _pool_concentration(produced: Sequence[Mapping[str, Any]]) -> float | None:
        """The dominant cluster's share of everything the run has produced, or `None`.

        Measured over **every** hypothesis carrying a label, whatever its status, and that
        is the whole point. Proximity archives the non-champions of a shared label, so the
        surviving pool has at most one member per cluster by construction and a concentration
        read off it can never exceed `1/k` — which is exactly why the collapse detector's
        Herfindahl over the active pool never fires on a real run, peaking at 0.137 on the
        one run with documented collapse. Dedupe is what hides the narrowing; counting what
        it retired is what reveals it.

        The denominator is every hypothesis, labelled or not — the convention `herfindahl`
        already uses. Dividing by the labelled ones instead would call a pool where proximity
        managed to label two ideas out of twelve fully concentrated, and demand a divergent
        variant on the strength of two labels.
        """
        if not produced:
            return None
        share = top_share([row.get("cluster") for row in produced])
        if not share.counts:
            return None
        largest = round(share.counts[0][1] / len(produced), 3)
        return largest if largest >= CONCENTRATED_POOL_SHARE else None

    async def _evolve(self, number: int, context: RoundContext) -> int:
        if self._round_mark(number, "evolution", False):
            return 0
        top_k = max(0, self._config.evolve_top_k)
        if top_k == 0:
            self._mark_round(number, evolution=True)
            return 0

        ranked = sorted(
            self._store.list_hypotheses(self._run_id, statuses=["active"]),
            key=self._rank_key,
        )
        top = self._parents(ranked, top_k)
        if not top:
            self._mark_round(number, evolution=True)
            return 0

        # Which parent, if any, `_parents` swapped in from outside the leaders' clusters.
        # The block is headed "TOP HYPOTHESES" and every row rendered identically, so the
        # loop's one structural anti-convergence move arrived as an unexplained lower-Elo
        # entry in a list labelled "top".
        swapped = top_k > 1 and len(ranked) > top_k and top[-1]["hid"] != ranked[top_k - 1]["hid"]
        outsider = top[-1]["hid"] if swapped else None
        unit = _Unit(
            key=f"evolution:{number}",
            prompt=evolution_prompt(
                context,
                count=top_k,
                top=top,
                reviews=self._reviews_by_hid(),
                concentration=self._pool_concentration(
                    self._store.list_hypotheses(self._run_id)
                ),
                outsider=outsider,
                existing=self._existing_rows(),
            ),
            cfg=self._role_cfg("evolution", round=number),
        )
        outcomes = await self._run_calls([unit], round=number)
        if not outcomes or not outcomes[0].ok:
            # Deliberately NOT marked done. Generation only records a shard that actually
            # succeeded; evolution used to record the step as complete on failure, so one
            # timeout removed evolution from that round permanently — a resumed or
            # continued run would never retry it. Run c4566ed2's engine_state says
            # `{"evolution": true}` for a round whose evolution call timed out twice, which
            # is why it has zero descent edges despite evolve_top_k = 3.
            attempts = int(self._round_mark(number, "evolution_attempts", 0)) + 1
            self._mark_round(number, evolution_attempts=attempts)
            if attempts >= MAX_EVOLUTION_ATTEMPTS:
                # A deterministically failing call must not loop forever across resumes.
                self._mark_round(number, evolution=True)
            self._events.emit(
                EventType.STEP_FAILED,
                {
                    "role": "evolution",
                    "round": number,
                    "requested": top_k,
                    "produced": 0,
                    "units": [f"attempt {attempts}"],
                },
                round=number,
            )
            return 0

        rows = {row["hid"]: row for row in self._store.list_hypotheses(self._run_id)}
        known = set(rows)
        added = 0
        for item in outcomes[0].data.get("hypotheses", []):
            claimed = [str(hid) for hid in item.get("derived_from", [])]
            parents = [hid for hid in claimed if hid in known]
            unresolved = sorted(set(claimed) - known)
            if unresolved:
                # A variant stored with no parents is indistinguishable from a freshly
                # generated one, and the descent graph the scientist is asked to trust can
                # be emptied without a trace. Say so rather than dropping it in silence.
                self._events.emit(
                    EventType.CONTRACT_VIOLATION,
                    {
                        "role": "evolution",
                        "round": number,
                        "error": (
                            f"derived_from names {', '.join(unresolved)}, which are not "
                            f"hypotheses in this run; lineage for "
                            f"{item.get('title', 'an offspring')!r} is incomplete"
                        ),
                        "unit": "lineage",
                        # No step was lost: the hypothesis is added, only its parentage is
                        # poorer. `_run_health` counts `contract_violation` rows as lost
                        # steps, so without this flag a lineage warning inflated
                        # `lost_steps` and deflated `retried_calls = failed - lost`.
                        "lost": False,
                    },
                    round=number,
                )
            self._add_hypothesis(
                item,
                number,
                parent_ids=parents,
                operator=item.get("operator"),
                # A variant bred entirely from seeded parents belongs to that seed. Without
                # this, cartographer attribution stopped at the first generation and any
                # measurement of "what did the graft actually produce" undercounted every
                # descendant. Only an unambiguous inheritance is claimed: a variant crossing
                # a seeded parent with an unseeded one has no single origin to name.
                seed_id=self._inherited_seed(parents, rows),
            )
            added += 1
        self._mark_round(number, evolution=True)
        return added

    @staticmethod
    def _inherited_seed(
        parents: Sequence[str], rows: Mapping[str, Mapping[str, Any]]
    ) -> str | None:
        """The seed every resolved parent shares, or `None` when they do not share one."""
        seeds = {rows[hid].get("seed_id") for hid in parents if hid in rows}
        if len(seeds) == 1:
            only = next(iter(seeds))
            return str(only) if only else None
        return None

    # --- step 7: meta-review -----------------------------------------------------------

    async def _meta_review(self, number: int, notes: Sequence[str]) -> None:
        """Synthesise the round into guidance. Reviews are never truncated; debates are.

        The input is specified rather than "whatever fits": every review of the round in
        all five fields, every debate, the standings, and the previous round's guidance.
        A review that never reaches the meta-review is a critique the next round cannot
        learn from, so when the prompt has to shrink it is the debates that give way,
        lowest-Elo match first.
        """
        history = list(self._run.get("feedback_history") or [])
        if any(entry.get("round") == number for entry in history):
            return

        reviewed = list(self._round_mark(number, "reviewed", []))
        reviews = self._store.list_reviews(self._run_id, hids=reviewed)
        debates = [
            match
            for match in self._store.list_matches(self._run_id, round=number)
            if match["status"] == "completed"
        ]
        standings = sorted(
            self._store.list_hypotheses(self._run_id, statuses=["active"]),
            key=self._rank_key,
        )
        previous_round, previous_guidance = self._latest_guidance()
        unit = _Unit(
            key=f"meta_review:{number}",
            prompt=meta_review_prompt(
                self._goal,
                round=number,
                reviews=reviews,
                debates=debates,
                standings=standings,
                previous_guidance=previous_guidance,
                previous_guidance_round=previous_round,
                scientist_notes=notes,
                round_health=self._round_health(number, reviews=len(reviews), debates=len(debates)),
            ),
            cfg=self._role_cfg("meta_review", round=number),
        )
        outcomes = await self._run_calls([unit], round=number)
        if not outcomes or not outcomes[0].ok:
            return

        data = outcomes[0].data
        # Unwrapped on the way in, exactly as the review fields are. Guidance is the only
        # artefact that crosses a round boundary: a JSON-encoded value here is injected
        # verbatim into both generation shards, the evolution call and the next
        # meta-review, and then into the report's guidance trajectory.
        guidance = (
            f"RECURRING ISSUES:\n{unwrap_prose(data['recurring_issues'])}\n\n"
            f"WHAT WINS:\n{unwrap_prose(data['what_wins'])}\n\n"
            f"GUIDANCE FOR THE NEXT ROUND:\n{unwrap_prose(data['guidance_next_round'])}"
        )
        self._store.record_feedback(self._run_id, number, guidance)
        self._reload()
        self._events.emit(
            EventType.FEEDBACK_RECORDED, {"round": number, "guidance": guidance}, round=number
        )

    def _round_health(self, number: int, *, reviews: int, debates: int) -> str:
        """What went wrong in this round, for the meta-review that steers the next one."""
        failures = [item for item in self._losses if item.get("round") == number]
        if not failures:
            return ""
        added = sum(
            1
            for row in self._store.list_hypotheses(self._run_id)
            if int(row["created_round"] or 0) == number
        )
        listed = "; ".join(
            f"{item.get('role')}{_unit_note(item)} failed: {item.get('error')}"
            for item in failures[:8]
        )
        return (
            f"{added} new hypothesis/hypotheses, {reviews} new review(s), "
            f"{debates} debate(s). {listed}."
        )

    # --- the overview ------------------------------------------------------------------

    async def _write_overview(self) -> None:
        """Compose the deliverable. Every ending reaches this, including budget exhaustion.

        Two calls were reserved at run start precisely so this one is affordable; if even
        that is gone (the dollar ceiling, say) the run still finishes — with a warning
        rather than a report, because a truthful absence beats an invented summary.

        Written once per ending, and the guard is what makes that true: a run killed while
        `finishing` and restarted must not pay for a second overview. `overview_stale` is
        how a *continued* run gets past that guard — the scientist gave it more rounds, so
        the report it wrote when it ended no longer describes it. What follows is a rewrite
        and not an amendment: the prompt is composed from the whole snapshot as it stands
        now and is never shown the previous text, so a continued run's report reads as one
        document about the fuller pool rather than a report with a postscript.
        """
        state = self._engine_state()
        if state.get("overview_md") and not state.get("overview_stale"):
            return
        if self._config.workflow == "adaptive":
            from app.engine.research import ResearchController

            await ResearchController(self).report()
            return
        try:
            self._store.check_budget(self._run_id, cost=1, reserve=0)
        except BudgetExhausted as exc:
            # Recorded, not only logged. Two of 29 app runs ended `completed` with no
            # deliverable and the only machine-readable trace was `has_overview: false`
            # buried in a payload; the Report tab said "No report was written" and offered
            # no reason and no next step. `_flag_budget` deduplicates, so it cannot be the
            # thing that reports this — by definition the flag is already set.
            self._flag_budget(exc)
            reason = f"budget_{exc.reason}"
            detail = (
                f"the run reached its {'cost' if exc.reason == 'usd' else 'call'} ceiling "
                f"(${exc.spend_usd:.2f} of ${exc.budget_usd:.2f}; "
                f"{exc.calls_used} of {exc.budget_calls} calls) before the report was written"
            )
            self._set_engine_state(overview_skipped_reason=reason)
            self._events.emit(EventType.REPORT_SKIPPED, {"reason": reason, "detail": detail})
            log.warning("run %s cannot afford its overview: %s", self._run_id, exc)
            return

        rounds_completed = int(state.get("last_completed_round", 0))
        snapshot = self._store.snapshot(self._run_id)
        leaderboard = snapshot["leaderboard"]
        active = [row for row in leaderboard if row["status"] == "active"]
        nothing_survived = not active
        standings = active or leaderboard
        # A hypothesis that has never played a match sits at the untouched default of 1200
        # and used to be presented as a top result — above ideas that competed and lost.
        # Every run ends with `evolve_top_k` of them, created after the final round's
        # tournament, so this was not an edge case: 23 runs in the corpus have at least one
        # in their reported top five, and five runs have three.
        ranked = [row for row in standings if int(row.get("matches") or 0) > 0]
        unranked = [row for row in standings if int(row.get("matches") or 0) == 0]
        debates = [
            match
            for match in self._store.list_matches(self._run_id)
            if match["status"] == "completed"
        ]
        unit = _Unit(
            key="overview",
            prompt=overview_prompt(
                self._goal,
                top=ranked[:OVERVIEW_TOP_K],
                also_ranked=ranked[OVERVIEW_TOP_K:],
                standings=standings,
                unranked=unranked,
                reviews=self._reviews_by_hid(),
                debates=debates,
                feedback_history=snapshot["feedback_history"],
                counts=snapshot["run"]["counts"],
                rounds_completed=rounds_completed,
                health=self._health_note(rounds_completed=rounds_completed),
                nothing_survived=nothing_survived,
                context=self._context,
            ),
            cfg=self._role_cfg("overview"),
        )
        outcomes = await self._run_calls([unit], round=None, reserve=0)
        if outcomes and outcomes[0].ok:
            # Both in one write: a fresh report that still read as stale would be rewritten
            # again by the next ending, at the price of a call and a different answer.
            self._set_engine_state(
                overview_md=outcomes[0].data["markdown"],
                overview_stale=False,
                overview_skipped_reason=None,
            )
        elif not state.get("overview_md"):
            # Only when there is nothing to show. A *continued* run that fails to rewrite
            # its report still has the one it wrote when it ended, and calling that missing
            # would be a second false statement on top of a first.
            self._set_engine_state(overview_skipped_reason="overview_call_failed")

    def _health_note(self, *, rounds_completed: int) -> str:
        """What the run lost, in the words the report has to be able to use.

        The overview agent was given goal, mode, a summary line built purely from
        successes, standings, top hypotheses and the guidance trajectory — and nothing
        else. It could not warn the reader because it was never told there was anything to
        warn about: a run whose generation died twice and whose second round introduced no
        ideas at all was narrated as an unqualified result.
        """
        planned = max(0, self._config.rounds)
        lines: list[str] = []
        if planned and rounds_completed < planned:
            lines.append(
                f"{rounds_completed} of {planned} planned round(s) were completed."
            )
        # The counts the run list and the run header show, read off the same event log they
        # are read off there. `self._losses` is this *process*'s list: a run resumed twice
        # starts each supervisor with only what `engine_state` carried, and the two numbers
        # then disagree about the same run. c4566ed2 reported `lost_steps: 0` in
        # `run_finished` and an empty RUN HEALTH section in its 28k-character report, while
        # the store's reading of the same database said 11 failed calls and 3 lost steps.
        health = self._store.snapshot(self._run_id)["run"]
        failed_calls = int(health.get("failed_calls") or 0)
        if failed_calls:
            lines.append(
                f"{failed_calls} model call(s) failed; "
                f"{int(health.get('lost_steps') or 0)} step(s) were lost outright."
            )
        if self._losses:
            listed = "; ".join(
                f"round {item.get('round') or '?'} {item.get('role')}"
                + _unit_note(item)
                + f" — {item.get('error')}"
                for item in self._losses[:12]
            )
            more = (
                f" and {len(self._losses) - 12} more" if len(self._losses) > 12 else ""
            )
            lines.append(f"Steps that failed: {listed}{more}.")
        barren = self._barren_rounds(rounds_completed)
        if barren:
            lines.append(
                "Rounds that added no new hypotheses: "
                + ", ".join(str(number) for number in barren)
                + "."
            )
        ended = {
            "budget_calls": "the call ceiling was reached",
            "budget_usd": "the cost ceiling was reached",
            "wall_clock": "the time limit was reached",
            "stop_requested": "the scientist stopped it",
            "finish_requested": "the scientist asked it to finish",
        }.get(self._end_reason)
        if ended:
            lines.append(f"The run ended because {ended}.")
        return " ".join(lines)

    def _barren_rounds(self, rounds_completed: int) -> list[int]:
        """Completed rounds whose generation and evolution produced nothing at all."""
        if self._config.workflow == "adaptive":
            return []  # evidence-only checkpoints are purposeful research, not lost generation
        added: Counter[int] = Counter()
        for row in self._store.list_hypotheses(self._run_id):
            added[int(row["created_round"] or 0)] += 1
        return [
            number
            for number in range(2, rounds_completed + 1)
            if added.get(number, 0) == 0
        ]

    # --- calling models ----------------------------------------------------------------

    async def _run_calls(
        self,
        units: Sequence[_Unit],
        *,
        round: int | None,
        on_result: Callable[[_Outcome], None] | None = None,
        reserve: int = OVERVIEW_RESERVED_CALLS,
    ) -> list[_Outcome]:
        """Run units concurrently; record and emit for them serially, here.

        This is where the single-writer discipline is implemented. Workers do exactly one
        thing — call the runner — and report over a queue. Every database write and every
        event comes off that queue in this coroutine, so `seq` order is commit order no
        matter how the calls interleave. `on_result` runs here too, which is why a
        leaderboard updates as matches land rather than at the end of the wave.
        """
        affordable = self._affordable(units, reserve=reserve)
        if not affordable:
            return []

        semaphore = asyncio.Semaphore(PARALLEL_CALLS)
        queue: asyncio.Queue[tuple[str, int, Any, _Outcome | None]] = asyncio.Queue()
        # Admitted first attempts must remain reserved until their ledger writes land.
        unsettled_calls = len(affordable)

        async def worker(index: int, unit: _Unit) -> None:
            nonlocal unsettled_calls
            prompt, data, error = unit.prompt, None, None
            telemetry: dict[str, Any] = {}
            cfg = unit.cfg
            try:
                for attempt in range(MAX_CALL_ATTEMPTS):
                    if attempt:
                        if not self._may_retry(reserve=reserve + unsettled_calls):
                            break
                        unsettled_calls += 1
                    await self._ready.wait()
                    async with semaphore:
                        queue.put_nowait(("started", index, None, None))
                        try:
                            result = await self._runner.run_role(cfg.role, prompt, cfg)
                        except asyncio.CancelledError:
                            raise
                        except BudgetExhausted as exc:
                            # Ours, not the model's. The spawn gate re-checks the ceiling,
                            # and a refusal there is a budget stop — retrying it is
                            # guaranteed to raise again, and reporting it as a role
                            # contract violation blames the wrong thing.
                            queue.put_nowait(("budget", index, exc, None))
                            error, data = f"budget exhausted: {exc}", None
                            break
                        except Exception as exc:  # noqa: BLE001 — a runner cannot kill the run
                            result = RoleResult(
                                role=cfg.role,
                                model=cfg.model,
                                error=f"{type(exc).__name__}: {exc}",
                            )
                    queue.put_nowait(("finished", index, result, None))
                    if result.error is not None:
                        error, data = result.error, None
                        if attempt + 1 >= MAX_CALL_ATTEMPTS:
                            break  # no next attempt, so nothing to decide terms for
                        stretched = self._retry_terms(cfg, result.error)
                        if stretched is None:
                            break
                        cfg = stretched
                        continue
                    problem = validate_role_output(cfg.role, result.data)
                    if problem is None and unit.validate is not None:
                        problem = unit.validate(dict(result.data or {}))
                    if problem is None:
                        data, error = dict(result.data or {}), None
                        telemetry = dict(result.telemetry)
                        break
                    error, data = problem, None
                    # A schema violation is the one failure a re-ask on the same terms can
                    # actually fix, because the terms were never the problem.
                    prompt = unit.prompt + repair_suffix(problem)
                    # Announced, because it was the only failure class in the loop that
                    # left no trace anywhere. `_record_call` has already written an `ok`
                    # ledger row and emitted `call_finished` with `ok: true` by now, so a
                    # scientist counting successful calls against results found three
                    # unexplained reflection calls in run c4566ed2 and no line in any log
                    # or event about any of them.
                    if attempt + 1 < MAX_CALL_ATTEMPTS:
                        queue.put_nowait(("repair", index, problem, None))
            except BaseException as exc:  # noqa: BLE001 — including cancellation
                error, data = error or f"{type(exc).__name__}: {exc}", None
                raise
            finally:
                # Unconditional: the drain loop counts these down, and a worker that died
                # without reporting would leave it waiting on a queue nobody will fill.
                queue.put_nowait(
                    ("done", index, None, _Outcome(unit=unit, data=data, error=error,
                                                 telemetry=telemetry))
                )

        tasks = [asyncio.create_task(worker(index, unit)) for index, unit in enumerate(affordable)]
        outcomes: dict[int, _Outcome] = {}
        try:
            pending = len(affordable)
            while pending:
                kind, index, result, outcome = await queue.get()
                unit = affordable[index]
                if kind == "started":
                    self._events.emit(
                        EventType.CALL_STARTED,
                        {
                            "role": unit.cfg.role,
                            "model": unit.cfg.model,
                            "round": round,
                            "unit": unit.cfg.unit,
                        },
                        round=round,
                    )
                elif kind == "finished":
                    assert isinstance(result, RoleResult)
                    self._record_call(unit, result, round=round)
                    unsettled_calls -= 1
                elif kind == "repair":
                    assert isinstance(result, str)
                    log.warning(
                        "run %s: %s (%s) returned a payload its schema refused — re-asking: %s",
                        self._run_id,
                        unit.cfg.role,
                        unit.cfg.unit or unit.key,
                        result,
                    )
                    self._events.emit(
                        EventType.SCHEMA_REPAIRED,
                        {
                            "role": unit.cfg.role,
                            "round": round,
                            "attempt": 1,
                            "problem": result[:500],
                            "unit": unit.cfg.unit or unit.key,
                        },
                        round=round,
                    )
                elif kind == "budget":
                    assert isinstance(result, BudgetExhausted)
                    self._flag_budget(result)
                else:
                    assert outcome is not None
                    outcomes[index] = outcome
                    if outcome.error is not None:
                        self._record_loss(
                            round=round,
                            role=unit.cfg.role,
                            unit=unit.cfg.unit or unit.key,
                            error=outcome.error,
                        )
                        self._events.emit(
                            EventType.CONTRACT_VIOLATION,
                            {
                                "role": unit.cfg.role,
                                "round": round,
                                "error": outcome.error,
                                "unit": unit.cfg.unit or unit.key,
                            },
                            round=round,
                        )
                    if on_result is not None:
                        on_result(outcome)
                    pending -= 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        return [outcomes[index] for index in sorted(outcomes)]

    def _retry_terms(self, cfg: RoleConfig, error: str) -> RoleConfig | None:
        """The terms a second attempt gets after a failed call, or `None` for no retry.

        A transport failure is worth re-sending as it stands. A **timeout** is not: the
        deadline is the thing that failed, so the retry either gets a longer one or does
        not happen. The stretch is bounded by what is left of the run's wall clock, which
        is what stops a doubling retry from outliving `wall_clock_minutes`.
        """
        if not error.startswith("timeout after"):
            return cfg
        stretched = cfg.timeout_s * TIMEOUT_RETRY_FACTOR
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            stretched = min(stretched, remaining)
        if stretched <= cfg.timeout_s:
            # No room to try on different terms, so there is nothing to try.
            log.info(
                "run %s will not retry the %s timeout: no wall clock left to lengthen it",
                self._run_id,
                cfg.role,
            )
            return None
        log.warning(
            "run %s retrying %s on a %.0fs deadline after a timeout at %.0fs",
            self._run_id,
            cfg.role,
            stretched,
            cfg.timeout_s,
        )
        return cfg.with_timeout(stretched)

    def _may_retry(self, *, reserve: int) -> bool:
        """Whether a second attempt is still affordable.

        `_affordable` prices one call per unit, but the worker can make two, so a wave of
        N admitted units could spend 2N against a reserve sized for N — and the reserve
        that goes missing is the report's. Checking here keeps it an invariant rather than
        an estimate, at the cost of one read per retry.
        """
        try:
            self._store.check_budget(self._run_id, cost=1, reserve=reserve)
        except BudgetExhausted:
            return False
        return True

    def _affordable(self, units: Sequence[_Unit], *, reserve: int) -> list[_Unit]:
        """Schedule only the calls the run can still pay for, cheapest guard first."""
        affordable: list[_Unit] = []
        for unit in units:
            try:
                self._store.check_budget(
                    self._run_id, cost=len(affordable) + 1, reserve=reserve
                )
            except BudgetExhausted as exc:
                self._flag_budget(exc)
                log.info("run %s refused %s: %s", self._run_id, unit.key, exc)
                break
            affordable.append(unit)
        return affordable

    def _record_call(self, unit: _Unit, result: RoleResult, *, round: int | None) -> None:
        """Ledger, backpressure, degradation and the call_finished event — in that order.

        Deliberately synchronous. An audit proposed moving `spend` onto a thread to keep
        the event loop free while other streams drain, and the latency argument is real —
        but this coroutine is the single writer of `run_events`, and every `await` added
        here is a new point at which a killed supervisor can leave a call paid for and its
        result unrecorded. `budget_guard` in `spawn` takes the thread instead: it is on the
        worker's path, where a yield costs nothing.
        """
        usage = result.usage
        try:
            self._store.spend(
                self._run_id,
                role=unit.cfg.role,
                model=unit.cfg.model,
                round=round,
                tokens_in=usage.tokens_in,
                tokens_out=usage.tokens_out,
                cache_creation=usage.cache_creation,
                cache_read=usage.cache_read,
                cost_usd=usage.cost_usd,
                duration_ms=usage.duration_ms,
                status="ok" if result.ok else "error",
            )
        except BudgetExhausted as exc:
            self._flag_budget(exc)

        if result.rate_limited:
            # `cooldown_s` so the Activity tab can say "paused for 30s" rather than showing
            # a rate-limit line and then nothing. Backpressure is the one state where the
            # run is deliberately doing nothing, and it must not look like a hang — on a
            # subscription this is the constraint that actually bites, not spend.
            self._events.emit(
                EventType.RATE_LIMITED,
                {
                    "role": unit.cfg.role,
                    "round": round,
                    "cooldown_s": self._rate_limit_cooldown,
                    "already_waiting": not self._ready.is_set(),
                },
                round=round,
            )
            self._engage_backpressure()
        if result.degraded:
            self._events.emit(
                EventType.ROLE_DEGRADED,
                {
                    "role": unit.cfg.role,
                    "model": unit.cfg.model,
                    "reason": result.error or "the call completed on different terms",
                },
                round=round,
            )
        if result.telemetry.get("near_timeout"):
            log.warning(
                "run %s: %s used %.0fs of its %.0fs ceiling",
                self._run_id,
                unit.cfg.role,
                float(result.telemetry.get("elapsed_s") or 0),
                unit.cfg.timeout_s,
            )
        telemetry = {
            key: result.telemetry[key]
            for key in CALL_TELEMETRY_FIELDS
            if result.telemetry.get(key) is not None
        }
        self._events.emit(
            EventType.CALL_FINISHED,
            {
                "role": unit.cfg.role,
                "model": unit.cfg.model,
                "round": round,
                "ok": result.ok,
                "duration_ms": usage.duration_ms,
                "error": result.error,
                **({"telemetry": telemetry} if telemetry else {}),
            },
            round=round,
        )

    def _engage_backpressure(self) -> None:
        """Stop spawning new calls until the provider's rate limit clears.

        Calls already in flight are left alone — they are paid for and may well succeed.
        What stops is the *next* one: every worker waits on `self._ready` before taking a
        semaphore slot, so a cleared event drains the wave without cancelling anything.

        Re-entrant on purpose. A second limit arriving inside an open cooldown does not
        extend it: the cooldown is a fixed backoff, and stacking extensions on a plan-window
        limit that reports on every call would push the run out to an unbounded wait.
        """
        if not self._ready.is_set():
            return
        self._ready.clear()
        task = asyncio.create_task(asyncio.sleep(self._rate_limit_cooldown))
        self._background.add(task)
        # The gate is reopened from the task's *done callback*, not from inside the
        # coroutine. A callback fires however the task ends — elapsed, cancelled, or
        # cancelled before it ever took its first step, which a `finally` inside the
        # coroutine would miss entirely. Nothing may leave workers parked on an event that
        # will never be set again.
        task.add_done_callback(self._release_backpressure)

    def _release_backpressure(self, task: asyncio.Task[None]) -> None:
        self._background.discard(task)
        self._ready.set()

    def _record_loss(
        self, *, round: int | None, role: str, unit: str | None, error: str
    ) -> None:
        """Remember a unit the run gave up on, for the report and the run header.

        Persisted rather than only emitted: the overview is composed at the end of a run
        that may have been resumed twice, and reconstructing this by replaying an event log
        that is itself windowed to 50 rows is how the fact stayed invisible.
        """
        entry = {"round": round, "role": role, "unit": unit, "error": error[:300]}
        self._losses.append(entry)
        self._set_engine_state(losses=self._losses[-100:])

    def _flag_budget(self, exc: BudgetExhausted) -> None:
        self._end_reason = f"budget_{exc.reason}"
        if self._budget_exhausted:
            return
        self._budget_exhausted = True
        self._events.emit(
            EventType.BUDGET_WARNING,
            {
                "reason": exc.reason,
                "calls_used": exc.calls_used,
                "budget_calls": exc.budget_calls,
                "spend_usd": exc.spend_usd,
                "budget_usd": exc.budget_usd,
            },
        )

    def _role_cfg(
        self,
        role: str,
        *,
        round: int | None = None,
        unit: str | None = None,
        effort: str | None = None,
    ) -> RoleConfig:
        try:
            model, base_effort = self._models[role]
        except KeyError:
            raise ValueError(
                f"run {self._run_id} has no model_table entry for role {role!r}"
            ) from None
        cfg = role_config(
            role,
            model=model,
            # An escalation raises a floor; it never lowers one. See `effort_at_least`.
            effort=effort_at_least(base_effort, effort) if effort else base_effort,
            system_prompt=self._system_prompts[role],
            round=round,
            unit=unit,
            # The ceiling is a function of the terms, not of the role alone: the same role
            # at `low`/`shallow` and at `max`/`deep` are not the same call, and one number
            # for both is what killed every grounded generation call in run c4566ed2.
            grounding_depth=self._config.grounding_depth,
        )
        if self._config.workflow == "adaptive" and cfg.tools:
            strategy = (self._engine_state().get("research", {}).get("frame") or {}).get(
                "source_strategy"
            )
            cfg = replace(cfg, system_prompt=cfg.system_prompt + source_strategy_prompt(strategy))
        return cfg

    # --- boundaries, control and state -------------------------------------------------

    def _start_clock(self) -> None:
        """Arm the wall-clock ceiling, if this run has one.

        Timed from when *this* supervisor started driving, not from when the run was
        created. The ceiling exists to stop a pathological loop, and a run that was paused
        on Friday and resumed on Monday has not looped — it has waited for a person. Timing
        from creation would finish it instantly on resume, before it did any work at all.
        """
        minutes = self._config.wall_clock_minutes
        self._deadline = time.monotonic() + minutes * 60 if minutes else None

    def _out_of_time_now(self) -> bool:
        """True the first time the ceiling is passed; records and announces it once."""
        if self._deadline is None or self._out_of_time:
            return self._out_of_time
        if time.monotonic() < self._deadline:
            return False

        self._out_of_time = True
        minutes = self._config.wall_clock_minutes or 0.0
        elapsed = (time.monotonic() - self._deadline) / 60 + minutes
        totals = self._store.budget_totals(self._run_id)
        log.info(
            "run %s passed its %.0f-minute ceiling at %.1f minutes; finishing gracefully",
            self._run_id,
            minutes,
            elapsed,
        )
        # The same event a spent budget emits, because it is the same thing from the
        # scientist's side: a ceiling they set has stopped the run. Reusing it means the
        # Activity tab renders it without knowing this ceiling exists.
        self._events.emit(
            EventType.BUDGET_WARNING,
            {
                "reason": "wall_clock",
                "elapsed_minutes": round(elapsed, 1),
                "wall_clock_minutes": minutes,
                **totals,
            },
        )
        return True

    def _boundary(self) -> _Directive:
        """Heartbeat and read the control flag. Called at every step boundary."""
        if not self._store.heartbeat(self._run_id):
            raise RunHalted(f"run {self._run_id} is gone")
        if self._out_of_time_now():
            # Deliberately the `finish` path and not the `stop` one: the run ran out of
            # time, it was not cancelled, so it ends `completed` with its report written.
            self._finish_requested = True
            self._end_reason = "wall_clock"
            return _Directive.FINISH
        action = self._store.get_control(self._run_id)
        if action == "pause":
            self._pause_requested = True
            return _Directive.PAUSE
        if action in ("stop", "force_stop"):
            self._stop_requested = True
            self._end_reason = "stop_requested"
            return _Directive.STOP
        if action == "finish":
            self._finish_requested = True
            self._end_reason = "finish_requested"
            return _Directive.FINISH
        return _Directive.CONTINUE

    def _interrupted(self) -> bool:
        """True when the round must stop where it is (mid-round, between steps).

        A spent budget and a passed wall clock both unwind here rather than waiting for the
        round to end: both are ceilings, and a ceiling that lets the run keep spending for
        another few steps is not one. A `finish` *request* is different — it means "stop
        after this round" — which is why it is not in this list.
        """
        directive = self._boundary()
        return (
            directive in (_Directive.PAUSE, _Directive.STOP)
            or self._budget_exhausted
            or self._out_of_time
        )

    def _clear_stale_control(self) -> None:
        """A resume request is consumed by starting; a stop or finish still stands."""
        if self._store.get_control(self._run_id) in ("resume", "pause"):
            self._store.set_control(self._run_id, None)

    def _set_lifecycle(self, lifecycle: str) -> None:
        previous = self._run.get("lifecycle")
        if previous == lifecycle:
            return
        self._run = self._store.set_lifecycle(self._run_id, lifecycle)
        self._events.emit(
            EventType.LIFECYCLE_CHANGED, {"lifecycle": lifecycle, "previous": previous}
        )

    def _reload(self) -> dict[str, Any]:
        run = self._store.get_run(self._run_id)
        if run is None:
            raise RunHalted(f"run {self._run_id} is gone")
        self._run = run
        return run

    def _engine_state(self) -> dict[str, Any]:
        return dict(self._run.get("engine_state") or {})

    def _set_engine_state(self, **patch: Any) -> None:
        self._run["engine_state"] = self._store.set_engine_state(self._run_id, patch)

    def _round_mark(self, number: int, key: str, default: Any) -> Any:
        steps = self._engine_state().get("steps") or {}
        return (steps.get(str(number)) or {}).get(key, default)

    def _mark_round(self, number: int, **marks: Any) -> None:
        """Persist step-completion markers so a resumed round skips what it finished."""
        steps = dict(self._engine_state().get("steps") or {})
        steps[str(number)] = {**(steps.get(str(number)) or {}), **marks}
        self._set_engine_state(steps=steps)

    def _latest_guidance(self) -> tuple[int | None, str]:
        """The newest guidance and **which round produced it**.

        The round used to be discarded at this line and the composer then printed whatever
        came back under a fixed header saying it came from the last round. A meta-review
        that fails records nothing, so the next round was told that round-(N−2) guidance —
        including its standings-derived WHAT WINS, about a tournament that has since
        moved — was the previous round's. Returning the round lets the prompt say what is
        true; see `prompts._feedback`.
        """
        history = list(self._run.get("feedback_history") or [])
        if not history:
            return None, ""
        newest = history[-1]
        round_ = newest.get("round")
        return (int(round_) if round_ is not None else None), str(newest.get("guidance") or "")

    def _reviews_by_hid(self) -> dict[str, dict[str, Any]]:
        """Latest review per hypothesis, for the prompts that show one alongside a body."""
        latest: dict[str, dict[str, Any]] = {}
        for review in self._store.list_reviews(self._run_id):
            latest[review["hid"]] = review
        return latest

    def _project(self, reason: str) -> None:
        if self._projector is None:
            return
        try:
            self._projector(reason)
        except Exception:  # noqa: BLE001 — a projection failure must not fail the run
            log.exception("projection after %s failed for run %s", reason, self._run_id)
