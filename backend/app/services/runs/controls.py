"""Pause, resume, stop, finish, continue, force-stop — and the reconciler after a crash.

Three kinds of control live here, and the differences between them are the whole design:

**Cooperative controls** (`pause`, `resume`, `stop`, `finish`) set a flag on the run row.
The supervisor reads it at every step boundary and acts on it there. Nothing is killed,
nothing is interrupted mid-call, and `stop` still writes the overview — which is the promise
the Confirm step makes to the scientist, so it is honoured by making stop an ordinary
ending rather than a special one.

**Force-stop** kills the process tree. It exists because a cooperative stop needs a
supervisor that is still reading its flag, and the case you need a stop button for is
exactly the case where it might not be. The pid is verified before the kill, twice over:
the recorded pid must still be a running Python process, *and* the run must still be
beating recently enough for that record to mean anything. Pids are recycled, this
application is itself a Python process, and `taskkill /T` on a stale pid takes down
whatever inherited it. The price of force-stop is the report: nothing writes the overview,
because the process that would have is gone. That is why the UI only reveals it twenty
seconds after an ordinary stop.

**Continue** goes the other way: it takes a run that has *ended* and gives it more rounds,
in place. Everything the run learned stays and is used — the hypotheses with their Elo and
match history, the clusters, the meta-review guidance that was steering the next round —
so the round after a continue opens on exactly the standings and guidance it would have
had if the run had never stopped. That is the difference between it and `from_run`, which
copies the setup and starts the science from zero; a scientist who ran Quick, liked what
they saw and wants to go deeper wants this one, and the guidance loop is the whole reason
why. It is also the single sanctioned exception to `config` being immutable after launch,
which is why the mutation is a store method that can only reach three keys — the round
target and the two ceilings — and why it leaves a `run_extended` event behind saying by how
much.

**The reconciler** closes the last case: nobody pressed anything, the supervisor simply
died. A run whose heartbeat is a minute old and whose pid is gone is marked `lost`, which
frees its harness lane. It runs on startup, every thirty seconds, and inline before the lane
check on `POST /runs` — the last of those because a wedged lane with the backend still up
would otherwise be unfixable without a restart.

The legality table is plan C4's, written there once and implemented here once. The same
table drives the frontend's button enablement, through `allowed_actions`.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.core.config import Settings, get_settings
from app.db.engine_models import LANE_LIFECYCLES
from app.engine.core import OVERVIEW_RESERVED_CALLS
from app.engine.events import EventType, EventWriter
from app.engine.spawn import process_image, python_executable, terminate_pid_tree
from app.engine.store import (
    LIVE_LIFECYCLES,
    UNCHANGED,
    LifecycleConflict,
    RunNotFound,
    RunStore,
    Unchanged,
    usd_ceiling,
)
from app.services.runs.launcher import (
    MAX_ROUNDS,
    LaunchRefused,
    lane_busy,
    spawn_supervisor,
)

__all__ = [
    "ACTIONS",
    "CONTINUE",
    "CONTINUE_LIFECYCLES",
    "LEGAL_ACTIONS",
    "STALE_HEARTBEAT_SECONDS",
    "SUPERVISOR_IMAGES",
    "IllegalTransition",
    "allowed_actions",
    "apply_control",
    "halt_all",
    "quiesce_supervisors",
    "reconcile_runs",
]

log = logging.getLogger(__name__)

CONTINUE = "continue"
"""The wire value of the extend-an-ended-run action.

Spelled out as a constant because `continue` is a Python keyword: the string is what the
API accepts and what `LEGAL_ACTIONS` is keyed by, while nothing in this module is ever
*named* `continue`. Calling the action something else to dodge the keyword would have put
the awkwardness in the place a scientist reads it rather than the place a compiler does.
"""

CONTINUE_LIFECYCLES: frozenset[str] = frozenset({"completed", "stopped"})
"""The two endings a person would want to extend, and deliberately only those.

