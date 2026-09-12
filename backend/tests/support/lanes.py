"""Keep one module's abandoned run from wedging the next module's harness lane.

The lane is a partial unique index: at most one run per harness may sit in `queued`,
`running`, `pausing`, `stopping` or `finishing`. That constraint is load-bearing — it is
what stops two supervisors spending against the same account — so it is never relaxed for
tests. What the suite needs instead is for every test to leave the lanes as it found them.

A test that deliberately ends with a run still queued (an engine run refused before its
first call, say) is asserting something true and should keep doing so. It just has to hand
the lane back afterwards, because the whole session shares one throwaway schema and the
module that runs next will try to launch into it.

`release_lanes` is the hand-back: it parks live runs in a terminal lifecycle and touches
nothing else about them. Called at teardown by the modules that leave lanes held, and again
at setup by the ones that need them free, so no test depends on the order pytest chose.
"""

from __future__ import annotations

import os
from uuid import UUID

from app.engine.store import LIVE_LIFECYCLES, RunStore

__all__ = ["release_lanes"]


def release_lanes(store: RunStore, *, skip_live_processes: bool = False) -> list[str]:
    """Park every live run in `stopped` so its harness lane is free. Returns what moved.

    With `skip_live_processes`, a run whose recorded pid is still a running Python process
    is left alone — that is a supervisor from an earlier test on its way out, and stopping
    its run underneath it would surface as a failure in a test that has not begun. At
    teardown the caller wants the lane back regardless, so the default releases everything.
    """
    # Imported here rather than at module scope because `controls` imports the launcher,
    # which imports the store: at import time this module is loaded by conftest first.
    from app.services.runs.controls import _supervisor_alive  # noqa: PLC0415

    released: list[str] = []
    for run in store.active_runs(lifecycles=tuple(sorted(LIVE_LIFECYCLES))):
        pid = run.get("supervisor_pid")
        if skip_live_processes and pid and int(pid) != os.getpid() and _supervisor_alive(int(pid)):
            continue
        store.set_lifecycle(UUID(str(run["id"])), "stopped")
        released.append(str(run["id"]))
    return released
