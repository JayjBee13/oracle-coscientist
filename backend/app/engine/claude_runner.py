"""The Claude Code CLI as a stateless function call, one process per role call.

The model does not drive this system. It is asked one question, with one set of tools, and
its answer is parsed by code that assumes nothing — that is the whole architecture, and this
module is where it is enforced. Plan C1 fixes the invocation; the parts that are easy to get
subtly wrong, and why they are the way they are:

* **`--tools` is availability, `--allowedTools` is permission.** A grounded call with only
  the first is denied after burning ~59K tokens on the prompt. Both flags are set from the
  same string, and a denial is *never* answered by weakening the permission mode.
* **`--system-prompt` replaces, not appends.** The default prompt puts the model in a
  coding-agent register and costs ~7K tokens a call; a hypothesis generator has no use for
  either.
* **The prompt goes on stdin.** argv is public, size-capped, and shell-mangled on Windows.
* **Host config is suppressed twice, because one flag does not cover it.** `--setting-sources
  ""` drops the settings files; MCP servers live somewhere else entirely and need
  `--strict-mcp-config`. Without it the operator's own claude.ai connectors turned up in
  roughly half of all calls — an engine role has no business holding somebody's Gmail.
* **The invariants are asserted on every call, from the `system/init` envelope.** The CLI
  ignores flags it does not recognise, silently. A flag rename in a future release would
  otherwise re-enable MCP servers, host settings or slash commands with no error anywhere —
  so we do not trust that our flags were understood, we check what the CLI reports back.
* **The result comes from `structured_output`, never the `result` string.** The schema is
  enforced by the CLI; re-parsing prose is what made the archived runs undebuggable. The
  assistant's own `StructuredOutput` tool_use block carries the identical object and is
  the fallback when the result line never arrives — see `_structured`.
* **A deadline covers the whole call and moves when the answer lands.** Spawn has its own
  short ceiling, the stream reader cannot outlive the child, and submitting the structured
  answer buys a fixed grace window for the CLI's wrap-up. Every one of those three was a
  way for the recorded duration and the enforced ceiling to disagree.

Failures are returned, not raised: a `RoleResult` with `error` set and usage still filled in,
because those tokens were spent whatever the model did with them — accumulated per turn, so
that stays true for a call that was killed before it could report. The two exceptions are a
budget refusal and a spawn refusal, which are ours, not the model's, and belong in a
traceback.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import logging
import shutil
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.engine.models import Substitution, canonical_family, check_substitution, reported_models
from app.engine.runners import TIMEOUT_WARN_FRACTION, RoleConfig, RoleResult, Usage
from app.engine.spawn import (
    SpawnedProcess,
    kill_process_tree,
    probe_version,
    resolve_claude_exe,
    spawn_role_process,
)

__all__ = [
    "FORBIDDEN_FLAGS",
    "ClaudeCliRunner",
    "build_claude_argv",
]

log = logging.getLogger(__name__)

FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        "--bare",  # refuses subscription OAuth outright
        "--add-dir",  # June 2026: this is how a run mutated another run's state
        "--dangerously-skip-permissions",
        "--permission-mode",  # only ever wanted for bypassPermissions; do not start
        "--fallback-model",  # a silent downgrade mid-tournament poisons Elo
        "--append-system-prompt",
    }
)

TOOL_WEB_SEARCH = "WebSearch"

STRUCTURED_OUTPUT_TOOL = "StructuredOutput"
"""`--json-schema` is implemented as an internal tool, and init reports it as one.

Verified live on 2026-08-01, CLI 2.1.220: a call with `--tools ""` and a schema reports
`tools: ["StructuredOutput"]`. It is the schema mechanism itself, not a capability that
reaches the machine, so it is permitted exactly when a schema was asked for — and a call
without a schema that somehow has it is still a violation.
"""

MAX_RAW_LINES = 25
MAX_LINE_CHARS = 2_000
MAX_RAW_TAIL_CHARS = 6_000
_READ_CHUNK = 65_536

SPAWN_TIMEOUT_S = 30.0
"""Ceiling on process creation alone.

`asyncio.wait_for` used to wrap only the stream consumption, so the budget round trip and
`create_subprocess_exec` were outside the deadline entirely — which is how run c4566ed2
recorded a 257-second call against a 180-second ceiling. The interval that is enforced and
the interval that is recorded now measure the same thing.
"""

STRUCTURED_GRACE_S = 90.0
"""Extra time granted the moment the model submits its structured answer.