`completed` is a run that reached its round target, and `stopped` is one the scientist
ended early — both of them ran to a clean boundary and wrote a report, so "give it three
more rounds" means exactly one thing.

`failed` and `lost` are excluded, and the exclusion is the point. A `failed` run stopped
because something went wrong that nobody has diagnosed — a missing model table, a runner
that would not construct, a database it could not reach — and continuing would re-enter
that code path with the failure still in `runs.error` and no report to build on. A `lost`
run is worse: its supervisor died without saying anything, so nothing here knows whether
the machine rebooted, the process was killed, or the loop wedged. Salvaging one of those is
a real feature and a different one — it would have to show the operator what went wrong and
ask — and letting the Continue button quietly mean "resurrect" half the time would make it
mean nothing the rest of the time.
"""

# Plan C4's legal control actions, by the lifecycle they are legal in. This table is the
# contract: the API validates against it and the frontend renders its buttons from it.
LEGAL_ACTIONS: dict[str, frozenset[str]] = {
    "pause": frozenset({"running"}),
    "resume": frozenset({"paused"}),
    CONTINUE: CONTINUE_LIFECYCLES,
    "stop": frozenset({"running", "paused", "pausing"}),
    "finish": frozenset({"running", "paused"}),
    "force_stop": frozenset(LIVE_LIFECYCLES),
}

ACTIONS: tuple[str, ...] = ("pause", "resume", CONTINUE, "stop", "finish", "force_stop")

STALE_HEARTBEAT_SECONDS = 60.0
"""How old a heartbeat has to be before a run is a candidate for `lost`.

Six missed beats. Long enough to survive a database hiccup or a machine that swapped, short
enough that a scientist watching a dead run finds out within a minute.
"""

SUPERVISOR_IMAGES: frozenset[str] = frozenset(
    {Path(python_executable()).name.lower(), "python.exe", "pythonw.exe"}
)
"""Executable names a supervisor pid may legitimately have. Anything else is a recycled pid."""

PID_TRUST_SECONDS = 600.0
"""How stale a run's heartbeat may be and still vouch for its recorded pid.

The image check on its own cannot identify a supervisor, because this application is a
Python process too: a recycled pid pointing at the API server would pass it, and
`taskkill /T` would then take down the backend. The heartbeat is the second half of the
proof the plan asks for — the pid is trusted only while something is still writing
liveness to *that run's row*.

Sixty missed beats. A healthy supervisor writes every ten seconds from a task of its own,
so it keeps beating through even a seven-minute grounded call; one that cannot reach the
database stops itself after two failures. A row untouched for ten minutes is therefore not
being written by a supervisor of ours, whatever is running under its pid now.

