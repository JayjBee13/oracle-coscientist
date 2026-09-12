"""The one place this application starts a model process.

Every `claude` call goes through `spawn_role_process`, and it exists because the failures
this system has already had were all failures of *what got launched*, not of what the model
said. So the gate is deliberately paranoid, and each check is here for a specific incident:

* **The binary is resolved past the npm shim.** Windows `.cmd` and Linux Node launchers
  insert processes between us and the thing we later have to kill. The resolvers locate
  the vendored PE/ELF binary directly, so the pid we retain is the process doing the work.
* **argv is checked for `://`.** Connection strings and tokens reach the supervisor through
  the environment, never the command line — an argv is visible in the process table and in
  every crash dump. The check is blunt on purpose: a URL in argv means someone is about to
  put a credential there.
* **cwd is confined to the run workdir.** A model process that can see the repository is a
  model process that can be asked to change it. Each call gets an empty scratch directory.
* **The budget is re-checked here.** The orchestrator already refuses unaffordable steps;
  this is the backstop that holds when the orchestrator has a bug, which is the only kind of
  cap that is worth anything.
* **The child's environment is scrubbed.** The supervisor holds the database DSN and the
  app token; the model process must not inherit them. An `ANTHROPIC_API_KEY` is dropped too:
  this account runs on subscription OAuth, and an inherited key would silently bill the API.

What the allowlist is and is not: it stops argv[0] drifting away from a binary someone
deliberately registered (the resolved CLI, or a test stub). It is not a sandbox — the caller
is our own code. It is a tripwire for the day argv[0] starts coming from configuration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.runs.paths import is_within

__all__ = [
    "ARGV_CHAR_CEILING",
    "CODEX_EXE_ENV",
    "ClaudeNotFound",
    "CliNotFound",
    "CodexNotFound",
    "SpawnRefused",
    "SpawnedProcess",
    "allow_binary",
    "child_env",
    "kill_process_tree",
    "probe_version",
    "process_image",
    "python_executable",
    "record_spawn",
    "recorded_spawns",
    "reset_claude_exe_cache",
    "resolve_claude_exe",
    "resolve_codex_exe",
    "spawn_role_process",
    "terminate_pid_tree",
]

log = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"

ARGV_CHAR_CEILING = 30_000
"""Windows' CreateProcess command line tops out at 32,767 characters.

Refusing at 30,000 turns a truncated-argv mystery into a named error naming the flag that
grew — in practice an inline `--json-schema` or a system prompt that got templated wrong.
"""

NPM_PACKAGE_DIR = Path("node_modules") / "@anthropic-ai" / "claude-code" / "bin"

CLAUDE_EXE_ENV = "COSCIENTIST_CLAUDE_EXE"
CODEX_EXE_ENV = "COSCIENTIST_CODEX_EXE"

# The platform triple npm's codex package names its vendored binary directory after.
_WINDOWS_TRIPLES: dict[str, str] = {
    "arm64": "aarch64-pc-windows-msvc",
    "aarch64": "aarch64-pc-windows-msvc",
}
_DEFAULT_WINDOWS_TRIPLE = "x86_64-pc-windows-msvc"
_LINUX_TRIPLES: dict[str, tuple[str, str]] = {
    "aarch64": ("aarch64-unknown-linux-musl", "codex-linux-arm64"),
    "arm64": ("aarch64-unknown-linux-musl", "codex-linux-arm64"),
    "x86_64": ("x86_64-unknown-linux-musl", "codex-linux-x64"),
    "amd64": ("x86_64-unknown-linux-musl", "codex-linux-x64"),
}

_SECRET_ENV_KEY = re.compile(
    r"PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|DATABASE_URL|CREDENTIAL|PRIVATE_KEY|(?:^|_)DSN$",
    re.IGNORECASE,
)
_CREDENTIALED_URL = re.compile(r"[a-z][a-z0-9+.-]*://[^/\s]*:[^/\s]*@", re.IGNORECASE)

_SPAWN_LOG: deque[dict[str, Any]] = deque(maxlen=200)
"""Recent spawns, for the safety doctor and for tests. Bounded: this is a live process."""

_ALLOWED: set[str] = set()


class SpawnRefused(RuntimeError):
    """The gate refused to start a process. Always a bug on our side, never the model's."""