At that point the model is done and only the CLI's own wrap-up remains — writing the
result envelope, closing the session. Three of c4566ed2's ten timeouts were killed with a
complete `StructuredOutput` payload already in the transcript; the deadline landed on the
CLI finalising, and a finished answer was thrown away as a transport failure.
"""

EXIT_DRAIN_S = 2.0
"""How long the stdout reader is given after the child has exited.

`_read_stdout` blocks until the pipe reaches EOF, which never happens if a grandchild
inherited the handle. Two ranking calls in c4566ed2 sat on a pipe belonging to a process
that had *already exited* for the full 180-second ceiling (recorded outcome `exited`, not
`killed`). The reader now races the child's exit and gets this long to finish the tail.
"""

_EXIT_POLL_S = 0.1


async def _exited(process: asyncio.subprocess.Process) -> None:
    """Resolve when the child's exit status is known, whatever its pipes are doing."""
    while process.returncode is None:
        await asyncio.sleep(_EXIT_POLL_S)


def build_claude_argv(cfg: RoleConfig, session_id: str, *, exe: Path | str) -> list[str]:
    """Plan C1's argv, flag for flag. The only place a claude command line is composed."""
    argv = [
        str(exe),
        "-p",
        "--input-format",
        "text",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        canonical_family(cfg.model),
        "--effort",
        cfg.effort,
        "--system-prompt",
        cfg.system_prompt,
        "--tools",
        cfg.tools,
        "--disable-slash-commands",
        "--setting-sources",
        "",
        # MCP servers do not come from the settings sources, so suppressing those does not
        # suppress them: the host's claude.ai connectors reached calls anyway, about half
        # the time (verified 2026-08-02 — six servers, intermittently, which is worse than
        # always). With no `--mcp-config` to name, this flag means "no MCP servers at all".
        "--strict-mcp-config",
        "--no-session-persistence",
        "--session-id",
        session_id,
    ]
    if cfg.tools:
        # Availability is not permission. Both, or the call is denied after the prompt.
        argv += ["--allowedTools", cfg.tools]
    if cfg.json_schema is not None:
        argv += ["--json-schema", json.dumps(cfg.json_schema)]
    _assert_argv_law(argv)
    return argv


def _assert_argv_law(argv: Sequence[str]) -> None:
    """Refuse an argv that breaks a rule we already paid to learn.

    Only elements that look like flags are inspected, so a system prompt that happens to
    mention one of these words is not mistaken for passing it.
    """
    flags = {item for item in argv if item.startswith("--")}
    forbidden = flags & FORBIDDEN_FLAGS
    if forbidden:
        raise ValueError(f"argv contains forbidden flag(s): {sorted(forbidden)}")
    if "--system-prompt" not in flags:
        raise ValueError("argv must replace the system prompt, not inherit the default one")
    if "--tools" not in flags:
        raise ValueError("argv must state --tools explicitly, even when it is empty")
    if "--strict-mcp-config" not in flags:
        raise ValueError(
            "argv must pass --strict-mcp-config: --setting-sources does not suppress MCP "
            "servers, and the host's connectors otherwise reach the call"
        )

    tools = _flag_value(argv, "--tools")
    allowed = _flag_value(argv, "--allowedTools")
    if tools and allowed != tools:
        raise ValueError(
            f"--allowedTools must mirror --tools ({tools!r}), got {allowed!r}; "
            "without it every grounded call is denied"
        )
    if not tools and allowed is not None:
        raise ValueError("--allowedTools without --tools grants nothing and hides the intent")


