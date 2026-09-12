"""The Codex CLI as a stateless function call, one process per role call.

The same architecture as `claude_runner`, against a CLI that is different in every detail
that matters. Everything below was pinned by a live probe on 2026-08-11 (codex-cli 0.146.0,
five real calls) rather than inferred from the Claude runner, because every place the two
CLIs *look* alike is a place this could have been written wrong and still passed review:

* **The binary is resolved past two shims, not one.** `codex.cmd` names `bin/codex.js`, and
  node then spawns the vendored `codex.exe`. Going through the shim yields
  `cmd.exe → node.exe → codex.exe` with `cmd.exe` staying resident as the parent — so the
  pid we would be handed is not the pid that is billing, and killing it orphans a running
  model turn. `spawn.resolve_codex_exe` finds the vendored binary by layout and we launch it
  directly; the pid we hold is then the one that matters. The tree kill stays anyway: codex
  ships sibling helpers (`codex-command-runner.exe`, `rg.exe`) that a tool-using turn spawns.
* **There is no effort flag, and no approval flag either.** Reasoning effort is a config
  override, `-c model_reasoning_effort=<level>`, and the vocabulary is enforced server-side:
  an unknown level is a 400 and a failed turn, not a warning. The level is therefore clamped
  to the model's own published ladder before it is sent (`models.resolve_effort`) and the
  clamp is reported as `effort_clamped`, never applied in silence. Approval has the same
  shape: `-a`/`--ask-for-approval` belongs to the *interactive* command only, and `codex
  exec` rejects it outright — `error: unexpected argument '-a' found`, exit 2, before a
  single token is spent. It travels as `-c approval_policy="never"` for the same reason the
  sandbox is pinned: a role call has no tty, so a prompt it cannot answer would hold it open
  until its ceiling.
* **The answer comes out of a file, never scraped from stdout.** `--output-schema <file>`
  plus `-o <file>`: the last-message file holds bare schema-conforming JSON. Its failure mode
  is the important part — on a failed turn the file is **not created at all**, so a missing
  or empty file is a hard failure and never an empty result.
* **Web search is a config toggle, not a flag.** `--search` exists only on the interactive
  command; under `exec` it is `-c tools.web_search=true`. It is set explicitly either way, so
  a grounded call that was not granted search is a refused invariant rather than a quiet
  ungrounded answer. Searches are counted from `web_search` items in the JSONL — never
  inferred from prose.
* **The host's own Codex config is suppressed, and this one is not optional.** The operator's
  `~/.codex/config.toml` sets `sandbox_mode = "danger-full-access"` and
  `approval_policy = "never"` globally, so without `-s read-only` every role call would
  inherit unsandboxed shell access. `--ignore-user-config` additionally keeps their model,
  effort, personality, service tier and `notify` hook (which fires an executable on every
  turn) out of the run; `--ignore-rules` skips execpolicy files; `--ephemeral` keeps the
  session transcript out of the operator's home directory.
* **Success is `turn.completed`, and stderr is not a failure signal.** A probe call exited 0
  while printing `ERROR codex_models_manager::cache: failed to load models cache` — benign
  version skew that self-healed. Keying off stderr would have failed a healthy call.
* **Nothing verifies which model answered, and that is stated rather than assumed.** Codex's
  JSONL names no model anywhere: there is no `modelUsage` equivalent, so
  `models.check_substitution` covers zero Codex calls and every call is marked
  `model_verified: False`. What replaces it is the closed allowlist checked before the spawn,
  a loud failure on an unknown id (exit 1, no last-message file), and the treatment of any
  `fallback metadata` error item as a refusal even when the turn later succeeds — that line
  means the client is running with another model's context window and tool shapes.

Failures are returned, not raised, exactly as in `claude_runner`. The one honest gap is
usage: a failed Codex turn carries no `usage` block at all, and `Usage`'s counters are ints
that the ledger sums, so they read 0 — `usage_reported: False` in telemetry is what says
that 0 is an absence rather than a measurement. Nothing should read those counters without
it.
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

from app.engine.models import (
    Substitution,
    check_substitution,
    resolve_effort,
    validate_model,
)
from app.engine.runners import TIMEOUT_WARN_FRACTION, RoleConfig, RoleResult, Usage
from app.engine.spawn import (
    SpawnedProcess,
    kill_process_tree,
    probe_version,
    resolve_codex_exe,
    spawn_role_process,
)

__all__ = [
    "FORBIDDEN_FLAGS",
    "FORBIDDEN_SANDBOXES",
    "CodexCliRunner",
    "build_codex_argv",
    "build_codex_stdin",
    "codex_output_schema",
]

log = logging.getLogger(__name__)

FORBIDDEN_FLAGS: frozenset[str] = frozenset(
    {
        # The one-flag route to the thing `-s read-only` exists to prevent.
        "--dangerously-bypass-approvals-and-sandbox",
        "--full-auto",
        # Interactive-only, and it would leave the call waiting on a tty that is not there.
        "--yolo",
        # Relocates config, sessions AND auth. `--ephemeral` is the supported way to keep a
        # call out of the operator's home directory; redirecting CODEX_HOME instead means
        # copying auth.json somewhere or running unauthenticated.
        "--cd-home",
    }
)

FORBIDDEN_SANDBOXES: frozenset[str] = frozenset({"workspace-write", "danger-full-access"})

SANDBOX = "read-only"

WEB_SEARCH_KEY = "tools.web_search"
EFFORT_KEY = "model_reasoning_effort"
APPROVAL_KEY = "approval_policy"

APPROVAL_POLICY = "never"
"""Mostly irrelevant under a read-only exec, and stated anyway: an approval request with no
tty to answer it would otherwise be able to hold a call open until its ceiling.