The cost is the far edge: a supervisor wedged so completely that it has not beaten in ten
minutes will not be force-stopped, and has to be ended from Task Manager. That is the
right way round — killing the wrong process is damage, refusing to kill is an inconvenience
with a manual fix.
"""


class IllegalTransition(RuntimeError):
    """The action is not legal on this run as it stands."""

    def __init__(
        self,
        action: str,
        lifecycle: str,
        allowed: Sequence[str],
        *,
        reason: str | None = None,
    ) -> None:
        self.action = action
        self.lifecycle = lifecycle
        self.allowed = tuple(allowed)
        super().__init__(
            f"Cannot {action} a run that is {lifecycle}."
            + (f" {reason}" if reason else "")
            + (f" Available here: {', '.join(self.allowed)}." if self.allowed else "")
        )


IMPORTED_REFUSAL = (
    "An imported run is a record of research another engine did: it carries no resolved "
    "model table, no workdir and no budget, so nothing here can drive it."
)


def allowed_actions(lifecycle: str, *, source: str = "app") -> tuple[str, ...]:
    """Which controls are legal, in the order a control bar shows them.

    `source` narrows the answer and is not decoration. Every imported run is `completed`,
    which since `continue` exists is no longer a lifecycle that offers nothing — and an
    imported run is the one thing continuing must never touch, because a supervisor would
    fail on its first step and stamp `failed` over the only copy of a historical result. A
    table that answered on the lifecycle alone would light up a Continue button that can
    only ever return an error.
    """
    if source != "app":
        return ()
    return tuple(action for action in ACTIONS if lifecycle in LEGAL_ACTIONS[action])


# ------------------------------------------------------------------------------ controls


def apply_control(
    store: RunStore,
    run_id: UUID,
    action: str,
    *,
    add_rounds: int | None = None,
    budget_calls: int | None = None,
    budget_usd: float | None | Unchanged = UNCHANGED,
    settings: Settings | None = None,
    reveal_conflict: bool = False,
) -> dict[str, Any]:
    """Apply one control to one run. Returns `{accepted, lifecycle}` per plan C5.

    `add_rounds`, `budget_calls` and `budget_usd` belong to `continue` alone. They are
    refused on any other action rather than ignored: a caller who sent a raised budget with
    a `finish` has asked for something that will not happen, and answering "accepted" would
    be a lie about what the run is now allowed to do.

    `budget_usd` has three states and only two of them are values. `UNCHANGED` leaves the
    run's dollar ceiling alone, a number raises it, and an explicit `None` removes it.
    """
    resolved = settings or get_settings()
    if action not in LEGAL_ACTIONS:
        raise IllegalTransition(action, "unknown", ())
    if action != CONTINUE and (
        add_rounds is not None
        or budget_calls is not None
        or not isinstance(budget_usd, Unchanged)
    ):
        raise LaunchRefused(f"{action} takes no add_rounds, budget_calls or budget_usd")

    run = store.get_run(run_id)
    if run is None or run.get("deleted_at"):
        raise RunNotFound(f"No run {run_id}")

    lifecycle = str(run["lifecycle"])
    source = str(run.get("source") or "app")
    if source != "app":
        raise IllegalTransition(
            action, lifecycle, allowed_actions(lifecycle, source=source), reason=IMPORTED_REFUSAL
        )
    if lifecycle not in LEGAL_ACTIONS[action]:
        raise IllegalTransition(action, lifecycle, allowed_actions(lifecycle))

    if action == "force_stop":
        return _force_stop(store, run, action="force_stop")
    if action == "resume":
        return _resume(store, run, settings=resolved, reveal_conflict=reveal_conflict)
    if action == CONTINUE:
        return _continue_run(
            store,
            run,
            add_rounds=add_rounds,
            budget_calls=budget_calls,
            budget_usd=budget_usd,
            settings=resolved,
            reveal_conflict=reveal_conflict,
        )

    # Cooperative: the supervisor reads this at its next step boundary. The lifecycle does
    # not move here — it moves when the supervisor acts, which is the only moment at which
    # it is true.
    store.set_control(run_id, action)
    log.info("run %s: %s requested while %s", run_id, action, lifecycle)
    return {"accepted": True, "lifecycle": lifecycle}


def _assert_lane_is_free(
    store: RunStore,
    run_id: UUID,
    harness: str,
    *,
    requester_owner_id: UUID | None,
    reveal_conflict: bool,
) -> None:
    """Reconcile, then refuse if anything else holds this harness lane.

    Both controls that start a process — `resume` and `continue` — go through here, and
    `launch_run` does the same two things in the same order. Reconciling first is the point
    of doing it here rather than trusting the startup sweep: a supervisor that died while
    the backend stayed up would otherwise wedge its lane until the next restart, and the run
    that wants it back is sitting in front of a scientist who cannot fix that.

    This is a courtesy, not the decision. The decision is the partial unique index, which
    the write that takes the lane runs into whatever this said.
    """
    reconcile_runs(store)
    holder = store.lane_holder(harness, excluding_run_id=run_id)
    if holder:
        raise lane_busy(
            store,
            harness,
            requester_owner_id=requester_owner_id,
            reveal_conflict=reveal_conflict,
        )


def _resume(
    store: RunStore,
    run: Mapping[str, Any],
    *,
    settings: Settings,
    reveal_conflict: bool,
) -> dict[str, Any]:
    """Start a new supervisor for a paused run.

    A paused run has no process: pausing is how a supervisor exits cleanly with its work
    banked. Resuming therefore means launching again, and the new supervisor picks up from
    the persisted units — the round plan, the reviews already written — rather than from
    the beginning of the round.
    """
    run_id = UUID(str(run["id"]))
    # A run that gave up its lane while paused has to reclaim it, and something else may
    # have taken it in the meantime.
    _assert_lane_is_free(
        store,
        run_id,
        str(run["harness"]),
        requester_owner_id=run.get("owner_id"),
        reveal_conflict=reveal_conflict,
    )

    store.set_control(run_id, "resume")
    workdir = settings.workdir_for(str(run["engine_run_id"]))
    workdir.mkdir(parents=True, exist_ok=True)
    pid = spawn_supervisor(run_id, settings=settings, workdir=workdir)
    store.heartbeat(run_id, pid=pid)
    log.info("run %s resumed as pid %s", run_id, pid)
    return {"accepted": True, "lifecycle": str(run["lifecycle"])}


def _continue_run(
    store: RunStore,
    run: Mapping[str, Any],
    *,
    add_rounds: int | None,
    budget_calls: int | None,
    budget_usd: float | None | Unchanged,
    settings: Settings,
    reveal_conflict: bool,
) -> dict[str, Any]:
    """Give an ended run more rounds and start a supervisor for it — the same run id.

    Nothing is copied and nothing is reset. The new supervisor reads the same row, the same
    hypotheses with the Elo and match history they finished on, the same clusters, the same
    `feedback_history` — so `_drive` resumes at `last_completed_round + 1` and the round it
    opens is handed the previous round's meta-review guidance exactly as if the run had
    never stopped. The tournament pairs the newcomers against the incumbents on their real
    ratings rather than starting the standings over, because the standings were never
    touched.

    The increment is rounds *of work*, counted from what the run actually completed rather
    than from the target it was launched with. A run that was stopped at round 2 of 5 and
    continued by 3 runs rounds 3, 4 and 5; the same run continued by 1 runs round 3 and
    finishes, which lowers `rounds_target` to 3 and is the honest reading of "one more
    round". A completed run is the easy case: its target and its completed count are the
    same number.

    Rounds are not the only thing that can stop a run short, which is why both ceilings can
    move here. `budget_calls` raises the call budget; `budget_usd` raises or removes the
    dollar ceiling, and removing it is what rescues a run launched before that ceiling
    became optional — it stopped on a number that is API-equivalent telemetry rather than
    money, and the alternative to lifting it is throwing away every hypothesis, rating and
    round of guidance the run had already produced.
    """
    run_id = UUID(str(run["id"]))
    previous = str(run["lifecycle"])
    harness = str(run["harness"])

    if add_rounds is None:
        raise LaunchRefused("continue needs add_rounds: how many more rounds to run")
    if add_rounds < 1:
        raise LaunchRefused(f"add_rounds must be at least 1, got {add_rounds}")

    completed = int((run.get("engine_state") or {}).get("last_completed_round", 0))
    rounds_target = completed + add_rounds
    if rounds_target > MAX_ROUNDS:
        raise LaunchRefused(
            f"a run may not pass {MAX_ROUNDS} rounds; this one has completed {completed}, "
            f"so it can take at most {MAX_ROUNDS - completed} more"
        )

    ceiling = int(budget_calls if budget_calls is not None else (run.get("budget_calls") or 0))
    _assert_affordable(run, ceiling, budget_usd)

    _assert_lane_is_free(
        store,
        run_id,
        harness,
        requester_owner_id=run.get("owner_id"),
        reveal_conflict=reveal_conflict,
    )

    try:
        extended = store.extend_run(
            run_id,
            rounds_target=rounds_target,
            budget_calls=budget_calls,
            budget_usd=budget_usd,
            expect_lifecycles=tuple(sorted(CONTINUE_LIFECYCLES)),
        )
    except LifecycleConflict as exc:
        # The run moved between the read above and the lock — someone continued it first.
        raise IllegalTransition(
            CONTINUE, exc.lifecycle, allowed_actions(exc.lifecycle)
        ) from exc
    except IntegrityError as exc:
        # The index, not the check: something took the lane in between.
        raise lane_busy(
            store,
            harness,
            requester_owner_id=run.get("owner_id"),
            reveal_conflict=reveal_conflict,
        ) from exc
    except ValueError as exc:
        raise LaunchRefused(str(exc)) from exc

    before, after = extended["before"], extended["run"]
    # Safe to emit from out of band: the run was terminal until the line above, so there is
    # no supervisor alive to race the single-writer discipline with. It goes out before the
    # spawn so the audit record survives a spawn that fails.
    EventWriter(store, run_id).emit(
        EventType.RUN_EXTENDED,
        {
            "added_rounds": add_rounds,
            "rounds_target": after["rounds_target"],
            "previous_rounds_target": before["rounds_target"],
            "budget_calls": after["budget_calls"],
            "previous_budget_calls": before["budget_calls"],
            # Both sides of the dollar ceiling too, and `None` on either side means "no
            # ceiling" rather than "$0.00". A run that was capped at $5.00 and continued
            # without one has to read back as exactly that a year later, because removing
            # a ceiling is the part of this record somebody will come looking for.
            "budget_usd": usd_ceiling(after["budget_usd"]),
            "previous_budget_usd": before["budget_usd"],
            "previous_lifecycle": previous,
        },
    )

    workdir = settings.workdir_for(str(run["engine_run_id"]))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        pid = spawn_supervisor(run_id, settings=settings, workdir=workdir)
    except Exception:
        # Put the run back where it was. A continue that could not start a process has not
        # invalidated the science this run already did, and marking it `failed` — which is
        # what a failed *launch* does — would bury a finished run's report under a crash it
        # had nothing to do with.
        log.exception("could not start a supervisor to continue run %s", run_id)
        store.set_lifecycle(run_id, previous)
        EventWriter(store, run_id).emit(
            EventType.LIFECYCLE_CHANGED, {"lifecycle": previous, "previous": "queued"}
        )
        raise

    store.heartbeat(run_id, pid=pid)
    log.info(
        "run %s continued from %s: %d more round(s) to a target of %d, as pid %s",
        run_id,
        previous,
        add_rounds,
        after["rounds_target"],
        pid,
    )
    return {"accepted": True, "lifecycle": str(after["lifecycle"])}


def _assert_affordable(
    run: Mapping[str, Any], budget_calls: int, budget_usd: float | None | Unchanged = UNCHANGED
) -> None:
    """Refuse a continue the run could not act on, before anything is written.

    The engine reserves two calls for the overview at every step, so a run needs headroom
    for one step *plus* that reserve or it will come straight back out of the loop and end
    again — having spent a process launch to change nothing. Naming the number the request
    would need is the difference between a refusal a scientist can act on and one they have
    to guess at.

    The dollar ceiling is judged the same way, against the ceiling the *request* would leave
    the run with rather than the one it stopped on. It used to be judged against the stored
    one and the refusal told the scientist to start a new run, which was true when a ceiling
    was fixed at launch and is not true now that continuing can raise or remove it. Runs
    launched before the ceiling became optional still carry one, and throwing away their
    idea pool to escape a number that was never money is precisely the wrong trade — so the
    only thing still refused here is a request that would leave the run over its ceiling,
    and it is refused by naming the field that fixes it.
    """
    used = int(run.get("calls_used") or 0)
    needed = used + OVERVIEW_RESERVED_CALLS + 1
    # A ceiling of 0 means "no ceiling"; app runs always carry one, so this is belt to the
    # source check's braces.
    if budget_calls and budget_calls < needed:
        raise LaunchRefused(
            f"This run has used {used} of its {budget_calls} calls. Continuing needs "
            f"budget_calls of at least {needed}: one for the next step, and "
            f"{OVERVIEW_RESERVED_CALLS} held back so the run can still rewrite its report."
        )

    spend = float(run.get("spend_usd") or 0)
    dollars = (
        float(run.get("budget_usd") or 0)
        if isinstance(budget_usd, Unchanged)
        else float(budget_usd or 0)
    )
    if dollars and spend >= dollars:
        raise LaunchRefused(
            f"This run has spent ${spend:.4f} of a ${dollars:.2f} cost ceiling, so it "
            "would stop again on its first step. Raise or remove the cost ceiling to "
            f"continue: send budget_usd above ${spend:.4f}, or null for no ceiling at all. "
            "These calls run on a subscription, so that figure is API-equivalent telemetry "
            "rather than money."
        )


def _force_stop(
    store: RunStore, run: Mapping[str, Any], *, action: str = "force_stop"
) -> dict[str, Any]:
    """Verify the pid, kill the tree, then write the ending the process will never write.

    Verification is two questions, because neither answers it alone: is that pid a Python
    process (`terminate_pid_tree`), and is this run still writing the heartbeat that vouches
    for it (`_pid_is_current`)? This application runs under `python.exe` too, so a recycled
    pid could pass the first check and `taskkill /T` would take the backend down with it.

    The control flag is set *first*: if the kill is refused, some process is still out there,
    and a flag a live supervisor will honour at its next boundary is a better outcome than a
    lifecycle that claims it stopped.
    """
    run_id = UUID(str(run["id"]))
    previous = str(run["lifecycle"])
    store.set_control(run_id, action)

    pid = run.get("supervisor_pid")
    outcome = "no_pid"
    if pid:
        supervisor_alive = _supervisor_alive(int(pid), run_id=run_id)
        if supervisor_alive and not _pid_is_current(run):
            # Something is running under that pid, and nothing has written this run's
            # heartbeat in long enough that the pid is no longer evidence of what.
            outcome = "refused"
        elif supervisor_alive:
            outcome = terminate_pid_tree(int(pid), expected_images=tuple(SUPERVISOR_IMAGES))
        elif _process_alive(int(pid)):
            # On Linux the exact argv check can prove this is another Python process even
            # though the image allowlist alone would accept it. PID reuse must refuse.
            outcome = "refused"
        else:
            outcome = "vanished"
    log.info("run %s: force stop of pid %s -> %s", run_id, pid, outcome)

    if outcome == "refused":
        # Either someone else owns that pid now, or we cannot prove we still do. Never emit
        # here: a live supervisor may still be the single writer of this run's events.
        return {"accepted": True, "lifecycle": previous}

    # The supervisor never reached `_finish`, so nothing wrote the report and nothing would
    # ever have said why. A run that reads `stopped` with no deliverable and no explanation
    # is the same blank the budget path used to leave.
    events = EventWriter(store, run_id)
    state = dict(run.get("engine_state") or {})
    if not str(state.get("overview_md") or "").strip():
        reason = "force_stopped"
        detail = (
            "the run process was killed before it could write its report; everything it "
            "produced is in the Hypotheses tab"
        )
        store.set_engine_state(
            run_id, {"overview_skipped_reason": reason, "ended_reason": reason}
        )
        events.emit(EventType.REPORT_SKIPPED, {"reason": reason, "detail": detail})
    store.set_lifecycle(run_id, "stopped")
    store.set_control(run_id, None)
    events.emit(
        EventType.LIFECYCLE_CHANGED, {"lifecycle": "stopped", "previous": previous}
    )
    return {"accepted": True, "lifecycle": "stopped"}


def halt_all(store: RunStore, *, settings: Settings | None = None) -> dict[str, Any]:
    """The kill switch: stop every run that is still live, and kill every recorded pid.

    Deliberately blunt. It is the button for the moment something is wrong and the cost of
    stopping a healthy run is nothing next to the cost of not stopping the other one.
    """
    reconcile_runs(store)
    stopped: list[str] = []
    for run in store.active_runs():
        try:
            _force_stop(store, run, action="stop")
        except Exception:  # noqa: BLE001 — one stubborn run must not spare the rest
            log.exception("halt-all could not stop run %s", run["id"])
            continue
        stopped.append(str(run["id"]))
    log.warning("halt-all stopped %d run(s)", len(stopped))
    return {"stopped": stopped}


def quiesce_supervisors(
    store: RunStore,
    *,
    grace_seconds: float,
    poll_interval: float = 0.1,
) -> dict[str, list[str]]:
    """Park app-owned runs before a managed container shutdown.

    Each supervisor first gets the ordinary cooperative pause request. At the deadline,
    any process still working is terminated through the same verified tree-kill path as
    force-stop. Its committed units remain in Postgres and the run lands in ``paused``, so
    the normal Resume control restarts it from the last safe unit boundary.

    This function is opt-in at the application lifespan. Workstation API restarts continue
    to leave detached supervisors alone.
    """
    targets: set[UUID] = set()
    for run in store.active_runs(lifecycles=LANE_LIFECYCLES):
        if str(run.get("source") or "app") != "app":
            continue
        run_id = UUID(str(run["id"]))
        targets.add(run_id)
        if run.get("control_requested") not in ("stop", "finish"):
            store.set_control(run_id, "pause")

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        working = False
        for run_id in targets:
            current = store.get_run(run_id)
            if current is None or current["lifecycle"] not in LANE_LIFECYCLES:
                continue
            pid = _optional_pid(current.get("supervisor_pid"))
            if pid is not None and _process_alive(pid):
                working = True
                break
        if not working:
            break
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    parked: list[str] = []
    refused: list[str] = []
    for run_id in targets:
        current = store.get_run(run_id)
        if current is None or current["lifecycle"] not in LANE_LIFECYCLES:
            continue
        pid = _optional_pid(current.get("supervisor_pid"))
        if pid is not None and _supervisor_alive(pid, run_id=run_id):
            if not _pid_is_current(current):
                refused.append(str(run_id))
                continue
            outcome = terminate_pid_tree(pid, expected_images=tuple(SUPERVISOR_IMAGES))
            if outcome == "refused":
                refused.append(str(run_id))
                continue
        elif pid is not None and _process_alive(pid):
            # The pid exists but its exact supervisor argv does not match this run. It may
            # be a just-spawned process before exec, or a recycled pid; neither is safe to
            # kill or declare parked.
            refused.append(str(run_id))
            continue

        parked_run = store.park_for_shutdown(run_id, expected_pid=pid)
        if not parked_run["changed"]:
            continue
        previous = str(parked_run["previous"])
        EventWriter(store, run_id).emit(
            EventType.LIFECYCLE_CHANGED,
            {"lifecycle": "paused", "previous": previous, "reason": "service_shutdown"},
        )
        parked.append(str(run_id))

    log.info(
        "managed shutdown parked %d run(s); refused %d unverified pid(s)",
        len(parked),
        len(refused),
    )
    return {"parked": parked, "refused": refused}


def _optional_pid(value: Any) -> int | None:
    return int(value) if value else None


# ---------------------------------------------------------------------------- reconciler


def reconcile_runs(
    store: RunStore,
    *,
    stale_after: float = STALE_HEARTBEAT_SECONDS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Mark runs whose supervisor died as `lost`. Returns what it changed.

    Both conditions are required: a stale heartbeat *and* a dead pid. A supervisor blocked
    on a seven-minute grounded call has a stale heartbeat and is perfectly healthy, and
    marking that run lost would free a lane the live process is still using.

    `paused` runs are deliberately out of scope — they have no supervisor by design.
    """
    moment = now or datetime.now(UTC)
    lost: list[dict[str, Any]] = []

    for run in store.active_runs(lifecycles=LANE_LIFECYCLES):
        age = _age_seconds(run, moment)
        if age is not None and age <= stale_after:
            continue
        pid = run.get("supervisor_pid")
        run_id = UUID(str(run["id"]))
        if pid and _supervisor_alive(int(pid), run_id=run_id):
            continue

        error = {
            "type": "supervisor_lost",
            "message": (
                f"No heartbeat for {age:.0f}s and pid {pid or 'unknown'} is not running."
                if age is not None
                else f"No heartbeat recorded and pid {pid or 'unknown'} is not running."
            ),
            "where": "reconciler",
        }
        store.set_lifecycle(run_id, "lost", error=error)
        # Safe to emit: the reconciler only reaches this line once it has established that
        # no supervisor is alive, so there is no other writer to race with.
        EventWriter(store, run_id).emit(
            EventType.LIFECYCLE_CHANGED,
            {"lifecycle": "lost", "previous": run["lifecycle"], "reason": error["message"]},
        )
        log.warning("run %s marked lost: %s", run_id, error["message"])
        lost.append({"run_id": str(run_id), "previous": run["lifecycle"], "pid": pid})

    return lost


