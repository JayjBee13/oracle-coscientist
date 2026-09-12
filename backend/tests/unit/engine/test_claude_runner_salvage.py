"""What a Claude call killed at its ceiling is still allowed to be.

Salvage exists because run c4566ed2 threw away three complete answers that were already on
the wire when the deadline landed on the CLI's wrap-up. What it must not become is an
amnesty: the kill explains one thing, the missing result envelope, and the assembly path
used to return the salvaged payload without asking any of the other questions `_failure`
exists to ask. A result line that *did* arrive and said `is_error`, a grounded call whose
searches were denied, a transcript with no `system/init` in it — each of those was reported
as a good answer as long as the process was killed afterwards.

`_result` is exercised directly rather than through the stub. The trace is the entire input
to this decision, and building one by hand states the exact combination under test in three
lines instead of inventing a CLI that produces it.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from app.engine.claude_runner import ClaudeCliRunner, _Trace
from app.engine.runners import RoleConfig, role_config

PYTHON = str(Path(sys.executable).resolve())

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
ANSWER = {"ok": True}

INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "01999999-0000-7000-8000-000000000000",
    "tools": [],
    "mcp_servers": [],
    "slash_commands": [],
    "permissionMode": "default",
    "claude_code_version": "2.1.220",
}


def cfg(role: str = "ranking") -> RoleConfig:
    return role_config(
        role,
        model="claude-opus-5",
        effort="high",
        system_prompt="SYSTEM INSTRUCTIONS.",
        json_schema=SCHEMA,
    )


def answered_trace(**overrides: Any) -> _Trace:
    """A call whose model submitted its structured answer and was then killed."""
    trace = _Trace()
    trace.init = dict(INIT)
    trace.structured = dict(ANSWER)
    trace.structured_at = time.monotonic()
    trace.tool_uses = {"StructuredOutput": 1}
    for key, value in overrides.items():
        setattr(trace, key, value)
    return trace


def runner(tmp_path: Path) -> ClaudeCliRunner:
    return ClaudeCliRunner(workdir=tmp_path, budget_guard=lambda: None, exe=PYTHON)


def test_the_legitimate_salvage_is_still_a_success(tmp_path: Path):
    """The whole point of the path, pinned before anything is allowed to narrow it: a model
    that finished, a CLI killed mid-wrap-up, and an answer that is nobody's fault."""
    result = runner(tmp_path)._result(
        cfg(), answered_trace(), elapsed=200.0, salvaged="killed"
    )

    assert result.ok, result.error
    assert result.data == ANSWER
    assert result.degraded, "an answer taken off a killed process is not an ordinary success"
    assert result.telemetry["salvaged_after_timeout"] == "killed"


def test_a_result_envelope_that_said_is_error_is_not_redeemed_by_the_kill(tmp_path: Path):
    """The envelope arrived, the CLI then hung, and the deadline landed on the hang. The
    answer in the transcript belongs to a call the API reported as failed."""
    trace = answered_trace(
        result={
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "api_error_status": 529,
            "stop_reason": "error",
            "terminal_reason": "api_error",
            "result": "Overloaded",
        }
    )

    result = runner(tmp_path)._result(cfg(), trace, elapsed=200.0, salvaged="killed")

    assert not result.ok and result.data is None
    assert "529" in result.error
    assert result.telemetry["salvaged_after_timeout"] == "killed", (
        "the salvage is still what happened to it, and telemetry has to keep saying so"
    )


def test_a_denied_grounded_call_is_not_redeemed_by_the_kill(tmp_path: Path):
    """The failure that used to be invisible, arriving by the one door that did not check
    for it. A grounded role denied its search answered from memory."""
    trace = answered_trace(
        result={
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "permission_denials": [
                {"tool_name": "WebSearch", "tool_use_id": "toolu_017", "reason": "not allowed"}
            ],
        }
    )

    result = runner(tmp_path)._result(
        cfg("generation"), trace, elapsed=900.0, salvaged="killed"
    )

    assert not result.ok and result.data is None
    assert "permission denied on a grounded call" in result.error


def test_a_salvage_with_no_init_envelope_cannot_claim_its_invariants_were_checked(
    tmp_path: Path,
):
    """`--setting-sources` and `--strict-mcp-config` are flags the CLI would ignore in
    silence if either were ever renamed, and the init envelope is the only thing that says
    they were understood. A payload recovered from a transcript that never contained one is
    an answer from a call whose configuration was never verified."""
    trace = answered_trace(init=None)

    result = runner(tmp_path)._result(cfg(), trace, elapsed=200.0, salvaged="killed")

    assert not result.ok and result.data is None
    assert "no system/init envelope" in result.error