It is a **config override**, not a flag. `codex exec` has no `-a`/`--ask-for-approval` — that
option exists only on the interactive command, and passing it to `exec` is exit 2 with
`error: unexpected argument '-a' found` before the model is ever reached, which is how the
whole OpenAI lane came to die in 0.04s.
"""

APPROVAL_OVERRIDE = f'{APPROVAL_KEY}="{APPROVAL_POLICY}"'
"""The override as it is written on the command line, quotes included.

`-c` parses its value as TOML and falls back to the raw string only if that fails, so both
`approval_policy=never` and `approval_policy="never"` happen to arrive as the string
`never`. The quoted form is used because it is the one that parses: it is exactly what the
line reads in `~/.codex/config.toml`, whose value this overrides, so nothing here depends on
a fallback that exists for values TOML cannot express.
"""

SCHEMA_FILE = "output-schema.json"
LAST_MESSAGE_FILE = "last-message.txt"

MAX_RAW_LINES = 25
MAX_LINE_CHARS = 2_000
MAX_RAW_TAIL_CHARS = 6_000
_READ_CHUNK = 65_536

SPAWN_TIMEOUT_S = 30.0
"""Ceiling on process creation alone — a CreateProcess and a budget round trip. Charging a
wedged box to the model's clock is how a 180s ceiling produced a 257s ledger row."""

ANSWER_GRACE_S = 90.0
"""Extra time granted the moment the agent's message lands.

From that point the model is done and only the CLI's own wrap-up remains — writing the
last-message file, closing the thread. The Claude runner learned this the expensive way:
three of run c4566ed2's ten timeouts were killed with a complete answer already on the wire.
"""

EXIT_DRAIN_S = 2.0
_EXIT_POLL_S = 0.1

_UNSUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {"minItems", "maxItems", "minLength", "maxLength", "minProperties", "maxProperties",
     "pattern", "format", "minimum", "maximum", "multipleOf", "default"}
)
"""JSON Schema keywords a strict structured-output schema may not carry.

Dropped rather than refused: they are the *validator's* business, and this engine
re-validates every payload against the full schema on the way in (`validate_role_output`)
regardless of what the provider enforced. Sending them would fail the request outright and
buy nothing, because the check they express happens here either way.
"""