def _age_seconds(run: Mapping[str, Any], now: datetime) -> float | None:
    """Seconds since this run last showed a sign of life, counting from creation if never."""
    stamp = _parse(run.get("heartbeat_at")) or _parse(run.get("created_at"))
    if stamp is None:
        return None
    return (now - stamp).total_seconds()


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _pid_is_current(run: Mapping[str, Any], *, now: datetime | None = None) -> bool:
    """Is this run's recorded pid still vouched for by a recent heartbeat?

    The other half of the plan's verify-before-kill, and the half that actually identifies
    the process: the image check proves the pid belongs to *a* Python program, this proves
    something is still writing liveness to *this run's* row.
    """
    age = _age_seconds(run, now or datetime.now(UTC))
    if age is None:
        return False
    if age > PID_TRUST_SECONDS:
        log.error(
            "refusing to kill pid %s for run %s: its heartbeat is %.0fs old, so the pid "
            "is no longer proof of anything",
            run.get("supervisor_pid"),
            run.get("id"),
            age,
        )
        return False
    return True


def _supervisor_alive(pid: int, *, run_id: UUID | None = None) -> bool:
    """True when that pid is an Oracle supervisor, optionally for this exact run."""
    if os.name == "nt":
        image = process_image(pid)
        return image is not None and image.lower() in SUPERVISOR_IMAGES
    return _posix_supervisor_matches(pid, run_id=run_id)


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        return process_image(pid) is not None
    try:
        stat = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        return stat.rsplit(")", 1)[1].strip().split(maxsplit=1)[0] != "Z"
    except (OSError, IndexError):
        return False