class CliNotFound(RuntimeError):
    """No native executable could be resolved for a CLI this engine runs."""


class ClaudeNotFound(CliNotFound):
    """No native Claude executable could be resolved."""


class CodexNotFound(CliNotFound):
    """No native Codex executable could be resolved.

    A separate resolver from Claude's, because the shim shape is different in the one way
    that matters. `claude.cmd` names `claude.exe` outright, so the shim can be read and
    followed. `codex.cmd` names `bin\\codex.js`, and *node* then spawns the vendored native
    binary — so a regex over the shim finds nothing, and launching the shim would put
    `cmd.exe → node.exe → codex.exe` between us and the process we later have to kill.
    Three levels, with `cmd.exe` staying resident as the parent: killing the pid we were
    handed would orphan node and codex, and leave a model turn running and billing. So the
    vendored `.exe` is located by layout instead (verified live 2026-08-11) and spawned
    directly, which makes the pid we hold the pid that matters.
    """


# --------------------------------------------------------------------------- resolution


def _shim_targets(shim: Path) -> Iterable[Path]:
    """Where a `claude` shim's real executable might live, best guess first."""
    names = ("claude.exe",) if IS_WINDOWS else ("claude", "claude.exe")
    for name in names:
        yield shim.parent / NPM_PACKAGE_DIR / name
    try:
        with shim.open("rb") as handle:
            raw = handle.read(65_537)
    except OSError:
        return
    if len(raw) > 65_536 or raw.startswith(b"\x7fELF"):
        return
    text = raw.decode("utf-8", errors="replace")
    # Both shims name the executable outright; %dp0% (cmd) and $basedir (sh) are the
    # shim's own directory. Reading it beats hard-coding one npm layout forever.
    suffix = r"claude\.exe" if IS_WINDOWS else r"claude(?:\.exe)?"
    for raw in re.findall(rf'["\']?([^"\'\s%$]*{suffix})', text):
        candidate = raw.replace("\\", "/").lstrip("/")
        yield shim.parent / candidate


def _is_native_executable(path: Path) -> bool:
    """Whether *path* is the native process we can safely own and later kill."""
    if not path.is_file():
        return False
    if IS_WINDOWS:
        return path.suffix.lower() == ".exe"
    if not os.access(path, os.X_OK):
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(4) == b"\x7fELF"
    except OSError:
        return False


def _binary_key(path: Path | str) -> str:
    value = str(Path(path).resolve())
    return value.lower() if IS_WINDOWS else value


def _resolve_uncached(hint: str | None) -> Path:
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint))
    else:
        env_hint = os.environ.get(CLAUDE_EXE_ENV)
        if env_hint:
            candidates.append(Path(env_hint))
        found = shutil.which("claude")
        if found:
            candidates.append(Path(found))

    if not candidates:
        raise ClaudeNotFound(
            "No `claude` on PATH. Install the CLI or set "
            f"{CLAUDE_EXE_ENV} to the full path of the native Claude executable."
        )

    tried: list[str] = []
    for candidate in candidates:
        tried.append(str(candidate))
        if _is_native_executable(candidate):
            return candidate.resolve()
        for target in _shim_targets(candidate):
            tried.append(str(target))
            if _is_native_executable(target):
                return target.resolve()

    raise ClaudeNotFound(
        "Could not resolve a native Claude executable past the npm shim. Tried: "
        + ", ".join(tried)
    )


_EXE_CACHE: dict[str, Path] = {}


def resolve_claude_exe(hint: str | Path | None = None) -> Path:
    """The native Claude binary, never the `.cmd`/`.ps1`/Node shim.

    Resolution order: explicit `hint`, `$COSCIENTIST_CLAUDE_EXE`, then `claude` on PATH
    followed through the shim. Cached — this runs on every call and hits the filesystem.
    """
    key = str(hint) if hint else os.environ.get(CLAUDE_EXE_ENV, "")
    cached = _EXE_CACHE.get(key)
    if cached is not None:
        return cached
    resolved = _resolve_uncached(str(hint) if hint else None)
    if not _is_native_executable(resolved):  # pragma: no cover — resolver enforces it
        raise ClaudeNotFound(f"{resolved} is not a native executable")
    _EXE_CACHE[key] = resolved
    allow_binary(resolved)
    return resolved