def _flag_value(argv: Sequence[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


# ------------------------------------------------------------------------------ trace


@dataclass
class _Trace:
    """What one call's stream said, accumulated as it arrives.

    Owned by `run_role` rather than by the reader, so a timeout still has the partial
    transcript to report — the whole point of keeping it.
    """

    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_RAW_LINES))
    tool_uses: dict[str, int] = field(default_factory=dict)
    init: dict[str, Any] | None = None
    init_error: str | None = None
    result: dict[str, Any] | None = None
    rate_limited: bool = False
    garbage: int = 0
    envelopes: int = 0
    stderr: str = ""
    exit_code: int | None = None

    structured: dict[str, Any] | None = None
    """The schema payload, read off the assistant's `StructuredOutput` tool_use block.

    The same object the result envelope would have carried, seen the moment the model
    submits it rather than when the CLI finishes wrapping up. Kept because those two
    moments are minutes apart on a grounded call and the deadline used to land between
    them."""

    structured_at: float | None = None
    """`time.monotonic()` when the answer above arrived. Starts the grace window."""

    turn_tokens: dict[str, int] = field(
        default_factory=lambda: {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
    )
    """Usage accumulated per assistant turn, for the calls that never reach a result line.

    The module docstring has always claimed a failed call reports its usage "because those
    tokens were spent whatever the model did with them". Reading it only off the terminal
    envelope made that false for exactly the expensive failures: 13 of 30 calls in
    c4566ed2 wrote zero tokens and a NULL cost after burning 65 minutes of Opus time."""

    turns_seen: int = 0

    def note(self, line: str) -> None:
        self.lines.append(line[:MAX_LINE_CHARS])

    def add_usage(self, usage: Mapping[str, Any]) -> None:
        for key in self.turn_tokens:
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.turn_tokens[key] += int(value)
        self.turns_seen += 1


def _expected_tools(cfg: RoleConfig) -> set[str]:
    return {part.strip() for part in cfg.tools.split(",") if part.strip()}


def _check_init(envelope: dict[str, Any], cfg: RoleConfig) -> str | None:
    """The per-call invariants from plan C1. Returns a description of the first violation.

    This is the only real gate on host-config suppression: `--setting-sources ""` is a flag
    the CLI would ignore in silence if it were ever renamed, and the blast radius of that
    silence is the whole June 2026 incident.
    """
    servers = envelope.get("mcp_servers")
    if servers:
        names = [
            server.get("name", server) if isinstance(server, dict) else server
            for server in servers
        ]
        return f"mcp_servers is not empty: {names}"

    commands = envelope.get("slash_commands") or []
    if len(commands) != 0:
        return f"{len(commands)} slash command(s) are loaded; expected none"

    mode = envelope.get("permissionMode")
    if mode == "bypassPermissions":
        return "permissionMode is bypassPermissions"

    reported = {tool for tool in (envelope.get("tools") or []) if isinstance(tool, str)}
    expected = _expected_tools(cfg)
    permitted = expected | (
        {STRUCTURED_OUTPUT_TOOL} if cfg.json_schema is not None else set()
    )
    extra = reported - permitted
    if extra:
        return (
            f"tools {sorted(extra)} are available and were not asked for; "
            f"this call requested {sorted(expected) or 'no tools at all'}"
        )
    missing = expected - reported
    if missing:
        return (
            f"tools {sorted(missing)} were requested but are not available; "
            "the call cannot do the job it was given"
        )
    return None


def _read_assistant(envelope: dict[str, Any], trace: _Trace) -> None:
    """Everything one assistant turn tells us: tools used, tokens spent, answer submitted.

    The tool count is the only honest measure of whether a grounded role searched. The
    result envelope's `usage.server_tool_use.web_search_requests` counts *server-side* tool
    use; the CLI runs WebSearch on this machine, so that number stays 0 no matter how many
    searches happened (verified 2026-08-02: three WebSearch calls with results, counter 0).
    Believing it would mean concluding that grounding is broken when it is working.

    The other two readings exist because a call can be killed after the model has finished
    and before the CLI has said so. `StructuredOutput`'s `input` *is* the schema payload —
    it is not a hint that one is coming — and the per-turn `usage` is the only account of
    a call that never reaches its result line.
    """
    message = envelope.get("message")
    if not isinstance(message, dict):
        return
    usage = message.get("usage")
    if isinstance(usage, dict):
        trace.add_usage(usage)
    content = message.get("content")
    if not isinstance(content, list):  # plain-text turns carry a string
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "unknown")
        trace.tool_uses[name] = trace.tool_uses.get(name, 0) + 1
        if name != STRUCTURED_OUTPUT_TOOL:
            continue
        payload = block.get("input")
        if isinstance(payload, dict):
            trace.structured = payload
            trace.structured_at = time.monotonic()


def _is_rate_limit(envelope: dict[str, Any]) -> bool:
    """Recognise the backpressure envelope without pinning one shape of it.

    Observed live as a top-level `rate_limit_event`; the CLI has moved this kind of thing
    between top level and `system` subtypes before, so both count, and a status that says
    the limit is not currently biting does not.
    """
    marker = "rate_limit" in str(envelope.get("type", "")) or "rate_limit" in str(
        envelope.get("subtype", "")
    )
    if not marker and "rate_limit" not in envelope:
        return False
    payload = envelope.get("rate_limit") or envelope.get("rate_limit_event") or envelope
    status = payload.get("status") if isinstance(payload, dict) else None
    return str(status).lower() not in {"allowed", "ok", "none"}


# ----------------------------------------------------------------------------- runner


SpawnFn = Callable[..., Awaitable[SpawnedProcess]]