def _posix_supervisor_matches(
    pid: int, *, run_id: UUID | None, proc_root: Path = Path("/proc")
) -> bool:
    """Identify a live Linux supervisor by argv, rejecting zombies and PID reuse.

    A container restart can assign an old supervisor's numeric pid to the new API process
    (often pid 1). Merely checking ``kill(pid, 0)`` then keeps the old run's lane wedged
    forever. ``/proc`` is available on the Oracle Linux deployment target and lets the
    reconciler prove both the module entry point and, when supplied, the run UUID.
    """
    process_dir = proc_root / str(pid)
    try:
        stat = (process_dir / "stat").read_text(encoding="utf-8")
        # Field 2 (comm) is parenthesized and may contain spaces. Everything after its last
        # closing parenthesis begins with field 3, the one-character process state.
        state = stat.rsplit(")", 1)[1].strip().split(maxsplit=1)[0]
        if state == "Z":
            return False
        argv = [
            item.decode("utf-8", errors="replace")
            for item in (process_dir / "cmdline").read_bytes().split(b"\0")
            if item
        ]
    except (OSError, IndexError):
        return False

    try:
        module_index = argv.index("-m")
        run_index = argv.index("--run-id")
    except ValueError:
        return False
    if module_index + 1 >= len(argv) or argv[module_index + 1] != "app.engine.supervisor":
        return False
    if run_index + 1 >= len(argv):
        return False
    return run_id is None or argv[run_index + 1] == str(run_id)