def reset_claude_exe_cache() -> None:
    """Forget resolved paths. For tests, and for a CLI upgrade mid-session."""
    _EXE_CACHE.clear()


# ------------------------------------------------------------------------ codex resolution


def _codex_triple() -> str:
    if not IS_WINDOWS:
        return _LINUX_TRIPLES.get(platform.machine().lower(), ("", ""))[0]
    return _WINDOWS_TRIPLES.get(os.environ.get("PROCESSOR_ARCHITECTURE", "").lower(), "") or (
        _WINDOWS_TRIPLES.get(sys.platform, "") or _DEFAULT_WINDOWS_TRIPLE
    )


def _codex_shim_targets(shim: Path) -> Iterable[Path]:
    """Where the npm `codex` shim's real executable lives, best guess first.

    The two documented layouts first, because they are exact path joins and cost nothing;
    the recursive glob last, because it is the one that keeps working when npm rearranges
    `node_modules` again. All three are relative to the shim's own directory, which is npm's
    bin directory and therefore the root of the package tree the shim belongs to.
    """
    root = shim.parent
    resolved_shim = shim.resolve(strict=False)
    package = (
        resolved_shim.parent.parent
        if resolved_shim.name == "codex.js" and resolved_shim.parent.name == "bin"
        else root / "node_modules" / "@openai" / "codex"
    )
    triple = _codex_triple()
    if IS_WINDOWS:
        platform_pkg = "codex-win32-arm64" if triple.startswith("aarch64") else "codex-win32-x64"
        executable = "codex.exe"
    else:
        _, platform_pkg = _LINUX_TRIPLES.get(platform.machine().lower(), ("", ""))
        executable = "codex"
    vendored = Path("vendor") / triple / "bin" / executable
    yield package / "node_modules" / "@openai" / platform_pkg / vendored
    yield package / vendored
    try:
        yield from sorted(package.glob(f"**/vendor/*/bin/{executable}"))
    except OSError:  # pragma: no cover — an unreadable tree is just no candidates
        return


def _resolve_codex_uncached(hint: str | None) -> Path:
    candidates: list[Path] = []
    if hint:
        candidates.append(Path(hint))
    else:
        env_hint = os.environ.get(CODEX_EXE_ENV)
        if env_hint:
            candidates.append(Path(env_hint))
        # `codex.exe` is deliberately asked for first even though PATHEXT prefers .EXE: on
        # this box `shutil.which("codex")` returns the .CMD shim and `which("codex.exe")`
        # returns None, so asking for both is how a future native install is preferred
        # without depending on PATHEXT ordering.
        names = ("codex.exe", "codex") if IS_WINDOWS else ("codex",)
        for name in names:
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    if not candidates:
        raise CodexNotFound(
            "No `codex` on PATH. Install the Codex CLI or set "
            f"{CODEX_EXE_ENV} to the full path of the native Codex executable."
        )

    tried: list[str] = []
    for candidate in candidates:
        tried.append(str(candidate))
        if _is_native_executable(candidate):
            return candidate.resolve()
        for target in _codex_shim_targets(candidate):
            tried.append(str(target))
            if _is_native_executable(target):
                return target.resolve()

    raise CodexNotFound(
        "Could not resolve a native Codex executable past the npm shim; the shim names "
        "`bin/codex.js`, so the vendored binary has to be found by layout. Tried: "
        + ", ".join(tried)
    )