class ClaudeCliRunner:
    """`AgentRunner` backed by the real CLI. One process per call, nothing shared.

    `budget_guard` is required, not optional: it is the cap that survives an orchestrator
    bug, and a runner constructed without one would look identical right up until the run
    that could not stop. Wire `RunStore.check_budget` into it.
    """

    name = "claude"

    def __init__(
        self,
        *,
        workdir: Path | str,
        budget_guard: Callable[[], None],
        exe: Path | str | None = None,
        argv_log: Path | str | None = None,
        spawn: SpawnFn = spawn_role_process,
        keep_scratch: bool = False,
    ) -> None:
        self._workdir = Path(workdir).resolve()
        self._budget_guard = budget_guard
        self._exe = Path(exe).resolve() if exe else resolve_claude_exe()
        self._argv_log = Path(argv_log) if argv_log else self._workdir / "spawn-argv.jsonl"
        self._spawn = spawn
        self._keep_scratch = keep_scratch

    # --- protocol ---------------------------------------------------------------------

    async def run_role(self, role: str, prompt: str, cfg: RoleConfig) -> RoleResult:
        if role != cfg.role:
            raise ValueError(f"role {role!r} does not match its config ({cfg.role!r})")

        session_id = str(uuid4())
        argv = build_claude_argv(cfg, session_id, exe=self._exe)
        scratch = self._scratch(session_id)
        trace = _Trace()
        started = time.monotonic()

        try:
            try:
                spawned = await asyncio.wait_for(
                    self._spawn(
                        argv,
                        cwd=scratch,
                        workdir_root=self._workdir,
                        stdin_text=f"ROLE: {cfg.role}.\n\n{prompt}",
                        budget_guard=self._budget_guard,
                        argv_log=self._argv_log,
                    ),
                    # Spawn gets its own, much shorter deadline. It is a database round
                    # trip and a CreateProcess; anything that stalls there is a wedged box,
                    # not a model thinking, and charging it to the model's budget is how a
                    # 180-second ceiling produced a 257-second ledger row.
                    timeout=SPAWN_TIMEOUT_S,
                )
            except TimeoutError:
                return self._result(
                    cfg,
                    trace,
                    error=f"spawn timed out after {SPAWN_TIMEOUT_S:.0f}s",
                    elapsed=time.monotonic() - started,
                )
            try:
                await self._consume_within(spawned.process, cfg, trace, started=started)
            except TimeoutError:
                outcome = await kill_process_tree(
                    spawned.process, expected_images=(spawned.image,)
                )
                elapsed = time.monotonic() - started
                if trace.structured is not None:
                    # The model finished; the kill landed on the CLI's wrap-up. Throwing
                    # the answer away here is what left run c4566ed2 with no evolution
                    # children, no lineage and no descent edges at all.
                    log.warning(
                        "%s call was killed after %.0fs with its answer already submitted; "
                        "keeping it (%s)",
                        cfg.role,
                        elapsed,
                        outcome,
                    )
                    return self._result(cfg, trace, elapsed=elapsed, salvaged=outcome)
                log.warning(
                    "%s call timed out after %.0fs (limit %.0fs, %s)",
                    cfg.role,
                    elapsed,
                    cfg.timeout_s,
                    outcome,
                )
                return self._result(
                    cfg,
                    trace,
                    # Measured, not configured. The old string asserted the ceiling even
                    # when the call had run 43% past it, which made the ledger disagree
                    # with itself.
                    error=(
                        f"timeout after {elapsed:.0f}s "
                        f"(limit {cfg.timeout_s:.0f}s, {outcome})"
                    ),
                    elapsed=elapsed,
                )
            finally:
                await self._settle(spawned)

            if trace.init_error is not None:
                await kill_process_tree(spawned.process, expected_images=(spawned.image,))
                return self._result(
                    cfg,
                    trace,
                    error=f"init assertion failed: {trace.init_error}",
                    elapsed=time.monotonic() - started,
                )

            return self._result(cfg, trace, elapsed=time.monotonic() - started)
        finally:
            self._clean(scratch)

    async def probe(self) -> dict[str, Any]:
        result = await probe_version(self._exe)
        return {"name": self.name, **result}

    async def aclose(self) -> None:
        return None

    # --- process lifecycle ------------------------------------------------------------

    def _scratch(self, session_id: str) -> Path:
        """An empty directory per call, under the run workdir and nowhere else."""
        scratch = self._workdir / "calls" / session_id
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def _clean(self, scratch: Path) -> None:
        if self._keep_scratch:
            return
        shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    async def _settle(spawned: SpawnedProcess) -> None:
        """Let the stdin writer finish or drop it; never leave the task dangling.

        Also releases the pipe transports of a child that has already exited. Normally
        they close themselves when every pipe disconnects — but the case this exists for is
        precisely the one where a grandchild holds a pipe open, and an abandoned reader
        would otherwise be reclaimed by the garbage collector at an arbitrary later moment.
        A live child is left alone: the caller may still be about to kill it, and closing
        the transport takes the pid with it.
        """
        task = spawned.stdin_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        process = spawned.process
        transport = getattr(process, "_transport", None)
        if transport is None or process.returncode is None:
            return
        try:
            transport.close()
        except Exception:  # noqa: BLE001 — housekeeping must not fail a completed call
            log.debug("could not close the subprocess transport", exc_info=True)

    async def _consume_within(
        self,
        process: asyncio.subprocess.Process,
        cfg: RoleConfig,
        trace: _Trace,
        *,
        started: float,
    ) -> None:
        """Consume the stream under a deadline that moves once the answer has landed.

        `asyncio.wait_for` cannot express this: the deadline is not known when the wait
        begins. Until the model submits its `StructuredOutput` the ceiling is the role's;
        from that moment the only outstanding work is the CLI's own finalisation, so the
        deadline becomes a short fixed grace instead of a hard stop on a completed answer.

        Raises `TimeoutError` so the caller's existing kill-and-report path is unchanged.
        """
        task = asyncio.create_task(self._consume(process, cfg, trace))
        try:
            while True:
                deadline = started + cfg.timeout_s
                if trace.structured_at is not None:
                    deadline = max(deadline, trace.structured_at + STRUCTURED_GRACE_S)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait({task}, timeout=remaining)
                if done:
                    await task  # re-raise whatever the reader hit
                    return
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _consume(
        self, process: asyncio.subprocess.Process, cfg: RoleConfig, trace: _Trace
    ) -> None:
        stderr_task = asyncio.create_task(self._read_stderr(process, trace))
        try:
            await self._read_stdout_or_exit(process, cfg, trace)
            if trace.init_error is None:
                # stdout is closed; give stderr a moment to reach its own EOF so a crash
                # message that arrived last still makes it into the diagnosis.
                await asyncio.wait({stderr_task}, timeout=EXIT_DRAIN_S)
        finally:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        if trace.init_error is None:
            trace.exit_code = await self._exit_code(process)

    async def _read_stdout_or_exit(
        self, process: asyncio.subprocess.Process, cfg: RoleConfig, trace: _Trace
    ) -> None:
        """Read stdout, but never outlive the child by more than `EXIT_DRAIN_S`.

        `_read_stdout` returns when the pipe reaches EOF. A pipe handle inherited by a
        grandchild does not reach EOF when our child dies, so the reader waits on a dead
        process for the whole ceiling — twice in run c4566ed2, on a tool-less role, for
        257 seconds against a 180-second limit and an outcome of `exited`.

        The race is against `_exited`, not against `process.wait()`, and the difference is
        the whole point: `BaseSubprocessTransport._wait` only resolves once *every* pipe
        has disconnected, so waiting on it in this situation hangs for exactly as long as
        the reader does. `returncode` is set from the process handle itself and is
        therefore the one signal a held-open pipe cannot suppress.
        """
        reader = asyncio.create_task(self._read_stdout(process, cfg, trace))
        waiter = asyncio.create_task(_exited(process))
        try:
            await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not reader.done():
                await asyncio.wait({reader}, timeout=EXIT_DRAIN_S)
            if reader.done():
                await reader  # surface a reader exception rather than swallowing it
        finally:
            for task in (reader, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, waiter, return_exceptions=True)

    @staticmethod
    async def _exit_code(process: asyncio.subprocess.Process) -> int | None:
        """The child's exit status, without hanging on a pipe somebody else still holds."""
        if process.returncode is not None:
            return process.returncode
        try:
            return await asyncio.wait_for(process.wait(), timeout=EXIT_DRAIN_S)
        except TimeoutError:
            return None

    async def _read_stdout(
        self, process: asyncio.subprocess.Process, cfg: RoleConfig, trace: _Trace
    ) -> None:
        """Decode and dispatch stream-json lines as they arrive.

        Chunked reads with an incremental decoder rather than `readline()`: a single
        envelope can be megabytes (a long debate, a big schema echo), which trips
        `StreamReader`'s line limit, and a multi-byte character split across two reads must
        not become a decode error. `errors="replace"` means a mangled byte costs one
        character, not the call.
        """
        stream = process.stdout
        if stream is None:  # pragma: no cover — we always ask for a pipe
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                buffer += decoder.decode(b"", final=True)
                for line in buffer.splitlines():
                    if not self._handle(line, cfg, trace):
                        return
                return
            buffer += decoder.decode(chunk)
            *complete, buffer = buffer.split("\n")
            for line in complete:
                if not self._handle(line, cfg, trace):
                    return

    async def _read_stderr(
        self, process: asyncio.subprocess.Process, trace: _Trace
    ) -> None:
        """Drain stderr so a chatty child cannot block on a full pipe."""
        stream = process.stderr
        if stream is None:  # pragma: no cover
            return
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        collected: list[str] = []
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                collected.append(decoder.decode(b"", final=True))
                trace.stderr = "".join(collected).strip()[:MAX_RAW_TAIL_CHARS]
                return
            if len(collected) < 64:
                collected.append(decoder.decode(chunk))
                # Assigned as it arrives, not at EOF: a call that is killed for timing out
                # still reports whatever the child managed to complain about.
                trace.stderr = "".join(collected).strip()[:MAX_RAW_TAIL_CHARS]

    def _handle(self, line: str, cfg: RoleConfig, trace: _Trace) -> bool:
        """One stream line. Returns False to stop reading (an invariant was violated)."""
        stripped = line.strip()
        if not stripped:
            return True
        trace.note(stripped)
        try:
            envelope = json.loads(stripped)
        except ValueError:
            # Not JSON. A crash banner or a stray print — kept in the tail, not fatal.
            trace.garbage += 1
            return True
        if not isinstance(envelope, dict):
            trace.garbage += 1
            return True

        trace.envelopes += 1
        if _is_rate_limit(envelope):
            trace.rate_limited = True
            return True
        kind = envelope.get("type")
        if kind == "system" and envelope.get("subtype") == "init":
            trace.init = envelope
            trace.init_error = _check_init(envelope, cfg)
            if trace.init_error is not None:
                log.error("%s call refused: %s", cfg.role, trace.init_error)
                return False
            return True
        if kind == "assistant":
            _read_assistant(envelope, trace)
        if kind == "result":
            trace.result = envelope
        # Everything else — assistant, user, system/thinking_tokens, whatever ships next —
        # is transcript. Tolerating unknown envelopes is deliberate: a new event type must
        # not be able to fail a run.
        return True

    # --- result assembly --------------------------------------------------------------

    def _result(
        self,
        cfg: RoleConfig,
        trace: _Trace,
        *,
        error: str | None = None,
        elapsed: float,
        salvaged: str | None = None,
    ) -> RoleResult:
        envelope = trace.result or {}
        usage = _usage(envelope, trace, elapsed)
        denials = envelope.get("permission_denials") or []
        swap = check_substitution(cfg.model, envelope.get("modelUsage"))
        summary = _summary(cfg, trace, envelope, elapsed, swap)
        summary["structured_from_transcript"] = (
            trace.structured is not None and not isinstance(envelope.get("structured_output"), dict)
        )
        summary["usage_estimated"] = not envelope
        summary["near_timeout"] = elapsed >= TIMEOUT_WARN_FRACTION * cfg.timeout_s
        summary["timeout_s"] = cfg.timeout_s
        if salvaged is not None:
            # A completed answer recovered from a killed process. The call is a success —
            # the payload is the model's, on-schema, and nothing about it is partial — but
            # it did not complete on the terms it was asked on, which is what `degraded`
            # means everywhere else in this system.
            #
            # Success *if nothing else was wrong with it*. The kill excuses the missing
            # result envelope and nothing more: a result line that did arrive and said
            # `is_error`, a grounded call whose searches were denied, a transcript with no
            # `system/init` in it — each of those is still the reason it says it is, and the
            # timeout path must not be the one route by which they reach the pipeline as a
            # good answer.
            summary["salvaged_after_timeout"] = salvaged
            if error is None:
                error = _failure(cfg, trace, envelope, denials, salvaged=True)
            if error is None:
                return RoleResult(
                    role=cfg.role,
                    model=cfg.model,
                    data=dict(trace.structured or {}),
                    usage=usage,
                    raw_tail=_raw_tail(summary, trace),
                    telemetry=summary,
                    degraded=True,
                    rate_limited=trace.rate_limited,
                )
            # Otherwise fall through: an ordinary failure that happens to carry
            # `salvaged_after_timeout` in its telemetry, which is what happened to it.

        if error is None:
            error = _failure(cfg, trace, envelope, denials)

        # A downgrade below the provider's floor fails the call even when everything else
        # about it succeeded. The answer is well-formed and on-schema; it just came from a
        # judge the scientist did not choose, and accepting it would put two different
        # judges' verdicts into one Elo table with nothing in the artefacts to say which was
        # which. A model from *another provider entirely* — or from none this engine knows —
        # fails for a stronger reason: within Anthropic an unrecognised id is probably a
        # model newer than our table, but a name belonging to no known provider means this
        # invocation reached something we have no account of, and "no idea" must not be
        # rounded down to "probably fine".
        if error is None and swap is not None and swap.fatal:
            log.error("%s call was answered by the wrong model: %s", cfg.role, swap.detail)
            reason = (
                "from another provider" if swap.cross_provider else "below the Opus floor"
            )
            error = f"model substitution {reason}: {swap.detail}"

        data = None
        if error is None:
            data = _structured(cfg, envelope, trace)
            if data is None:
                error = (
                    "no structured_output in the result envelope; the schema was not honoured"
                )

        if swap is not None and not swap.fatal:
            # Sideways, not down — most likely `fable` resolving to Opus 5, the trap
            # `models.py` documents. Keep the answer, flag the run, and put both names in
            # telemetry so the Activity tab can say which model actually did the work.
            log.warning("%s call ran on a different model: %s", cfg.role, swap.detail)

        return RoleResult(
            role=cfg.role,
            model=cfg.model,
            data=data,
            error=error,
            usage=usage,
            raw_tail=_raw_tail(summary, trace),
            telemetry=summary,
            degraded=swap is not None,
            rate_limited=trace.rate_limited or "rate_limit" in str(envelope.get("terminal_reason")),
        )