def codex_output_schema(schema: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A role schema rewritten for `--output-schema`, or None when it cannot be.

    Structured output is *strict*: every object must list all of its properties in
    `required` and set `additionalProperties: false`, and the validation keywords above are
    not accepted. Most role schemas satisfy that after a mechanical rewrite.

    One does not, and the exception is the point of the `None` return. `proximity` answers
    with an open map — hypothesis id → cluster label — whose keys are not knowable when the
    schema is written, and strict mode has no way to say "an object with arbitrary string
    keys". Rather than mangle that role into a shape it cannot answer in, the runner drops
    the flag for it and states the contract in the prompt instead, recording which of the two
    happened as `schema_mode` in telemetry. A quietly wrong schema would cost every clustering
    call in every Codex run; a stated fallback costs a line of telemetry.
    """
    if schema is None:
        return None
    try:
        return _strict(schema)
    except _NotStrictlyExpressible as exc:
        log.debug("schema is not expressible as strict structured output: %s", exc)
        return None


class _NotStrictlyExpressible(ValueError):
    """A schema construct strict structured output has no way to express."""


def _strict(node: Any) -> Any:
    if isinstance(node, list):
        return [_strict(item) for item in node]
    if not isinstance(node, Mapping):
        return node

    rewritten = {
        key: _strict(value)
        for key, value in node.items()
        if key not in _UNSUPPORTED_KEYWORDS
    }
    if rewritten.get("type") != "object":
        return rewritten

    properties = rewritten.get("properties")
    extra = rewritten.get("additionalProperties")
    if not isinstance(properties, Mapping) or not properties:
        raise _NotStrictlyExpressible("an object with no declared properties is an open map")
    if isinstance(extra, Mapping):
        raise _NotStrictlyExpressible("additionalProperties as a schema is an open map")

    # Every property required, which is what strict mode means by "the shape is fixed".
    # Safe here because the only role schema with a genuinely optional property is the one
    # this function refuses anyway — and a re-ask is cheaper than a field the model omitted.
    rewritten["required"] = list(properties)
    rewritten["additionalProperties"] = False
    return rewritten


FALLBACK_METADATA_MARKER = "fallback metadata"
"""The decoy first stage of an unknown-model failure.

Verbatim from the probe: `Model metadata for X not found. Defaulting to fallback metadata;
this can degrade performance and cause issues.` It is client-side metadata only — it does
not substitute a different model — but it means the turn is running with the wrong context
window and tool shapes, so it is a refusal even on a turn that goes on to succeed.
"""


async def _exited(process: asyncio.subprocess.Process) -> None:
    while process.returncode is None:
        await asyncio.sleep(_EXIT_POLL_S)


def build_codex_stdin(cfg: RoleConfig, prompt: str, *, schema_in_prompt: bool) -> str:
    """Everything the model is told, in the one channel Codex has for telling it.

    The Claude runner puts the role's instructions in `--system-prompt`, which *replaces* the
    CLI's default coding-agent persona. `codex exec` has no such flag, so the system prompt
    travels at the head of stdin instead. Dropping it — which is what "reuse the Claude
    runner's stdin" would have done — would send the role's prompt to a coding agent with no
    idea it is meant to be generating hypotheses, and the failure would look like a bad model
    rather than a missing instruction.

    `schema_in_prompt` covers the one role whose contract `--output-schema` cannot express
    (see `codex_output_schema`): the shape is stated in words instead, so the fallback is a
    different route to the same contract rather than a weaker one.
    """
    parts = [cfg.system_prompt.strip(), f"ROLE: {cfg.role}.", prompt]
    if schema_in_prompt and cfg.json_schema is not None:
        parts.append(
            "Reply with a single JSON object and nothing else — no prose, no code fence — "
            "conforming to this JSON Schema:\n"
            + json.dumps(cfg.json_schema, ensure_ascii=False)
        )
    return "\n\n".join(part for part in parts if part)


def build_codex_argv(
    cfg: RoleConfig,
    *,
    exe: Path | str,
    scratch: Path,
    effort: str | None = None,
    schema_file: bool = True,
) -> list[str]:
    """The one place a codex command line is composed.

    `effort` overrides the config's, for the caller that has already clamped it to the
    model's ladder — the argv builder must not clamp silently on its own, because the clamp
    has to be reported and this function has nowhere to report it.

    `schema_file` is False for a role whose schema cannot be expressed strictly; the flag is
    then omitted and the contract is stated in the prompt.
    """
    argv: list[str] = [
        str(exe),
        "exec",
        "--json",
        "-m",
        cfg.model,
        "-c",
        f"{EFFORT_KEY}={effort or cfg.effort}",
        # Availability stated either way. A grounded role with the toggle absent would
        # answer from memory and look identical to one that searched.
        "-c",
        f"{WEB_SEARCH_KEY}={'true' if cfg.tools else 'false'}",
        "-s",
        SANDBOX,
        # Approval is a config override, never a flag: `codex exec` has no `-a`, and passing
        # one is exit 2 before the model is reached.
        "-c",
        APPROVAL_OVERRIDE,
        # The operator's own config sets danger-full-access and a notify hook that runs an
        # executable on every turn. None of that belongs in a role call.
        "--ignore-user-config",
        "--ignore-rules",
        # No session transcript under the operator's home: a call writes inside its scratch
        # directory and nowhere else.
        "--ephemeral",
        # The scratch directory is not a git repository, and exec refuses to run in one
        # without this.
        "--skip-git-repo-check",
        "-C",
        str(scratch),
        "-o",
        str(Path(scratch) / LAST_MESSAGE_FILE),
    ]
    if cfg.json_schema is not None and schema_file:
        argv += ["--output-schema", str(Path(scratch) / SCHEMA_FILE)]
    # Last, and always: `-` reads the prompt from stdin. A prompt in argv would be public,
    # size-capped and shell-mangled — and passing both concatenates them, so `-` is what
    # makes the invocation unambiguous rather than merely correct.
    argv.append("-")
    _assert_argv_law(argv)
    return argv


def _assert_argv_law(argv: Sequence[str]) -> None:
    """Refuse an argv that breaks a rule the probe already paid to learn."""
    flags = {item for item in argv if item.startswith("--")}
    forbidden = flags & FORBIDDEN_FLAGS
    if forbidden:
        raise ValueError(f"argv contains forbidden flag(s): {sorted(forbidden)}")
    if "exec" not in argv:
        raise ValueError("argv must use `codex exec`: anything else wants a terminal")
    if argv[-1] != "-":
        raise ValueError(
            "argv must end with `-` so the prompt is read from stdin; a prompt argument and "
            "piped stdin are concatenated, which is how a prompt ends up on a command line"
        )
    sandbox = _flag_value(argv, "-s")
    if sandbox != SANDBOX:
        raise ValueError(
            f"argv must pass -s {SANDBOX}, got {sandbox!r}; the operator's config.toml sets "
            "sandbox_mode = danger-full-access globally, so an absent flag is not a default"
        )
    if sandbox in FORBIDDEN_SANDBOXES:  # pragma: no cover — unreachable past the check above
        raise ValueError(f"sandbox {sandbox!r} is never permitted for a role call")
    if "--ignore-user-config" not in flags:
        raise ValueError(
            "argv must pass --ignore-user-config: the operator's model, effort, service tier "
            "and notify hook would otherwise reach the call"
        )
    if "--ephemeral" not in flags:
        raise ValueError(
            "argv must pass --ephemeral: without it every call writes a session transcript "
            "outside its scratch directory"
        )
    if "--skip-git-repo-check" not in flags:
        raise ValueError("argv must pass --skip-git-repo-check: the scratch dir is not a repo")

    overrides = _config_overrides(argv)
    if EFFORT_KEY not in overrides:
        raise ValueError(f"argv must state {EFFORT_KEY}: codex exec has no effort flag")
    if WEB_SEARCH_KEY not in overrides:
        raise ValueError(
            f"argv must state {WEB_SEARCH_KEY} explicitly, even when it is false — "
            "availability is intent, and an absent toggle hides which one was meant"
        )
    if _toml_string(overrides.get(APPROVAL_KEY)) != APPROVAL_POLICY:
        raise ValueError(
            f"argv must state {APPROVAL_KEY}={APPROVAL_POLICY!r} as a config override — "
            "codex exec has no -a flag, and a role call with no tty that inherits an "
            "interactive approval policy waits on a prompt nobody can answer until its "
            f"ceiling; got {overrides.get(APPROVAL_KEY)!r}"
        )


def _flag_value(argv: Sequence[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _toml_string(value: str | None) -> str | None:
    """A `-c` value with its TOML quoting removed, so the law compares names not spellings."""
    if value is None:
        return None
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _config_overrides(argv: Sequence[str]) -> dict[str, str]:
    """Every `-c key=value` on the command line, as a map."""
    overrides: dict[str, str] = {}
    for index, item in enumerate(argv):
        if item == "-c" and index + 1 < len(argv) and "=" in argv[index + 1]:
            key, _, value = argv[index + 1].partition("=")
            overrides[key.strip()] = value.strip()
    return overrides


# ------------------------------------------------------------------------------ trace


@dataclass
class _Trace:
    """What one call's JSONL said, accumulated as it arrives."""

    lines: deque[str] = field(default_factory=lambda: deque(maxlen=MAX_RAW_LINES))
    thread_id: str | None = None
    turn_completed: bool = False
    turn_failed: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    """The `turn.completed` usage block, or None. None means *absent*, not zero: a failed
    turn carries no usage at all, and a 0 written into the ledger for it would be a
    measurement the CLI never made."""

    searches: dict[str, list[str]] = field(default_factory=dict)
    """`web_search` item id → the queries it ran. Keyed by id so the `item.started` and
    `item.completed` halves of one search are not counted as two."""

    answer: str | None = None
    answer_at: float | None = None
    errors: list[str] = field(default_factory=list)
    reported_model: str | None = None
    """Whatever a `model` field in the stream said, if a future release ever adds one.
    Today nothing sets this, which is why `model_verified` is False on every call."""

    garbage: int = 0
    envelopes: int = 0
    items: dict[str, int] = field(default_factory=dict)
    stderr: str = ""
    exit_code: int | None = None

    schema_mode: str = "none"
    """How the output contract was imposed: `output_schema` (the flag), `prompt` (stated in
    words, for the one role strict mode cannot express) or `none` (no schema asked for).
    Recorded because "the model ignored the schema" and "the schema was never sent" are
    different failures with different fixes."""

    def note(self, line: str) -> None:
        self.lines.append(line[:MAX_LINE_CHARS])

    @property
    def metadata_refusal(self) -> str | None:
        for message in self.errors:
            if FALLBACK_METADATA_MARKER in message.lower():
                return message
        return None

    @property
    def rate_limited(self) -> bool:
        """Backpressure, recognised without pinning one shape of it.

        The probe never provoked a 429, so this reads both the HTTP status and the text
        rather than asserting a field that has not been seen. A false negative here costs a
        retry; a false positive would pause a healthy run for the cooldown.
        """
        payload = _api_error(self.turn_failed)
        inner = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        if 429 in {payload.get("status"), inner.get("status")}:
            return True
        haystack = " ".join([*self.errors, str(payload.get("message") or ""), str(inner)]).lower()
        return "rate limit" in haystack or "rate_limit" in haystack


def _api_error(message: Any) -> dict[str, Any]:
    """Unwrap Codex's doubly-encoded error payload.

    `{"type":"error","message":"{\\"status\\":400,...}"}` — the message is a JSON *string*
    that has to be loaded a second time to reach `{status, error.type, error.message}`.
    Anything that does not unwrap is kept as prose rather than dropped.
    """
    if isinstance(message, dict):
        return message
    text = str(message or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {"message": text}
    return parsed if isinstance(parsed, dict) else {"message": text}


def _error_text(payload: Any) -> str:
    """The most specific sentence in a codex error payload."""
    unwrapped = _api_error(payload)
    inner = unwrapped.get("error")
    if isinstance(inner, dict):
        detail = str(inner.get("message") or "").strip()
        if detail:
            status = unwrapped.get("status") or inner.get("status")
            return f"{detail}" + (f" (status {status})" if status else "")
    detail = str(unwrapped.get("message") or "").strip()
    return detail or "the turn failed"


# ----------------------------------------------------------------------------- runner


SpawnFn = Callable[..., Awaitable[SpawnedProcess]]


class CodexCliRunner:
    """`AgentRunner` backed by the Codex CLI. One process per call, nothing shared."""

    name = "codex"

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
        self._exe = Path(exe).resolve() if exe else resolve_codex_exe()
        self._argv_log = Path(argv_log) if argv_log else self._workdir / "spawn-argv.jsonl"
        self._spawn = spawn
        self._keep_scratch = keep_scratch

    # --- protocol ---------------------------------------------------------------------

    async def run_role(self, role: str, prompt: str, cfg: RoleConfig) -> RoleResult:
        if role != cfg.role:
            raise ValueError(f"role {role!r} does not match its config ({cfg.role!r})")
        # Before the spawn, not after: an id off the allowlist is a 400 that costs a call
        # slot and a round trip, and an id whose metadata is missing runs with the wrong
        # context window. This is the whole of the substitution guarantee on this provider.
        validate_model(cfg.model)

        call_id = str(uuid4())
        scratch = self._scratch(call_id)
        effort = resolve_effort(cfg.model, cfg.effort)
        trace = _Trace()
        started = time.monotonic()

        strict = codex_output_schema(cfg.json_schema)
        trace.schema_mode = (
            "none"
            if cfg.json_schema is None
            else ("output_schema" if strict is not None else "prompt")
        )

        try:
            if strict is not None:
                # Into the scratch directory, never argv: a schema inline would also blow
                # the spawn gate's argv ceiling on the bigger roles.
                (scratch / SCHEMA_FILE).write_text(
                    json.dumps(strict, ensure_ascii=False), encoding="utf-8"
                )
            argv = build_codex_argv(
                cfg,
                exe=self._exe,
                scratch=scratch,
                effort=effort.effort,
                schema_file=strict is not None,
            )

            try:
                spawned = await asyncio.wait_for(
                    self._spawn(
                        argv,
                        cwd=scratch,
                        workdir_root=self._workdir,
                        stdin_text=build_codex_stdin(
                            cfg, prompt, schema_in_prompt=trace.schema_mode == "prompt"
                        ),
                        budget_guard=self._budget_guard,
                        argv_log=self._argv_log,
                    ),
                    timeout=SPAWN_TIMEOUT_S,
                )
            except TimeoutError:
                return self._result(
                    cfg,
                    trace,
                    effort,
                    scratch,
                    error=f"spawn timed out after {SPAWN_TIMEOUT_S:.0f}s",
                    elapsed=time.monotonic() - started,
                )

            try:
                await self._consume_within(spawned.process, trace, cfg=cfg, started=started)
            except TimeoutError:
                outcome = await kill_process_tree(
                    spawned.process, expected_images=(spawned.image,)
                )
                elapsed = time.monotonic() - started
                salvaged = self._salvage(cfg, trace, scratch)
                if salvaged is not None:
                    log.warning(
                        "%s codex call was killed after %.0fs with its answer already "
                        "written; keeping it (%s)",
                        cfg.role,
                        elapsed,
                        outcome,
                    )
                    return self._result(
                        cfg, trace, effort, scratch, elapsed=elapsed, salvaged=outcome
                    )
                log.warning(
                    "%s codex call timed out after %.0fs (limit %.0fs, %s)",
                    cfg.role,
                    elapsed,
                    cfg.timeout_s,
                    outcome,
                )
                return self._result(
                    cfg,
                    trace,
                    effort,
                    scratch,
                    error=(
                        f"timeout after {elapsed:.0f}s (limit {cfg.timeout_s:.0f}s, {outcome})"
                    ),
                    elapsed=elapsed,
                )
            finally:
                await self._settle(spawned)

            return self._result(cfg, trace, effort, scratch, elapsed=time.monotonic() - started)
        finally:
            self._clean(scratch)

    async def probe(self) -> dict[str, Any]:
        result = await probe_version(self._exe)
        return {"name": self.name, **result}

    async def aclose(self) -> None:
        return None

    # --- process lifecycle ------------------------------------------------------------

    def _scratch(self, call_id: str) -> Path:
        scratch = self._workdir / "calls" / call_id
        scratch.mkdir(parents=True, exist_ok=True)
        return scratch

    def _clean(self, scratch: Path) -> None:
        if self._keep_scratch:
            return
        shutil.rmtree(scratch, ignore_errors=True)

    @staticmethod
    async def _settle(spawned: SpawnedProcess) -> None:
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
        trace: _Trace,
        *,
        cfg: RoleConfig,
        started: float,
    ) -> None:
        """Consume the stream under a deadline that moves once the answer has landed."""
        task = asyncio.create_task(self._consume(process, trace))
        try:
            while True:
                deadline = started + cfg.timeout_s
                if trace.answer_at is not None:
                    deadline = max(deadline, trace.answer_at + ANSWER_GRACE_S)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                done, _ = await asyncio.wait({task}, timeout=remaining)
                if done:
                    await task
                    return
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def _consume(self, process: asyncio.subprocess.Process, trace: _Trace) -> None:
        stderr_task = asyncio.create_task(self._read_stderr(process, trace))
        try:
            await self._read_stdout_or_exit(process, trace)
            await asyncio.wait({stderr_task}, timeout=EXIT_DRAIN_S)
        finally:
            stderr_task.cancel()
            await asyncio.gather(stderr_task, return_exceptions=True)
        trace.exit_code = await self._exit_code(process)

    async def _read_stdout_or_exit(
        self, process: asyncio.subprocess.Process, trace: _Trace
    ) -> None:
        """Read stdout, but never outlive the child by more than `EXIT_DRAIN_S`.

        Codex spawns helper executables for shell and search tools, and any of them can
        inherit the stdout handle — in which case the pipe never reaches EOF after the child
        dies, and a reader waiting for one waits out the whole ceiling on a dead process.
        """
        reader = asyncio.create_task(self._read_stdout(process, trace))
        waiter = asyncio.create_task(_exited(process))
        try:
            await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
            if not reader.done():
                await asyncio.wait({reader}, timeout=EXIT_DRAIN_S)
            if reader.done():
                await reader
        finally:
            for task in (reader, waiter):
                if not task.done():
                    task.cancel()
            await asyncio.gather(reader, waiter, return_exceptions=True)

    @staticmethod
    async def _exit_code(process: asyncio.subprocess.Process) -> int | None:
        if process.returncode is not None:
            return process.returncode
        try:
            return await asyncio.wait_for(process.wait(), timeout=EXIT_DRAIN_S)
        except TimeoutError:
            return None

    async def _read_stdout(
        self, process: asyncio.subprocess.Process, trace: _Trace
    ) -> None:
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
                    self._handle(line, trace)
                return
            buffer += decoder.decode(chunk)
            *complete, buffer = buffer.split("\n")
            for line in complete:
                self._handle(line, trace)

    async def _read_stderr(
        self, process: asyncio.subprocess.Process, trace: _Trace
    ) -> None:
        """Drain stderr so a chatty child cannot block on a full pipe.

        Never a failure signal on its own: the installed CLI logs a benign models-cache
        version-skew error at ERROR level on a call that exits 0.
        """
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
                trace.stderr = "".join(collected).strip()[:MAX_RAW_TAIL_CHARS]

    def _handle(self, line: str, trace: _Trace) -> None:
        """One JSONL line. Unknown envelope types are transcript, never a failure."""
        stripped = line.strip()
        if not stripped:
            return
        trace.note(stripped)
        try:
            envelope = json.loads(stripped)
        except ValueError:
            trace.garbage += 1
            return
        if not isinstance(envelope, dict):
            trace.garbage += 1
            return

        trace.envelopes += 1
        model = envelope.get("model")
        if isinstance(model, str) and model.strip():
            trace.reported_model = model.strip()

        kind = envelope.get("type")
        if kind == "thread.started":
            trace.thread_id = str(envelope.get("thread_id") or "") or None
        elif kind == "turn.completed":
            trace.turn_completed = True
            usage = envelope.get("usage")
            trace.usage = usage if isinstance(usage, dict) else None
        elif kind == "turn.failed":
            trace.turn_failed = _api_error(
                (envelope.get("error") or {}).get("message")
                if isinstance(envelope.get("error"), dict)
                else envelope.get("error")
            )
        elif kind == "error":
            trace.errors.append(_error_text(envelope.get("message")))
        elif kind in ("item.started", "item.completed"):
            self._handle_item(envelope.get("item"), kind, trace)

    def _handle_item(self, item: Any, kind: str, trace: _Trace) -> None:
        if not isinstance(item, dict):
            return
        item_type = str(item.get("type") or "unknown")
        if kind == "item.completed":
            trace.items[item_type] = trace.items.get(item_type, 0) + 1

        if item_type == "error":
            trace.errors.append(str(item.get("message") or "").strip())
            return
        if item_type == "web_search":
            # Keyed by item id so `started` and `completed` are one search, and `action`
            # may carry either a single query or an array of parallel ones.
            queries = trace.searches.setdefault(str(item.get("id") or len(trace.searches)), [])
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            found = (item.get("query"), action.get("query"), *(action.get("queries") or ()))
            for candidate in found:
                text = str(candidate or "").strip()
                if text and text not in queries:
                    queries.append(text)
            return
        if item_type == "agent_message" and kind == "item.completed":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                trace.answer = text
                trace.answer_at = time.monotonic()

    # --- result assembly --------------------------------------------------------------

    def _last_message(self, scratch: Path) -> str | None:
        """The `-o` file's contents, or None when it was never written.

        None is load-bearing: on a failed turn Codex does not create this file at all
        (verified live), so an absent or empty file is a hard failure and must never read
        back as an empty answer.
        """
        try:
            text = (scratch / LAST_MESSAGE_FILE).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        return text if text.strip() else None

    @staticmethod
    def _parse(text: str | None) -> dict[str, Any] | None:
        """Parse an answer, tolerating a code fence around it.

        The probe's `--output-schema` answers were bare JSON with no fence and no trailing
        newline. The fence tolerance is for the prompt-stated contract, where the model is
        asked for bare JSON and a fence is the one deviation it reliably makes anyway —
        throwing away a correct payload over three backticks would be the pettiest possible
        way to lose a clustering call.
        """
        if not text:
            return None
        body = text.strip()
        if body.startswith("```"):
            fenced = body.split("\n", 1)[-1]
            body = fenced.rsplit("```", 1)[0].strip() if "```" in fenced else fenced.strip()
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else {"value": payload}

    def _salvage(
        self, cfg: RoleConfig, trace: _Trace, scratch: Path
    ) -> dict[str, Any] | None:
        """A complete answer recovered from a call that was killed at its ceiling.

        `is not None` rather than `or`: a file holding a bare `{}` parses to an empty dict,
        which is falsy, and the truthiness test used to hand the transcript's answer back
        here while `_result` went on to read the same `{}` off disk and return *that*. The
        two have to agree about what was recovered, and whether `{}` is an answer at all is
        `_failure`'s decision to make once — not something to settle differently in two
        places.
        """
        if cfg.json_schema is None:
            return None
        parsed = self._parse(self._last_message(scratch))
        if parsed is not None:
            return parsed
        return self._parse(trace.answer)

    def _result(
        self,
        cfg: RoleConfig,
        trace: _Trace,
        effort: Any,
        scratch: Path,
        *,
        error: str | None = None,
        elapsed: float,
        salvaged: str | None = None,
    ) -> RoleResult:
        usage = _usage(trace, elapsed)
        file_text = self._last_message(scratch)
        data = self._parse(file_text)
        from_transcript = False
        # `not data`, not `data is None`: a file holding a bare `{}` is an object with none
        # of the fields any role schema requires, so it is worth no more than an absent one
        # and must not shadow a complete answer sitting in the transcript.
        if not data:
            transcript = self._parse(trace.answer)
            if transcript:
                data, from_transcript = transcript, True

        swap = check_substitution(cfg.model, _reported_usage(trace))
        summary = _summary(cfg, trace, effort, elapsed, swap)
        summary["structured_from_transcript"] = from_transcript
        summary["near_timeout"] = elapsed >= TIMEOUT_WARN_FRACTION * cfg.timeout_s
        summary["timeout_s"] = cfg.timeout_s

        if salvaged is not None:
            summary["salvaged_after_timeout"] = salvaged
            # A kill at the ceiling excuses exactly one thing — the missing `turn.completed`
            # — and nothing else. Every other reason this call cannot be trusted still
            # applies: a `turn.failed`, an error item, and above all a fallback-metadata
            # refusal, which is a refusal *even on a turn that goes on to succeed*. Returning
            # here unconditionally made the timeout path the one route by which a refused
            # answer entered the pipeline as a good one.
            if error is None:
                error = _failure(cfg, trace, data, salvaged=True)
            if error is None:
                return RoleResult(
                    role=cfg.role,
                    model=cfg.model,
                    data=dict(data or {}),
                    usage=usage,
                    raw_tail=_raw_tail(summary, trace),
                    telemetry=summary,
                    degraded=True,
                    rate_limited=trace.rate_limited,
                )
            # Otherwise fall through: an ordinary failure that happens to carry
            # `salvaged_after_timeout` in its telemetry, which is what happened to it.

        if error is None:
            error = _failure(cfg, trace, data)
        # A swap this provider cannot even attribute to a known model is worse than no
        # answer. Codex reports no model today, so this is a hook rather than a live path —
        # written now because the day it starts reporting one is not the day to design it.
        if error is None and swap is not None and swap.fatal:
            log.error("%s codex call was answered by the wrong model: %s", cfg.role, swap.detail)
            error = f"model substitution: {swap.detail}"

        return RoleResult(
            role=cfg.role,
            model=cfg.model,
            data=data if error is None else None,
            error=error,
            usage=usage,
            raw_tail=_raw_tail(summary, trace),
            telemetry=summary,
            degraded=swap is not None or effort.clamped,
            rate_limited=trace.rate_limited,
        )


def _failure(
    cfg: RoleConfig, trace: _Trace, data: dict[str, Any] | None, *, salvaged: bool = False
) -> str | None:
    """The first reason this call cannot be trusted, or None.

    `salvaged` says the process was killed at its ceiling with an answer already written. It
    suppresses one check and one only — the absent `turn.completed`, which is the expected
    state of a process that was killed before it could say so. Everything else is asked
    exactly as it would be of a call that finished on its own.
    """
    refusal = trace.metadata_refusal
    if refusal:
        # Even on a turn that completed: the client ran with another model's context window
        # and tool shapes, which is not the call that was asked for.
        return f"codex ran with fallback model metadata: {refusal}"
    if trace.turn_failed is not None:
        return f"the turn failed: {_error_text(trace.turn_failed)}"
    if trace.errors:
        return f"the turn failed: {trace.errors[0][:300]}"
    if not trace.turn_completed and not salvaged:
        detail = trace.stderr or "no output"
        return f"codex produced no turn.completed event (exit {trace.exit_code}): {detail[:400]}"
    if cfg.grounded and not trace.searches:
        # Not fatal for the call, but it is the fact that makes "grounded" a claim rather
        # than a setting, so it is said out loud rather than left to a counter nobody reads.
        log.warning("%s ran grounded on codex and searched nothing", cfg.role)
    if not data:
        # `not data` rather than `data is None`: no role schema admits `{}` — every one of
        # them requires at least one property — so an empty object is "the schema was not
        # honoured" on every path it can arrive by, and letting it through would put a
        # hypothesis-shaped hole into the pipeline as a real answer.
        return (
            "no answer file was written and no agent message parsed as a non-empty JSON "
            "object; the schema was not honoured"
        )
    return None


def _reported_usage(trace: _Trace) -> dict[str, Any] | None:
    """The substitution guard's input, in the shape it expects.

    Codex names no model anywhere in its stream, so this is `None` on every call today and
    the guard correctly returns "no evidence". It exists so that a release which starts
    reporting one is covered by the same provider-scoped rules as Claude, rather than by a
    second guard written in a hurry.
    """
    return {trace.reported_model: {}} if trace.reported_model else None


def _usage(trace: _Trace, elapsed: float) -> Usage:
    """Codex's counters, mapped by name. Its field names are not Claude's.

    `cost_usd` is always None: Codex reports no cost, and inventing one would put a guess in
    the ledger. When the turn failed there is no usage block at all — the counters then read
    0 because the ledger sums ints, and `usage_reported: False` in telemetry is what says
    that 0 is an absence.
    """
    raw = trace.usage or {}
    return Usage(
        tokens_in=int(raw.get("input_tokens") or 0),
        tokens_out=int(raw.get("output_tokens") or 0),
        cache_creation=int(raw.get("cache_write_input_tokens") or 0),
        cache_read=int(raw.get("cached_input_tokens") or 0),
        cost_usd=None,
        duration_ms=int(elapsed * 1000),
    )


def _summary(
    cfg: RoleConfig,
    trace: _Trace,
    effort: Any,
    elapsed: float,
    swap: Substitution | None,
) -> dict[str, Any]:
    """Every fact worth persisting about one codex call, in one compact object."""
    queries = [query for item in trace.searches.values() for query in item]
    failure = _api_error(trace.turn_failed) if trace.turn_failed else {}
    subtype = "turn.completed" if trace.turn_completed else None
    return {
        "role": cfg.role,
        "provider": "openai",
        "model": cfg.model,
        "effort": effort.effort,
        "effort_requested": effort.requested,
        "effort_clamped": effort.clamped,
        "tools": cfg.tools,
        "session_id": trace.thread_id,
        "cli_version": None,
        "is_error": bool(trace.turn_failed or trace.errors),
        "subtype": "turn.failed" if trace.turn_failed else subtype,
        "terminal_reason": _error_text(trace.turn_failed) if trace.turn_failed else None,
        "api_error_status": failure.get("status"),
        "permission_denials": None,
        "num_turns": 1 if trace.turn_completed or trace.turn_failed else None,
        "model_requested": cfg.model,
        "model_ran": ", ".join(swap.ran) if swap else None,
        "model_substituted": swap is not None,
        "model_below_floor": bool(swap and swap.below_floor),
        "model_cross_provider": bool(swap and swap.cross_provider),
        # Stated, not omitted. Codex's JSONL names no model, so nothing here verifies which
        # one answered — a reader must be able to tell that from the telemetry rather than
        # from this module's docstring.
        "model_verified": trace.reported_model is not None,
        "duration_ms": None,
        "total_cost_usd": None,
        "web_searches": len(trace.searches),
        "web_search_queries": queries or None,
        "tool_uses": dict(trace.items) or None,
        "schema_mode": trace.schema_mode,
        "usage_reported": trace.usage is not None,
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
