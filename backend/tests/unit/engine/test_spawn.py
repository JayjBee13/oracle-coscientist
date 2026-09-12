"""The spawn gate: what may be launched, from where, with what, and how it is killed.

Nothing here talks to the real CLI. The subject is the gate itself, so the process it
launches is a Python stub — which also proves the gate's checks are real rather than
tautologies about one specific binary.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

import app.engine.spawn as spawn_module
from app.engine.spawn import (
    ARGV_CHAR_CEILING,
    CODEX_EXE_ENV,
    ClaudeNotFound,
    CodexNotFound,
    SpawnRefused,
    child_env,
    kill_process_tree,
    probe_version,
    process_image,
    recorded_spawns,
    reset_claude_exe_cache,
    resolve_claude_exe,
    resolve_codex_exe,
    spawn_role_process,
)

STUB = Path(__file__).resolve().parents[3] / "tests" / "support" / "stub_claude.py"
PYTHON = str(Path(sys.executable).resolve())


class Refused(RuntimeError):
    """Stands in for BudgetExhausted: the gate only cares that the guard raises."""


def stub_argv(*extra: str) -> list[str]:
    return [PYTHON, "-X", "utf8", str(STUB), *extra]


async def spawn(tmp_path: Path, *, argv: list[str] | None = None, **kwargs):
    """Run the stub through the real gate, with the boring arguments filled in."""
    options: dict = {
        "cwd": tmp_path,
        "workdir_root": tmp_path,
        "stdin_text": "ROLE: ranking.\n\nprompt body",
        "budget_guard": lambda: None,
        "allowed_binaries": [PYTHON],
    }
    options.update(kwargs)
    return await spawn_role_process(argv or stub_argv(), **options)


def records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def record_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "stub-record.jsonl"
    monkeypatch.setenv("STUB_CLAUDE_RECORD", str(path))
    return path


# --- resolving the executable -------------------------------------------------------------


def npm_layout(root: Path, *, exe_name: str = "claude.exe") -> Path:
    """A miniature of the real npm install: a shim beside the package it points into."""
    package = root / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
    package.mkdir(parents=True)
    exe = package / exe_name
    fake_native(exe)
    shim = root / "claude.cmd"
    shim.write_text(
        '@ECHO off\r\n"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe" %*\r\n',
        encoding="utf-8",
    )
    return shim


def native_elf(path: Path) -> Path:
    """A minimal executable-looking file for resolver tests; it is never launched."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x7fELF" + b"\0" * 32)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def fake_native(path: Path) -> Path:
    if os.name != "nt":
        return native_elf(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ")
    return path


def test_the_shim_resolves_to_the_executable_beside_it(tmp_path: Path):
    shim = npm_layout(tmp_path)

    resolved = resolve_claude_exe(shim)

    assert resolved.suffix == ".exe"
    assert resolved == (tmp_path / "node_modules/@anthropic-ai/claude-code/bin/claude.exe")


def test_the_shim_is_read_when_the_package_layout_moved(tmp_path: Path):
    """npm has relaid this directory out before; the shim itself names the truth."""
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    fake_native(vendor / "claude.exe")
    shim = tmp_path / "claude.cmd"
    shim.write_text('@ECHO off\r\n"%dp0%\\vendor\\claude.exe" %*\r\n', encoding="utf-8")

    assert resolve_claude_exe(shim) == vendor / "claude.exe"


def test_a_shim_pointing_nowhere_is_refused_rather_than_spawned(tmp_path: Path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@ECHO off\r\n", encoding="utf-8")

    with pytest.raises(ClaudeNotFound, match="past the npm shim"):
        resolve_claude_exe(shim)


def test_the_installed_cli_resolves_to_a_real_exe():
    reset_claude_exe_cache()
    if shutil.which("claude") is None:  # pragma: no cover — depends on the machine
        pytest.skip("claude is not installed here")

    resolved = resolve_claude_exe()

    assert resolved.name.lower() == "claude.exe"
    assert resolved.is_file()


def test_resolution_is_cached_so_it_does_not_stat_on_every_call(tmp_path: Path):
    shim = npm_layout(tmp_path)
    reset_claude_exe_cache()

    first = resolve_claude_exe(shim)
    shutil.rmtree(tmp_path / "node_modules")

    assert resolve_claude_exe(shim) == first


def test_linux_claude_accepts_the_native_elf_even_when_it_keeps_exe_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(spawn_module, "IS_WINDOWS", False)
    reset_claude_exe_cache()
    binary = native_elf(tmp_path / "claude.exe")

    assert resolve_claude_exe(binary) == binary.resolve()


def test_linux_claude_refuses_an_executable_shell_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(spawn_module, "IS_WINDOWS", False)
    reset_claude_exe_cache()
    shim = tmp_path / "claude"
    shim.write_text("#!/bin/sh\nexec node cli.js\n", encoding="utf-8")
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    with pytest.raises(ClaudeNotFound, match="native Claude executable"):
        resolve_claude_exe(shim)


# --- the codex shim, which is a different shape -------------------------------------------


def codex_layout(tmp_path: Path, *, triple: str | None = None) -> Path:
    """npm's codex tree as it exists on this box, verified 2026-08-11.

    Note what the shim says: `bin\\codex.js`, not an executable. That is the whole reason
    codex needs its own resolver — the Claude one reads the shim for a `.exe` and finds none.
    """
    package = tmp_path / "node_modules" / "@openai" / "codex"
    if os.name == "nt":
        platform_package = "codex-win32-x64"
        triple = triple or "x86_64-pc-windows-msvc"
        executable = "codex.exe"
    else:
        platform_package = "codex-linux-x64"
        triple = triple or "x86_64-unknown-linux-musl"
        executable = "codex"
    vendored = (
        package / "node_modules" / "@openai" / platform_package / "vendor" / triple / "bin"
    )
    vendored.mkdir(parents=True)
    fake_native(vendored / executable)
    (package / "bin").mkdir(parents=True, exist_ok=True)
    (package / "bin" / "codex.js").write_text("#!/usr/bin/env node\n", encoding="utf-8")
    shim = tmp_path / "codex.cmd"
    shim.write_text(
        '@ECHO off\r\nnode "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n',
        encoding="utf-8",
    )
    return shim


def test_the_codex_shim_resolves_to_the_vendored_binary_it_never_names(tmp_path: Path):
    """Going through `codex.cmd` yields cmd.exe → node.exe → codex.exe, with cmd.exe
    resident as the parent — so the pid we would be handed is not the pid that is billing,
    and killing it orphans a running model turn. Spawning the vendored exe directly makes
    the pid we hold the pid that matters."""
    reset_claude_exe_cache()
    shim = codex_layout(tmp_path)

    resolved = resolve_codex_exe(shim)

    assert resolved.name.lower() == ("codex.exe" if os.name == "nt" else "codex")
    assert resolved.is_file()


def test_the_claude_resolver_cannot_find_the_codex_binary(tmp_path: Path):
    """Stated as a test because "reuse `_shim_targets`" is the obvious wrong answer: it
    regexes the shim for a `.exe`, and codex.cmd contains none."""
    reset_claude_exe_cache()
    shim = codex_layout(tmp_path)

    with pytest.raises(ClaudeNotFound):
        resolve_claude_exe(shim)


def test_the_codex_binary_is_found_when_npm_relays_the_tree_out(tmp_path: Path):
    """The documented paths first, the recursive glob as the fallback that keeps working."""
    reset_claude_exe_cache()
    package = tmp_path / "node_modules" / "@openai" / "codex"
    triple = "x86_64-pc-windows-msvc" if os.name == "nt" else "x86_64-unknown-linux-musl"
    executable = "codex.exe" if os.name == "nt" else "codex"
    moved = package / "somewhere" / "else" / "vendor" / triple / "bin"
    moved.mkdir(parents=True)
    fake_native(moved / executable)
    shim = tmp_path / "codex.cmd"
    shim.write_text("@ECHO off\r\nnode codex.js %*\r\n", encoding="utf-8")

    assert resolve_codex_exe(shim) == (moved / executable).resolve()


def test_a_codex_shim_with_no_vendored_binary_is_refused_rather_than_spawned(tmp_path: Path):
    reset_claude_exe_cache()
    shim = tmp_path / "codex.cmd"
    shim.write_text("@ECHO off\r\n", encoding="utf-8")

    with pytest.raises(CodexNotFound, match="past the npm shim"):
        resolve_codex_exe(shim)


def test_the_codex_override_env_var_mirrors_the_claude_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    reset_claude_exe_cache()
    exe = tmp_path / ("codex.exe" if os.name == "nt" else "codex")
    fake_native(exe)
    monkeypatch.setenv(CODEX_EXE_ENV, str(exe))

    assert resolve_codex_exe() == exe.resolve()


def test_the_two_resolvers_do_not_share_a_cache_entry(tmp_path: Path):
    """They are cached by the same dict and would collide on the empty-hint key."""
    reset_claude_exe_cache()
    claude = resolve_claude_exe(npm_layout(tmp_path / "a"))
    codex = resolve_codex_exe(codex_layout(tmp_path / "b"))

    assert claude != codex
    assert claude.name.lower() == "claude.exe"
    assert codex.name.lower() == ("codex.exe" if os.name == "nt" else "codex")


@pytest.mark.parametrize(
    ("machine", "platform_package", "triple"),
    [
        ("x86_64", "codex-linux-x64", "x86_64-unknown-linux-musl"),
        ("aarch64", "codex-linux-arm64", "aarch64-unknown-linux-musl"),
    ],
)
def test_linux_codex_js_resolves_to_its_architecture_specific_native_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
    platform_package: str,
    triple: str,
):
    monkeypatch.setattr(spawn_module, "IS_WINDOWS", False)
    monkeypatch.setattr(spawn_module.platform, "machine", lambda: machine)
    reset_claude_exe_cache()
    package = tmp_path / "node_modules" / "@openai" / "codex"
    js = package / "bin" / "codex.js"
    js.parent.mkdir(parents=True)
    js.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    binary = native_elf(
        package
        / "node_modules"
        / "@openai"
        / platform_package
        / "vendor"
        / triple
        / "bin"
        / "codex"
    )

    assert resolve_codex_exe(js) == binary.resolve()


@pytest.mark.skipif(os.name == "nt", reason="Windows paths are case-insensitive")
def test_linux_allowlist_keys_remain_case_sensitive(tmp_path: Path):
    upper = native_elf(tmp_path / "Model")
    lower = native_elf(tmp_path / "model")
    spawn_module.allow_binary(upper)

    with pytest.raises(SpawnRefused, match="not an allowed binary"):
        spawn_module._validate_binary(str(lower), None)


# --- what the gate refuses ----------------------------------------------------------------


async def test_a_connection_string_in_argv_is_refused(tmp_path: Path):
    dsn = "postgresql+psycopg://ai_coscientist_gui_app:secret@127.0.0.1:5432/db"

    with pytest.raises(SpawnRefused, match="://"):
        await spawn(tmp_path, argv=stub_argv("--database-url", dsn))


async def test_an_unregistered_binary_is_refused(tmp_path: Path):
    other = tmp_path / "somewhere.exe"
    other.write_bytes(b"MZ")

    with pytest.raises(SpawnRefused, match="not an allowed binary"):
        await spawn(tmp_path, argv=[str(other), "--version"])


async def test_a_relative_binary_is_refused(tmp_path: Path):
    with pytest.raises(SpawnRefused, match="absolute path"):
        await spawn(tmp_path, argv=["claude.exe", "-p"])


@pytest.mark.skipif(os.name != "nt", reason="the .exe rule is a Windows rule")
async def test_a_shim_is_refused_at_the_gate_too(tmp_path: Path):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@ECHO off\r\n", encoding="utf-8")

    with pytest.raises(SpawnRefused, match="not an .exe"):
        await spawn(tmp_path, argv=[str(shim)], allowed_binaries=[shim])


async def test_a_cwd_outside_the_run_workdir_is_refused(tmp_path: Path):
    workdir = tmp_path / "run"
    elsewhere = tmp_path / "elsewhere"
    workdir.mkdir()
    elsewhere.mkdir()

    with pytest.raises(SpawnRefused, match="outside the run workdir"):
        await spawn(tmp_path, cwd=elsewhere, workdir_root=workdir)


async def test_a_missing_cwd_is_refused(tmp_path: Path):
    with pytest.raises(SpawnRefused, match="cwd does not exist"):
        await spawn(tmp_path, cwd=tmp_path / "gone")


async def test_an_argv_over_the_windows_ceiling_is_refused_with_the_culprit(tmp_path: Path):
    with pytest.raises(SpawnRefused, match=f"over the {ARGV_CHAR_CEILING}"):
        await spawn(tmp_path, argv=stub_argv("--system-prompt", "x" * (ARGV_CHAR_CEILING + 1)))


async def test_the_budget_is_checked_before_anything_is_launched(
    tmp_path: Path, record_file: Path
):
    def guard() -> None:
        raise Refused("budget")

    before = len(recorded_spawns())

    with pytest.raises(Refused):
        await spawn(tmp_path, budget_guard=guard)

    assert records(record_file) == [], "the stub ran despite the budget refusal"
    assert len(recorded_spawns()) == before, "a refused spawn was recorded as if it happened"


# --- what the gate records ----------------------------------------------------------------


async def test_every_spawn_is_recorded_with_an_argv_that_carries_no_credentials(
    tmp_path: Path,
):
    argv_log = tmp_path / "spawn-argv.jsonl"

    spawned = await spawn(tmp_path, argv_log=argv_log)
    await spawned.process.communicate()

    entry = records(argv_log)[-1]
    assert entry["argv"] == list(spawned.argv)
    assert entry["cwd"] == str(tmp_path).replace("\\", "/")
    assert entry["stdin_chars"] > 0
    assert not any("://" in item for item in entry["argv"])
    assert recorded_spawns()[-1]["argv"] == list(spawned.argv)


# --- what the child receives --------------------------------------------------------------


async def test_the_prompt_arrives_on_stdin_and_never_in_argv(tmp_path: Path, record_file: Path):
    prompt = "ROLE: ranking.\n\nWhich hypothesis wins? h001 vs h002 — π café 日本語"

    spawned = await spawn(tmp_path, stdin_text=prompt)
    await spawned.process.communicate()

    entry = records(record_file)[-1]
    assert entry["stdin"] == prompt, "stdin did not survive the round trip as UTF-8"
    assert not any(prompt in item for item in entry["argv"])


async def test_the_child_does_not_inherit_the_secrets_the_supervisor_holds(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://app:pw@127.0.0.1:5432/db")
    monkeypatch.setenv("APP_AUTH_TOKEN", "t0ken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-be-billed")
    monkeypatch.setenv("PG_PASSWORD", "hunter2")
    monkeypatch.setenv("HTTPS_PROXY", "http://user:pw@proxy.local:8080")
    monkeypatch.setenv("COSCIENTIST_HARMLESS", "keep-me")

    spawned = await spawn(tmp_path)
    await spawned.process.communicate()

    env = records(record_file)[-1]["env"]
    keys = {key.upper() for key in env}
    for leaked in ("DATABASE_URL", "APP_AUTH_TOKEN", "ANTHROPIC_API_KEY", "PG_PASSWORD"):
        assert leaked not in keys, f"{leaked} reached the model process"
    assert "HTTPS_PROXY" not in keys, "a credentialed URL reached the model process"
    assert env["COSCIENTIST_HARMLESS"] == "keep-me"
    # The CLI's OAuth credentials live under the user profile; scrubbing must not cost it
    # the variables it needs to find them.
    assert {"PATH", "APPDATA", "SYSTEMROOT"} <= keys or os.name != "nt"


def test_the_scrubber_keeps_what_the_cli_needs_to_work():
    env = child_env()

    assert env, "the scrubber emptied the environment"
    assert all("PASSWORD" not in key.upper() for key in env)


async def test_the_child_runs_in_its_own_process_group_on_its_own_pipes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    captured: dict = {}

    class Dummy:
        stdin = stdout = stderr = None
        returncode = 0
        pid = 4242

    async def fake_exec(*argv, **kwargs):
        captured.update(kwargs)
        captured["argv"] = argv
        return Dummy()

    monkeypatch.setattr("app.engine.spawn.asyncio.create_subprocess_exec", fake_exec)

    await spawn(tmp_path)

    assert captured["stdin"] is asyncio.subprocess.PIPE
    assert captured["stdout"] is asyncio.subprocess.PIPE
    assert captured["stderr"] is asyncio.subprocess.PIPE
    if os.name == "nt":
        assert captured["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        assert captured["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert captured["start_new_session"] is False
    else:
        assert captured["creationflags"] == 0
        assert captured["start_new_session"] is True


# --- killing ------------------------------------------------------------------------------


async def wait_for_pid_to_vanish(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if os.name != "nt":
            if process_image(pid) is None:
                return True
            await asyncio.sleep(0.05)
            continue
        probe = await asyncio.create_subprocess_exec(
            "tasklist.exe",
            "/FI",
            f"PID eq {pid}",
            "/FO",
            "CSV",
            "/NH",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await probe.communicate()
        if not out.decode("utf-8", "replace").strip().startswith('"'):
            return True
        await asyncio.sleep(0.25)
    return False


async def test_the_whole_tree_dies_not_just_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "slow_tree")
    spawned = await spawn(tmp_path)

    grandchild = None
    while grandchild is None:
        line = await asyncio.wait_for(spawned.process.stdout.readline(), timeout=30)
        grandchild = json.loads(line).get("grandchild_pid")

    outcome = await kill_process_tree(spawned.process, expected_images=(spawned.image,))

    assert outcome == "killed"
    assert spawned.process.returncode is not None
    assert await wait_for_pid_to_vanish(grandchild), "the grandchild outlived the tree kill"


async def test_a_pid_that_is_not_ours_is_never_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Pids get recycled. Killing the wrong one takes down another run's supervisor."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "slow")
    spawned = await spawn(tmp_path)

    outcome = await kill_process_tree(spawned.process, expected_images=("notepad.exe",))

    assert outcome == "refused"
    assert spawned.process.returncode is None
    await kill_process_tree(spawned.process, expected_images=(spawned.image,))


async def test_killing_an_exited_process_is_a_no_op(tmp_path: Path):
    spawned = await spawn(tmp_path)
    await spawned.process.communicate()

    assert await kill_process_tree(spawned.process, expected_images=(spawned.image,)) == "exited"


def test_posix_process_image_treats_a_zombie_as_vanished(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(spawn_module, "IS_WINDOWS", False)
    monkeypatch.setattr(spawn_module, "_read_proc_identity", lambda _pid: None)
    monkeypatch.setattr(spawn_module.os, "readlink", lambda _path: pytest.fail("readlink called"))

    assert process_image(4242) is None


def test_posix_tree_kill_refuses_a_recycled_root_pid(monkeypatch: pytest.MonkeyPatch):
    root = spawn_module._ProcIdentity(4242, 1, 4242, 4242, "S", 100)
    recycled = spawn_module._ProcIdentity(4242, 1, 4242, 4242, "S", 200)
    monkeypatch.setattr(spawn_module, "_proc_snapshot", lambda: {4242: recycled})
    monkeypatch.setattr(
        spawn_module,
        "_open_owned_process",
        lambda _identity: pytest.fail("recycled pid was opened"),
    )

    assert spawn_module._terminate_posix_tree(root) == "refused"


async def test_a_vanished_child_is_still_reaped(monkeypatch: pytest.MonkeyPatch):
    class Dummy:
        returncode = None
        pid = 4242
        killed = False
        waited = False

        def kill(self):
            self.killed = True

        async def wait(self):
            self.waited = True
            self.returncode = -9
            return self.returncode

    process = Dummy()
    monkeypatch.setattr(spawn_module, "terminate_pid_tree", lambda *_args, **_kwargs: "vanished")

    outcome = await kill_process_tree(process, expected_images=("python",))

    assert outcome == "vanished"
    assert process.killed is True
    assert process.waited is True


# --- probe --------------------------------------------------------------------------------


async def test_probe_reads_a_version_out_of_the_binary_it_is_given():
    """`--version` and nothing else: a health probe must not be able to start a run."""
    result = await probe_version(PYTHON, timeout=60)

    assert result["installed"] is True
    assert re.fullmatch(r"\d+\.\d+\.\d+", result["version"])
    assert result["path"] == PYTHON


async def test_probe_reports_a_missing_binary_instead_of_raising(tmp_path: Path):
    result = await probe_version(tmp_path / f"absent-{uuid4().hex}.exe")

    assert result["installed"] is False
    assert result["version"] is None
    assert result["error"]