def _failure(
    cfg: RoleConfig,
    trace: _Trace,
    envelope: dict[str, Any],
    denials: list[Any],
    *,
    salvaged: bool = False,
) -> str | None:
    """The first reason this call cannot be trusted, or None.

    `salvaged` says the process was killed at its ceiling with the model's structured answer
    already in the transcript. It suppresses one check and one only — the absent result
    envelope, which is the expected state of a CLI that was killed mid-wrap-up. Every other
    question is asked exactly as it would be of a call that finished on its own.
    """
    if not envelope and not salvaged:
        detail = trace.stderr or "no output"
        return (
            f"the CLI produced no result event (exit {trace.exit_code}): {detail[:400]}"
        )
    if trace.init is None:
        # A result without an init envelope means the invariants were never checked. That
        # is indistinguishable from them having been silently ignored, so it fails.
        return "no system/init envelope: the per-call invariants could not be verified"
    if envelope.get("is_error"):
        return _error_detail(envelope)
    subtype = envelope.get("subtype")
    if subtype not in (None, "success"):
        return _error_detail(envelope)
    if denials and cfg.grounded:
        # Verbatim, always: this is the failure that used to be invisible, and the fix is
        # always a wrong --allowedTools, never a weaker permission mode.
        return f"permission denied on a grounded call: {json.dumps(denials, ensure_ascii=False)}"
    if denials:
        log.warning(
            "%s is tool-less yet reported permission denials: %s", cfg.role, denials
        )
    return None