def resolve_codex_exe(hint: str | Path | None = None) -> Path:
    """The vendored native Codex binary, never the shim or its `codex.js`.

    Resolution order: explicit `hint`, `$COSCIENTIST_CODEX_EXE`, then `codex` on PATH
    followed through the npm package layout. Cached, like Claude's — this runs on every call
    and the glob fallback hits the filesystem.
    """
    key = f"codex\0{hint if hint else os.environ.get(CODEX_EXE_ENV, '')}"
    cached = _EXE_CACHE.get(key)
    if cached is not None:
        return cached
    resolved = _resolve_codex_uncached(str(hint) if hint else None)
    if not _is_native_executable(resolved):  # pragma: no cover — resolver enforces it
        raise CodexNotFound(f"{resolved} is not a native executable")
    _EXE_CACHE[key] = resolved
    allow_binary(resolved)
    return resolved


def allow_binary(path: Path | str) -> Path:
    """Register a binary the gate may launch, and return its resolved path."""
    resolved = Path(path).resolve()
    if not _is_native_executable(resolved):
        raise SpawnRefused(f"not an executable file: {resolved}")
    _ALLOWED.add(_binary_key(resolved))
    return resolved


# ------------------------------------------------------------------------- environment


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The parent environment minus anything that looks like a secret.

    A denylist, not an allowlist: the CLI needs PATH, APPDATA (its OAuth credentials live
    under the user profile), TEMP and a long tail of Windows variables, and an allowlist
    would break on the first one we forgot. Dropped are keys that name a secret and values
    that carry credentials inside a URL, whatever the key is called.
    """
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        if _SECRET_ENV_KEY.search(key):
            continue
        if _CREDENTIALED_URL.search(value):
            continue
        env[key] = value
    # A CLI self-update mid-run replaces the binary while sibling calls are spawning it,
    # which fails every spawn until the write completes (WinError 216). Headless engine
    # calls must never trigger one.
    env["DISABLE_AUTOUPDATER"] = "1"
    if extra:
        env.update(extra)
    return env


# ------------------------------------------------------------------------------- gate


@dataclass(frozen=True, slots=True)
class SpawnedProcess:
    """A started process plus what it was started with.

    `stdin_task` feeds the prompt in the background. It has to be a task: a meta-review
    prompt is tens of kilobytes, the pipe buffer is not, and a caller that wrote the prompt
    before reading stdout would deadlock against a child doing exactly the same thing.
    """

    process: asyncio.subprocess.Process
    argv: tuple[str, ...]
    cwd: Path
    stdin_task: asyncio.Task[None] | None = None
    started_at: float = field(default_factory=time.monotonic)

    @property
    def image(self) -> str:
        """Executable name, for verifying the pid before killing it."""
        return Path(self.argv[0]).name


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if not argv:
        raise SpawnRefused("argv is empty")
    for index, item in enumerate(argv):
        if not isinstance(item, str):
            raise SpawnRefused(f"argv[{index}] is {type(item).__name__}, not str")
        if "\x00" in item:
            raise SpawnRefused(f"argv[{index}] contains a NUL byte")
        if "://" in item:
            raise SpawnRefused(
                f"argv[{index}] contains '://' — connection strings and tokens go through "
                "the environment, never the command line"
            )
    total = sum(len(item) + 3 for item in argv)
    if total > ARGV_CHAR_CEILING:
        longest = max(range(len(argv)), key=lambda index: len(argv[index]))
        raise SpawnRefused(
            f"argv is {total} characters, over the {ARGV_CHAR_CEILING} ceiling; "
            f"argv[{longest}] alone is {len(argv[longest])}"
        )
    return tuple(argv)


def _validate_binary(argv0: str, allowed: Iterable[Path | str] | None) -> Path:
    binary = Path(argv0)
    if not binary.is_absolute():
        raise SpawnRefused(f"argv[0] must be an absolute path, got {argv0!r}")
    resolved = binary.resolve()
    if IS_WINDOWS and resolved.suffix.lower() != ".exe":
        raise SpawnRefused(
            f"{resolved} is not an .exe — the shim layer is what we resolve past, "
            "not something to spawn"
        )
    if not resolved.is_file():
        raise SpawnRefused(f"no such executable: {resolved}")

    permitted = {_binary_key(item) for item in (allowed or ())} or _ALLOWED
    if _binary_key(resolved) not in permitted:
        raise SpawnRefused(
            f"{resolved} is not an allowed binary; register it with allow_binary() first"
        )
    if not IS_WINDOWS and not _is_native_executable(resolved):
        raise SpawnRefused(f"not a native executable: {resolved}")
    return resolved


def _record(entry: dict[str, Any], argv_log: Path | None) -> None:
    _SPAWN_LOG.append(entry)
    if argv_log is None:
        return
    try:
        argv_log.parent.mkdir(parents=True, exist_ok=True)
        with argv_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:  # an audit line is not worth failing a call over
        log.warning("could not append to %s: %s", argv_log, exc)


def record_spawn(entry: Mapping[str, Any], argv_log: Path | str | None = None) -> None:
    """Add a process this application started to the audit trail.

    The role gate records its own spawns; this is for the other one — the supervisor, which
    the launcher starts directly — so the safety doctor's "no `://` in any recorded argv"
    check covers the process that actually holds the connection string.
    """
    _record(dict(entry), Path(argv_log) if argv_log else None)


def recorded_spawns() -> tuple[dict[str, Any], ...]:
    """Every spawn this process has made, most recent last (bounded to the last 200)."""
    return tuple(_SPAWN_LOG)


async def _feed_stdin(process: asyncio.subprocess.Process, text: str) -> None:
    stdin = process.stdin
    if stdin is None:  # pragma: no cover — we always ask for a pipe
        return
    try:
        stdin.write(text.encode("utf-8"))
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # The child exited before reading the prompt. Its result envelope (or its absence)
        # is the real diagnosis; a traceback from the writer would only bury it.
        log.debug("child closed stdin before the prompt was written")
    except Exception as exc:  # noqa: BLE001
        log.warning("writing the prompt to stdin failed: %s", exc)
    finally:
        try:
            stdin.close()
        except Exception:  # noqa: BLE001 — already closed, already dead
            pass


async def spawn_role_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    workdir_root: Path,
    stdin_text: str,
    budget_guard: Callable[[], None],
    allowed_binaries: Iterable[Path | str] | None = None,
    argv_log: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> SpawnedProcess:
    """Start one model call. The only way this application launches a role process.

    Checks in order — argv, binary, cwd, budget — because each is cheaper than the next and
    the budget guard is the one with a database round trip in it. `budget_guard` raises
    (`BudgetExhausted`) rather than returning false, and it is called *before* the spawn, so
    a refusal costs nothing.
    """
    checked = _validate_argv(argv)
    binary = _validate_binary(checked[0], allowed_binaries)

    cwd = Path(cwd)
    if not cwd.is_dir():
        raise SpawnRefused(f"cwd does not exist: {cwd}")
    if not is_within(cwd, workdir_root):
        raise SpawnRefused(
            f"cwd {cwd} is outside the run workdir {workdir_root} — a model process only "
            "ever sees its own scratch directory"
        )

    # Off the event loop. This is a synchronous database round trip to a remote Postgres,
    # and up to `PARALLEL_CALLS` model streams share the loop it would otherwise block —
    # their deadlines keep running while it waits, so the supervisor would be charging its
    # own stalls to the model's time budget.
    await asyncio.to_thread(budget_guard)

    _record(
        {
            "ts": time.time(),
            "argv": list(checked),
            "cwd": str(cwd).replace("\\", "/"),
            "stdin_chars": len(stdin_text),
        },
        argv_log,
    )

    creationflags = (
        subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
    )
    process = await asyncio.create_subprocess_exec(
        str(binary),
        *checked[1:],
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=child_env(env),
        creationflags=creationflags,
        start_new_session=not IS_WINDOWS,
    )
    task = asyncio.create_task(_feed_stdin(process, stdin_text))
    return SpawnedProcess(process=process, argv=checked, cwd=cwd, stdin_task=task)


# -------------------------------------------------------------------------------- kill


@dataclass(frozen=True, slots=True)
class _ProcIdentity:
    pid: int
    ppid: int
    pgrp: int
    session: int
    state: str
    starttime: int


@dataclass(slots=True)
class _OwnedProcess:
    identity: _ProcIdentity
    pidfd: int | None = None


def _read_proc_identity(pid: int) -> _ProcIdentity | None:
    """Read stable Linux procfs fields, treating zombies as vanished."""
    if pid <= 0:
        return None
    try:
        raw = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        close = raw.rfind(")")
        fields = raw[close + 2 :].split()
        if close < 0 or len(fields) < 20 or fields[0] == "Z":
            return None
        return _ProcIdentity(
            pid=pid,
            state=fields[0],
            ppid=int(fields[1]),
            pgrp=int(fields[2]),
            session=int(fields[3]),
            starttime=int(fields[19]),
        )
    except (OSError, ValueError, IndexError):
        return None


def _same_process(identity: _ProcIdentity) -> bool:
    current = _read_proc_identity(identity.pid)
    return current is not None and current.starttime == identity.starttime


def _open_owned_process(identity: _ProcIdentity) -> _OwnedProcess | None:
    """Pin a pid when pidfds exist; otherwise retain identity for each signal."""
    opener = getattr(os, "pidfd_open", None)
    pidfd: int | None = None
    if callable(opener):
        try:
            pidfd = opener(identity.pid, 0)
        except (OSError, ValueError):
            pidfd = None
    if not _same_process(identity):
        if pidfd is not None:
            os.close(pidfd)
        return None
    return _OwnedProcess(identity=identity, pidfd=pidfd)


def _signal_owned(process: _OwnedProcess, sig: signal.Signals) -> bool:
    """Signal the captured process, never a later process that reused its pid."""
    try:
        pidfd_send = getattr(signal, "pidfd_send_signal", None)
        if process.pidfd is not None and callable(pidfd_send):
            pidfd_send(process.pidfd, sig)
        else:
            if not _same_process(process.identity):
                return False
            os.kill(process.identity.pid, sig)
        return True
    except ProcessLookupError:
        return False
    except OSError as exc:
        log.warning("signal %s for pid %s failed: %s", sig.name, process.identity.pid, exc)
        return False


def _close_owned(processes: Iterable[_OwnedProcess]) -> None:
    for process in processes:
        if process.pidfd is not None:
            try:
                os.close(process.pidfd)
            except OSError:
                pass


def _proc_snapshot() -> dict[int, _ProcIdentity]:
    snapshot: dict[int, _ProcIdentity] = {}
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError:
        return snapshot
    for entry in entries:
        if not entry.name.isdigit():
            continue
        identity = _read_proc_identity(int(entry.name))
        if identity is not None:
            snapshot[identity.pid] = identity
    return snapshot


def _descendants(
    root: _ProcIdentity, snapshot: Mapping[int, _ProcIdentity]
) -> dict[int, _ProcIdentity]:
    found: dict[int, _ProcIdentity] = {}
    for candidate in snapshot.values():
        if candidate.pid == root.pid:
            continue
        cursor = candidate
        visited: set[int] = set()
        while cursor.ppid not in visited:
            if cursor.ppid == root.pid:
                found[candidate.pid] = candidate
                break
            visited.add(cursor.ppid)
            parent = snapshot.get(cursor.ppid)
            if parent is None:
                break
            cursor = parent
    return found


def _is_ancestor_of_self(pid: int, snapshot: Mapping[int, _ProcIdentity]) -> bool:
    cursor = snapshot.get(os.getpid())
    visited: set[int] = set()
    while cursor is not None and cursor.ppid not in visited:
        if cursor.ppid == pid:
            return True
        visited.add(cursor.ppid)
        cursor = snapshot.get(cursor.ppid)
    return False


def _thaw(processes: Iterable[_OwnedProcess]) -> None:
    for process in processes:
        _signal_owned(process, signal.SIGCONT)


def _terminate_posix_tree(root: _ProcIdentity) -> str:
    """Freeze a proven descendant tree, then kill every session within it."""
    snapshot = _proc_snapshot()
    current_root = snapshot.get(root.pid)
    if (
        root.pid <= 1
        or current_root is None
        or current_root.starttime != root.starttime
        or _is_ancestor_of_self(root.pid, snapshot)
    ):
        log.error("refusing to kill protected ancestor pid %s", root.pid)
        return "refused"

    root_handle = _open_owned_process(root)
    if root_handle is None:
        return "vanished"
    owned: dict[int, _OwnedProcess] = {root.pid: root_handle}
    if not _signal_owned(root_handle, signal.SIGSTOP):
        _close_owned(owned.values())
        return "refused" if _same_process(root) else "vanished"

    killed = False
    try:
        # Each pass stops descendants found in the preceding snapshot. Once no new pid is
        # found, every proven member is frozen and the tree is stable across sessions.
        for _ in range(16):
            snapshot = _proc_snapshot()
            current_root = snapshot.get(root.pid)
            if current_root is None or current_root.starttime != root.starttime:
                log.error("refusing pid %s tree kill: root identity changed", root.pid)
                return "refused"
            descendants = _descendants(root, snapshot)
            new = [identity for pid, identity in descendants.items() if pid not in owned]
            if not new:
                break
            for identity in new:
                if identity.pid == os.getpid():
                    log.error("refusing tree kill because it contains this process")
                    _thaw(owned.values())
                    return "refused"
                handle = _open_owned_process(identity)
                if handle is None:
                    continue
                owned[identity.pid] = handle
                if not _signal_owned(handle, signal.SIGSTOP) and _same_process(identity):
                    log.error("refusing pid %s tree kill: descendant could not be frozen", root.pid)
                    return "refused"
        else:
            log.error("refusing pid %s tree kill: descendants did not stabilise", root.pid)
            _thaw(owned.values())
            return "refused"

        # Force-stop must not run the supervisor's graceful SIGTERM handler: that handler
        # can launch report work during shutdown and create a child outside this frozen
        # snapshot. Cooperative API shutdown gives supervisors their grace before calling
        # this primitive. Descendants die first; the root stays frozen until they are gone.
        ordered = sorted(owned.values(), key=lambda item: item.identity.pid == root.pid)
        for process in ordered:
            if not _signal_owned(process, signal.SIGKILL) and _same_process(process.identity):
                log.error("pid %s survived SIGKILL", process.identity.pid)
                return "refused"

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and any(
            _same_process(process.identity) for process in ordered
        ):
            time.sleep(0.05)
        survivors = [process.identity.pid for process in ordered if _same_process(process.identity)]
        if survivors:
            log.error("pids survived SIGKILL: %s", survivors)
            return "refused"
        killed = True
        return "killed"
    finally:
        if not killed:
            _thaw(owned.values())
        _close_owned(owned.values())


def process_image(pid: int) -> str | None:
    """The executable name behind a pid, or None when nothing is running under it.

    Linux reads procfs and rejects zombies or identity changes. Windows invokes `tasklist`
    outside the gate; its argv contains only constants and an integer we formatted.
    """
    if not IS_WINDOWS:
        before = _read_proc_identity(pid)
        if before is None:
            return None
        try:
            target = os.readlink(Path("/proc") / str(pid) / "exe")
        except OSError:
            return None
        after = _read_proc_identity(pid)
        if after is None or after.starttime != before.starttime:
            return None
        return Path(target.removesuffix(" (deleted)")).name
    try:
        completed = subprocess.run(
            ["tasklist.exe", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            # The identity check is a single cheap query, and this runs while its caller
            # still holds a `PARALLEL_CALLS` slot on a call that has already failed. A
            # long stall here is pure added latency on top of the ceiling that was breached.
            timeout=5,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("tasklist for pid %s failed: %s", pid, exc)
        return None
    text = completed.stdout.decode("utf-8", errors="replace").strip()
    if not text or not text.startswith('"'):
        return None
    return text.split('"')[1]


def terminate_pid_tree(pid: int, *, expected_images: Sequence[str]) -> str:
    """Kill a pid and everything under it, after proving the pid is who we think it is.

    Pids are recycled. A tree kill on a stale pid kills whatever inherited it, so the image
    name is checked first and a mismatch refuses the kill. Linux additionally pins procfs
    start times (and pidfds when available), freezes the root, enumerates and freezes every
    descendant across sessions, then kills only that proven snapshot. Returns `killed`,
    `refused` or `vanished`.

    The image check alone is not proof of identity, because this application is itself a
    Python process: callers that kill by a *recorded* pid must establish that the record is
    still current before trusting it (`controls` does this with the run's heartbeat). What
    is settled here is the one case no caller could ever legitimately want — killing the
    process making the call, and with `/T` every sibling it shares a tree with.

    Synchronous on purpose: the controls API and the reconciler are ordinary request
    handlers, and this is the only work they do that needs a subprocess.
    """
    if pid == os.getpid():
        log.error("refusing to kill pid %s: that is this process", pid)
        return "refused"

    image = process_image(pid)
    if image is None:
        return "vanished"

    wanted = {name.lower() for name in expected_images}
    if wanted and image.lower() not in wanted:
        log.error(
            "refusing to kill pid %s: it is %s, expected one of %s", pid, image, sorted(wanted)
        )
        return "refused"

    if not IS_WINDOWS:
        identity = _read_proc_identity(pid)
        if identity is None:
            return "vanished"
        return _terminate_posix_tree(identity)

    if IS_WINDOWS:
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=20,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("taskkill for pid %s failed: %s", pid, exc)
    return "killed"


async def kill_process_tree(
    process: asyncio.subprocess.Process, *, expected_images: Sequence[str]
) -> str:
    """Tree-kill a child we hold a handle to, then reap it.

    The verification and the kill are `terminate_pid_tree`'s; this adds what only a handle
    can do — noticing the child already exited, and waiting for it to be reaped so its
    pipes close. Returns `exited`, `killed`, `refused` or `vanished`.
    """
    if process.returncode is not None:
        return "exited"

    pid = process.pid
    outcome = await asyncio.to_thread(
        terminate_pid_tree, pid, expected_images=tuple(expected_images)
    )
    if outcome == "refused":
        # Left alive on purpose, and not reaped either. An audit proposed killing the
        # handle here on the reasoning that a mismatched image means our child is already
        # gone — but we still hold its handle, which is exactly what stops its pid being
        # recycled, so the premise does not hold and `process.kill()` would be a kill this
        # function exists to refuse. The leak is one handle on a path that cannot occur
        # while the handle is open.
        return "refused"

    with _suppress_process_errors():
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=20)
    except TimeoutError:  # pragma: no cover — force-kill does not usually miss
        log.error("pid %s survived the tree kill", pid)
    return outcome


class _suppress_process_errors:
    """`process.kill()` on an already-dead child raises; that is not an error here."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_: object) -> bool:
        return exc_type is not None and issubclass(exc_type, (ProcessLookupError, OSError))


# ------------------------------------------------------------------------------ probe


async def probe_version(exe: Path | str | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
    """`claude --version`, for `GET /health`.

    The one spawn that does not go through the role gate: it carries no run data, no prompt
    and no spend, and it has to work before any run exists. It still runs from a scratch
    directory the CLI cannot mistake for a project, and with the same scrubbed environment.
    """
    try:
        binary = Path(exe).resolve() if exe else resolve_claude_exe()
    except (ClaudeNotFound, OSError) as exc:
        return {"installed": False, "version": None, "error": str(exc)}

    scratch = Path(os.environ.get("TEMP", ".")).resolve()
    try:
        process = await asyncio.create_subprocess_exec(
            str(binary),
            "--version",
            cwd=str(scratch if scratch.is_dir() else Path.cwd()),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env(),
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
            if IS_WINDOWS else 0,
            start_new_session=not IS_WINDOWS,
        )
        raw, raw_err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (OSError, TimeoutError) as exc:
        return {"installed": False, "version": None, "error": f"{type(exc).__name__}: {exc}"}

    text = raw.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        detail = raw_err.decode("utf-8", errors="replace").strip() or text
        return {"installed": False, "version": None, "error": detail[:400]}
    match = re.search(r"\d+\.\d+\.\d+", text)
    return {
        "installed": True,
        "version": match.group(0) if match else (text.splitlines() or [""])[0][:80],
        "path": str(binary),
    }


def python_executable() -> str:
    """`sys.executable`, resolved. The supervisor spawns itself with this (plan 3A.1)."""
    return str(Path(sys.executable).resolve())
