"""Every flag the argv builders emit, checked against the CLI that would receive it.

The stubs under `tests/support` are Python scripts, and a Python script ignores arguments it
does not recognise. That is the right shape for replaying recorded output and exactly the
wrong shape for noticing a flag the real binary rejects: `build_codex_argv` emitted
`-a never` for as long as the module existed, every test in the suite passed, and the first
live call died in 0.04 seconds with `error: unexpected argument '-a' found` — `-a` is an
*interactive*-only option that `codex exec` has never had.

So this asks the installed binaries instead of a recording of them. Each CLI is asked for its
own `--help`, the answer is parsed for the options it advertises, and every flag the builder
would send has to appear in that set — for every role, and for both the schema-file and
prompt-stated paths. Nothing here starts a model, spends a token or touches the network; the
cost is two `--help` invocations for the whole module.

Skips rather than fails when a CLI is not installed: the suite has to pass on a machine with
neither. It does *not* skip when the help parses to nothing recognisable — a help format that
changed out from under this test is the case where a green run would be a lie.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import pytest

from app.engine.claude_runner import build_claude_argv
from app.engine.codex_runner import build_codex_argv
from app.engine.runners import ROLES, RoleConfig, role_config
from app.engine.spawn import CliNotFound, resolve_claude_exe, resolve_codex_exe

HELP_TIMEOUT_S = 60.0

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}

# An option *definition* line, in both help dialects: clap indents a long-only option by six
# spaces and a short/long pair by two, commander indents every one by two. Description text
# is indented further in both, which is what keeps a flag mentioned inside prose — `-c
# model="o3"` in codex's own `--config` example — out of the advertised set.
_DEFINITION_LINE = re.compile(r"^ {2,6}-")
_FLAG = re.compile(r"--?[A-Za-z][A-Za-z0-9-]*")


def _advertised(help_text: str) -> frozenset[str]:
    """Every option the help lists, short and long spellings alike."""
    found: set[str] = set()
    for line in help_text.splitlines():
        if not _DEFINITION_LINE.match(line):
            continue
        for token in re.split(r"[\s,=|]+", line.strip()):
            if _FLAG.fullmatch(token):
                found.add(token)
    return frozenset(found)


def _help_text(argv: list[str]) -> str | None:
    """`<cli> --help`, or None when the binary could not answer.

    Run from a temporary directory so neither CLI mistakes this repository for a project it
    should be reading, and with `stdin` closed so a help that decides to prompt cannot hang
    the suite.
    """
    try:
        with tempfile.TemporaryDirectory() as scratch:
            completed = subprocess.run(  # noqa: S603 — a constant argv and a resolved binary
                argv,
                capture_output=True,
                cwd=scratch,
                stdin=subprocess.DEVNULL,
                timeout=HELP_TIMEOUT_S,
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


@lru_cache(maxsize=1)
def _codex_help() -> str | None:
    try:
        exe = resolve_codex_exe()
    except (CliNotFound, OSError):
        return None
    return _help_text([str(exe), "exec", "--help"])


@lru_cache(maxsize=1)
def _claude_help() -> str | None:
    try:
        exe = resolve_claude_exe()
    except (CliNotFound, OSError):
        return None
    return _help_text([str(exe), "--help"])


def _options(help_text: str | None, cli: str) -> frozenset[str]:
    if help_text is None:
        pytest.skip(f"{cli} is not installed here; there is nothing to check the argv against")
    options = _advertised(help_text)
    # `--help` is the one option this engine never sends, which makes it the honest canary
    # for "the options section was found and parsed" — an assertion on a flag we do emit
    # would pass for the wrong reason.
    assert "--help" in options and len(options) >= 10, (
        f"{cli} --help parsed to {len(options)} option(s); the help format changed and this "
        "test can no longer tell a real flag from an invented one"
    )
    return options


def _cfg(role: str, *, schema: dict | None = SCHEMA) -> RoleConfig:
    return role_config(
        role,
        model="gpt-5.6-sol",
        effort="medium",
        system_prompt="SYSTEM INSTRUCTIONS.",
        json_schema=schema,
    )


def _emitted(argv: list[str]) -> list[str]:
    """The flags in an argv, ignoring argv[0] and the bare `-` that means stdin."""
    return [item for item in argv[1:] if item.startswith("-") and item != "-"]


# --- codex ---------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("schema_file", [True, False], ids=["schema-file", "prompt-mode"])
def test_every_codex_flag_the_builder_emits_is_one_the_installed_cli_advertises(
    role: str, schema_file: bool, tmp_path: Path
):
    options = _options(_codex_help(), "codex exec")
    argv = build_codex_argv(
        _cfg(role), exe="C:/codex.exe", scratch=tmp_path, schema_file=schema_file
    )

    assert argv[1] == "exec"
    unknown = sorted({flag for flag in _emitted(argv) if flag not in options})
    assert not unknown, (
        f"`codex exec` does not advertise {unknown} — the {role} call would be refused "
        "before the model is ever reached"
    )


def test_codex_exec_still_reads_its_prompt_from_stdin(tmp_path: Path):
    """The whole invocation rests on it: a prompt in argv would be public, size-capped and
    shell-mangled, and passing both a prompt argument and piped stdin concatenates them."""
    help_text = _codex_help()
    if help_text is None:
        pytest.skip("codex is not installed here")

    assert "stdin" in help_text.lower()


# --- claude --------------------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("schema", [SCHEMA, None], ids=["schema", "no-schema"])
def test_every_claude_flag_the_builder_emits_is_one_the_installed_cli_advertises(
    role: str, schema: dict | None
):
    """Cheap enough to be worth doing on both lanes: commander's help lists every option at
    the same indent as clap's, so one parser covers both.

    The Claude runner has its own belt for this — it asserts the invariants against the
    `system/init` envelope the CLI reports back, which catches a flag that is *accepted and
    ignored*. This is the brace: a flag that would be rejected outright never gets far enough
    for an init envelope to exist."""
    options = _options(_claude_help(), "claude")
    argv = build_claude_argv(_cfg(role, schema=schema), str(uuid4()), exe="C:/claude.exe")

    unknown = sorted({flag for flag in _emitted(argv) if flag not in options})
    assert not unknown, (
        f"`claude` does not advertise {unknown} — the {role} call would be refused before "
        "the model is ever reached"
    )