def _error_detail(envelope: dict[str, Any]) -> str:
    parts = [
        f"{key}={envelope[key]}"
        for key in ("subtype", "stop_reason", "terminal_reason", "api_error_status")
        if envelope.get(key) not in (None, "")
    ]
    text = str(envelope.get("result") or "").strip()
    if text:
        parts.append(f"result={text[:300]}")
    if not parts:
        return "the call failed"
    return "the call failed: " + ", ".join(parts)


def _structured(
    cfg: RoleConfig, envelope: dict[str, Any], trace: _Trace | None = None
) -> dict[str, Any] | None:
    """The parsed object the CLI produced for our schema.

    Never `result`: that is the assistant's prose rendering of the same thing, and parsing
    it is the habit this rebuild exists to break. A call configured without a schema is the
    only case where the text is the answer — no engine role is, but a one-off diagnostic
    call should not have to invent a schema to get an answer out.

    The transcript is the fallback, not a second source: the `StructuredOutput` tool_use
    block carries the identical object, and reading it costs nothing on a healthy call
    (the envelope wins) while saving the whole answer on a call whose result line never
    arrived.
    """
    structured = envelope.get("structured_output")
    if isinstance(structured, dict):
        return structured
    if structured is not None:
        return {"value": structured}
    if trace is not None and trace.structured is not None:
        return dict(trace.structured)
    if cfg.json_schema is None:
        text = envelope.get("result")
        return {"text": text} if isinstance(text, str) else None
    return None


def _usage(envelope: dict[str, Any], trace: _Trace, elapsed: float) -> Usage:
    """All four token classes. Counting `input` alone under-reports by ~700× with caching.

    Falls back to what the transcript accounted for when there is no result envelope. A
    killed call spent its tokens, and recording zero for it hid 43% of one run's calls from
    the dollar ceiling that was supposed to be governing it. `cost_usd` stays `None` in
    that case — the CLI is the only thing that prices a call, and inventing a figure would
    put a guess into the ledger — but the tokens are the truth we do have.
    """
    raw = envelope.get("usage") or {}
    if not raw and trace.turns_seen:
        raw = trace.turn_tokens
    cost = envelope.get("total_cost_usd")
    duration = envelope.get("duration_ms")
    return Usage(
        tokens_in=int(raw.get("input_tokens") or 0),
        tokens_out=int(raw.get("output_tokens") or 0),
        cache_creation=int(raw.get("cache_creation_input_tokens") or 0),
        cache_read=int(raw.get("cache_read_input_tokens") or 0),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        duration_ms=int(duration) if isinstance(duration, (int, float)) else int(elapsed * 1000),
    )


def _summary(
    cfg: RoleConfig,
    trace: _Trace,
    envelope: dict[str, Any],
    elapsed: float,
    swap: Substitution | None = None,
) -> dict[str, Any]:
    """Every result field plan C1 says to persist, in one compact object.

    This is what the Activity tab shows when a call goes wrong, and it is the whole
    diagnosis: subtype, stop reason, denials, turns, spend and where the stream stopped.
    """
    tools = envelope.get("usage", {}).get("server_tool_use") or {}
    return {
        "role": cfg.role,
        # Named on every call now that a run may mix providers step by step: "model=fable"
        # no longer tells a reader which CLI ran, and the two failure vocabularies are
        # different enough that it has to.
        "provider": "anthropic",
        "model": cfg.model,
        "effort": cfg.effort,
        # Claude's result envelope names what answered, so a Claude call's model is verified
        # against `modelUsage` on every call. Codex's stream names no model at all and its
        # telemetry says so — a reader must be able to tell the two apart from the payload.
        "model_verified": bool(reported_models(envelope.get("modelUsage"))),
        "tools": cfg.tools,
        "session_id": envelope.get("session_id") or (trace.init or {}).get("session_id"),
        "cli_version": (trace.init or {}).get("claude_code_version"),
        "is_error": envelope.get("is_error"),
        "subtype": envelope.get("subtype"),
        "stop_reason": envelope.get("stop_reason"),
        "terminal_reason": envelope.get("terminal_reason"),
        "api_error_status": envelope.get("api_error_status"),
        "permission_denials": envelope.get("permission_denials"),
        "num_turns": envelope.get("num_turns"),
        "modelUsage": envelope.get("modelUsage"),
        # Both names, always, so "requested X, ran Y" is renderable from telemetry alone
        # without the reader having to know how to read a modelUsage map. `model_ran` is
        # None when nothing contradicted the request — matched, absent, or housekeeping only.
        "model_requested": cfg.model,
        "model_ran": ", ".join(swap.ran) if swap else None,
        "model_substituted": swap is not None,
        "model_below_floor": bool(swap and swap.below_floor),
        "model_cross_provider": bool(swap and swap.cross_provider),
        "model_below_request": bool(swap and swap.below_request),
        "duration_ms": envelope.get("duration_ms"),
        "total_cost_usd": envelope.get("total_cost_usd"),
        # `web_searches` is counted from the transcript and is the one to trust;
        # `web_search_requests` is the server-side counter, which is always 0 here.
        "web_searches": trace.tool_uses.get(TOOL_WEB_SEARCH, 0),
        "tool_uses": dict(trace.tool_uses) or None,
        "web_search_requests": tools.get("web_search_requests"),
        "exit_code": trace.exit_code,
        "envelopes": trace.envelopes,
        "unparsable_lines": trace.garbage,
        "rate_limited": trace.rate_limited,
        "elapsed_s": round(elapsed, 2),
        "stderr": trace.stderr[:600] or None,
    }


def _raw_tail(summary: dict[str, Any], trace: _Trace) -> str:
    head = json.dumps(summary, ensure_ascii=False, default=str)
    tail = "\n".join(trace.lines)
    text = f"{head}\n--- last {len(trace.lines)} line(s) ---\n{tail}"
    return text[:MAX_RAW_TAIL_CHARS]
