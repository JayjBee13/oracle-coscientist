"""`ClaudeCliRunner` against a stub CLI: the argv law, and every way a call can end.

No test here runs the real `claude`. The stub replays envelopes recorded from it, which is
enough to pin the two things that actually matter — that we send a command line nobody has
to reason about twice, and that we believe a result only when the CLI has told us the call
was scoped the way we asked.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.engine.claude_runner import (
    FORBIDDEN_FLAGS,
    ClaudeCliRunner,
    _assert_argv_law,
    build_claude_argv,
)
from app.engine.runners import ROLES, AgentRunner, RoleConfig, role_config
from app.engine.spawn import spawn_role_process

STUB = Path(__file__).resolve().parents[3] / "tests" / "support" / "stub_claude.py"
PYTHON = str(Path(sys.executable).resolve())
SESSION = "0f4f6f7e-0000-4000-8000-000000000001"


class Refused(RuntimeError):
    """Stands in for BudgetExhausted."""


def cfg(role: str = "ranking", **overrides) -> RoleConfig:
    base = role_config(
        role,
        model="claude-opus-5",
        effort="medium",
        system_prompt="SYSTEM.",
        round=2,
        unit="h001",
        json_schema={"type": "object"},
    )
    return replace(base, **overrides) if overrides else base


def make_runner(tmp_path: Path, *, budget_guard=lambda: None) -> tuple[ClaudeCliRunner, list]:
    """A runner whose spawn puts the stub where `claude.exe` would be.

    The argv the runner built is passed through untouched and recorded, so the argv law is
    asserted against exactly what would have reached the real CLI.
    """
    seen: list[list[str]] = []
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"MZ")

    async def spawn(argv, **kwargs):
        seen.append(list(argv))
        return await spawn_role_process(
            [PYTHON, "-X", "utf8", str(STUB), *argv], allowed_binaries=[PYTHON], **kwargs
        )

    runner = ClaudeCliRunner(
        workdir=tmp_path, budget_guard=budget_guard, exe=exe, spawn=spawn
    )
    return runner, seen


@pytest.fixture
def record_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "stub-record.jsonl"
    monkeypatch.setenv("STUB_CLAUDE_RECORD", str(path))
    monkeypatch.delenv("STUB_CLAUDE_SCENARIO", raising=False)
    return path


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summary_of(result) -> dict:
    """The compact JSON summary the runner puts at the head of `raw_tail`."""
    return json.loads(result.raw_tail.splitlines()[0])


# --- the argv law -------------------------------------------------------------------------


def test_a_grounded_call_is_built_exactly_as_c1_says():
    argv = build_claude_argv(cfg("generation", effort="high"), SESSION, exe="C:/claude.exe")

    assert argv == [
        "C:/claude.exe",
        "-p",
        "--input-format",
        "text",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-opus-5",
        "--effort",
        "high",
        "--system-prompt",
        "SYSTEM.",
        "--tools",
        "WebSearch",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--session-id",
        SESSION,
        "--allowedTools",
        "WebSearch",
        "--json-schema",
        '{"type": "object"}',
    ]


def test_a_judging_call_states_empty_tools_and_grants_nothing():
    argv = build_claude_argv(cfg("ranking"), SESSION, exe="C:/claude.exe")

    assert argv == [
        "C:/claude.exe",
        "-p",
        "--input-format",
        "text",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        "claude-opus-5",
        "--effort",
        "medium",
        "--system-prompt",
        "SYSTEM.",
        "--tools",
        "",
        "--disable-slash-commands",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--session-id",
        SESSION,
        "--json-schema",
        '{"type": "object"}',
    ]
    assert "--allowedTools" not in argv


@pytest.mark.parametrize("role", ROLES)
def test_no_role_can_produce_an_argv_that_breaks_a_rule_we_paid_to_learn(role: str):
    prompt = "the task prompt, which must never appear on a command line"
    argv = build_claude_argv(cfg(role), SESSION, exe="C:/claude.exe")

    assert "--system-prompt" in argv and "--append-system-prompt" not in argv
    assert "--strict-mcp-config" in argv, "the host's MCP connectors must not reach a role call"
    assert not FORBIDDEN_FLAGS & set(argv)
    assert not any(token in item for item in argv for token in ("bypassPermissions", "://"))
    assert prompt not in argv


@pytest.mark.parametrize("role", ROLES)
def test_availability_and_permission_are_always_set_together(role: str):
    argv = build_claude_argv(cfg(role), SESSION, exe="C:/claude.exe")
    tools = argv[argv.index("--tools") + 1]

    if tools:
        assert argv[argv.index("--allowedTools") + 1] == tools
    else:
        assert "--allowedTools" not in argv


def test_the_law_refuses_an_argv_that_would_deny_itself():
    """The failure mode this catches cost 59K tokens per call before it was diagnosed."""
    with pytest.raises(ValueError, match="must mirror --tools"):
        _assert_argv_law(
            ["claude.exe", "--system-prompt", "s", "--strict-mcp-config", "--tools", "WebSearch"]
        )


def test_the_law_refuses_an_argv_that_would_let_the_host_s_mcp_servers_in():
    """`--setting-sources ""` does not cover MCP; the operator's connectors got in without it."""
    with pytest.raises(ValueError, match="--strict-mcp-config"):
        _assert_argv_law(["claude.exe", "--system-prompt", "s", "--tools", ""])


def test_the_law_refuses_an_argv_that_inherits_the_default_system_prompt():
    with pytest.raises(ValueError, match="replace the system prompt"):
        _assert_argv_law(["claude.exe", "-p", "--tools", ""])


@pytest.mark.parametrize("flag", sorted(FORBIDDEN_FLAGS))
def test_the_law_refuses_each_flag_that_caused_an_incident(flag: str):
    with pytest.raises(ValueError, match="forbidden flag"):
        _assert_argv_law(["claude.exe", "--system-prompt", "s", "--tools", "", flag, "x"])


def test_the_schema_travels_inline_and_parses_back_to_what_was_asked_for():
    schema = {"type": "object", "required": ["debate", "winner"]}
    argv = build_claude_argv(cfg("ranking", json_schema=schema), SESSION, exe="C:/c.exe")

    assert json.loads(argv[argv.index("--json-schema") + 1]) == schema


async def test_every_call_gets_a_fresh_session_id(tmp_path: Path, record_file: Path):
    runner, seen = make_runner(tmp_path)

    await runner.run_role("ranking", "first", cfg())
    await runner.run_role("ranking", "second", cfg())

    sessions = [argv[argv.index("--session-id") + 1] for argv in seen]
    assert len(set(sessions)) == 2
    assert all(re.fullmatch(r"[0-9a-f-]{36}", session) for session in sessions)


def test_the_runner_satisfies_the_protocol_the_orchestrator_depends_on(tmp_path: Path):
    runner, _ = make_runner(tmp_path)

    assert isinstance(runner, AgentRunner)
    assert runner.name == "claude"


# --- the happy path -----------------------------------------------------------------------


async def test_a_successful_call_returns_the_parsed_structured_output(
    tmp_path: Path, record_file: Path
):
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "Which wins, h001 or h002?", cfg())

    assert result.ok and result.error is None
    assert result.data["ok"] is True
    assert result.data["stdin_head"] == "ROLE: ranking."
    assert not result.degraded and not result.rate_limited


async def test_all_four_token_classes_and_the_cost_are_recorded(
    tmp_path: Path, record_file: Path
):
    """Input tokens alone under-report by ~700× once the prompt cache is warm."""
    runner, _ = make_runner(tmp_path)

    usage = (await runner.run_role("ranking", "prompt", cfg())).usage

    assert (usage.tokens_in, usage.tokens_out) == (12, 733)
    assert (usage.cache_creation, usage.cache_read) == (4102, 58610)
    assert usage.tokens_total == 63457
    assert usage.cost_usd == 0.0158
    assert usage.duration_ms == 3812


async def test_the_raw_tail_carries_the_whole_diagnosis(tmp_path: Path, record_file: Path):
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("generation", "prompt", cfg("generation"))
    summary = summary_of(result)

    for field in (
        "is_error",
        "subtype",
        "stop_reason",
        "terminal_reason",
        "api_error_status",
        "permission_denials",
        "num_turns",
        "modelUsage",
        "duration_ms",
        "total_cost_usd",
        "web_search_requests",
        "exit_code",
        "session_id",
    ):
        assert field in summary, f"{field} is not persisted; the Activity tab needs it"
    assert summary["num_turns"] == 1
    # The same facts, structured: raw_tail is for a human reading a failure, telemetry is
    # what the orchestrator puts on `call_finished` for a successful call nobody reads.
    assert result.telemetry == summary


async def test_searches_are_counted_from_the_transcript_not_the_providers_counter(
    tmp_path: Path, record_file: Path
):
    """The server-side counter cannot see a search the CLI ran on this machine.

    Live on 2026-08-02 a grounded call made three WebSearch calls, returned a cited answer,
    and reported `server_tool_use.web_search_requests == 0`. Trusting that number means
    concluding grounding is broken while it is working, so the count comes from the
    `tool_use` blocks the model actually emitted.
    """
    runner, _ = make_runner(tmp_path)

    grounded = await runner.run_role("generation", "prompt", cfg("generation"))
    judging = await runner.run_role("ranking", "prompt", cfg("ranking"))

    assert grounded.telemetry["web_searches"] == 2
    assert grounded.telemetry["tool_uses"] == {"WebSearch": 2}
    assert judging.telemetry["web_searches"] == 0
    assert judging.telemetry["tool_uses"] is None


async def test_the_prompt_is_delivered_on_stdin_under_its_role_header(
    tmp_path: Path, record_file: Path
):
    runner, seen = make_runner(tmp_path)
    prompt = "Compare h001 and h002 on novelty."

    await runner.run_role("ranking", prompt, cfg())

    entry = records(record_file)[-1]
    assert entry["stdin"] == f"ROLE: ranking.\n\n{prompt}"
    assert not any(prompt in item for item in seen[0])


async def test_each_call_runs_in_its_own_empty_scratch_directory_inside_the_workdir(
    tmp_path: Path, record_file: Path
):
    runner, _ = make_runner(tmp_path)

    await runner.run_role("ranking", "prompt", cfg())

    cwd = Path(records(record_file)[-1]["cwd"]).resolve()
    assert cwd.parent == (tmp_path / "calls").resolve()
    assert list((tmp_path / "calls").iterdir()) == [], "the scratch directory was left behind"


async def test_the_argv_of_every_call_is_written_to_the_run_ledger(
    tmp_path: Path, record_file: Path
):
    runner, seen = make_runner(tmp_path)

    await runner.run_role("ranking", "prompt", cfg())

    logged = records(tmp_path / "spawn-argv.jsonl")[-1]
    assert logged["argv"][-len(seen[0]) :] == seen[0]
    assert not any("://" in item for item in logged["argv"])


# --- the init invariants ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("bad_init_mcp", "mcp_servers is not empty"),
        ("bad_init_slash", "slash command"),
        ("bad_init_bypass", "permissionMode is bypassPermissions"),
        ("bad_init_tools", "tools ['Bash'] are available and were not asked for"),
        ("bad_init_missing", "tools ['WebSearch'] were requested but are not available"),
    ],
)
async def test_an_init_that_violates_an_invariant_fails_the_call_immediately(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch, scenario, expected
):
    """The CLI ignores unknown flags in silence, so this report is the only real gate.

    The stub keeps running for 30s after a bad init; a call that took that long would mean
    the runner had believed it and kept paying.
    """
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", scenario)
    runner, _ = make_runner(tmp_path)

    started = time.monotonic()
    result = await runner.run_role("generation", "prompt", cfg("generation"))

    assert not result.ok
    assert result.error.startswith("init assertion failed")
    assert expected in result.error
    assert result.data is None
    assert time.monotonic() - started < 20, "the call was not cut off at the violation"


async def test_a_result_without_an_init_envelope_is_not_trusted(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "no_init")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok
    assert "invariants could not be verified" in result.error


async def test_a_grounded_role_expects_its_tool_to_be_reported_back(
    tmp_path: Path, record_file: Path
):
    """The stub echoes `--tools`, so a grounded call passing here proves the mirror works."""
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("generation", "prompt", cfg("generation"))

    assert result.ok


async def test_the_schema_mechanism_is_allowed_to_appear_as_a_tool(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """`--json-schema` is implemented as a `StructuredOutput` tool and shows up in init.

    Verified against the real CLI on 2026-08-01. Asserting plain equality against the
    requested tools would have failed every single call in the run.
    """
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "structured_output_tool")
    runner, _ = make_runner(tmp_path)

    assert (await runner.run_role("ranking", "prompt", cfg())).ok
    assert (await runner.run_role("generation", "prompt", cfg("generation"))).ok


async def test_the_schema_tool_is_not_a_licence_to_appear_without_a_schema(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "structured_output_tool")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(json_schema=None))

    assert not result.ok
    assert "StructuredOutput" in result.error


# --- the ways a call fails ----------------------------------------------------------------


async def test_an_api_error_is_reported_with_the_fields_that_explain_it(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "is_error")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok and result.data is None
    assert "error_during_execution" in result.error
    assert "api_error_status=529" in result.error
    assert result.usage.tokens_out == 733, "the tokens were burned; the ledger must say so"


async def test_a_denied_grounded_call_fails_with_the_denial_verbatim(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """A denial is fixed by correcting --allowedTools, never by weakening permissions.

    Which is only possible if the denial survives the parser — in June 2026 it did not, and
    59K tokens a call went into failures nobody could see.
    """
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "denied")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("generation", "prompt", cfg("generation"))

    assert not result.ok
    assert "permission denied on a grounded call" in result.error
    assert "WebSearch" in result.error and "toolu_017" in result.error


async def test_a_denial_on_a_tool_less_role_is_surfaced_without_losing_the_answer(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """A judging role has no tools to be denied; if it happens the answer still stands."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "denied")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok
    assert summary_of(result)["permission_denials"][0]["tool_name"] == "WebSearch"


async def test_a_call_that_produces_no_result_event_reports_the_exit_and_stderr(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "no_result")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok
    assert "no result event (exit 3)" in result.error
    assert "connection reset by peer" in result.error


async def test_a_result_without_structured_output_is_refused_rather_than_reparsed(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """`result` is prose. Parsing it back is the habit this rebuild exists to break."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "no_structured")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok
    assert "no structured_output" in result.error


async def test_a_call_that_hangs_is_killed_and_reported_as_a_timeout(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "slow")
    runner, _ = make_runner(tmp_path)

    started = time.monotonic()
    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=2.0))
    elapsed = time.monotonic() - started

    assert not result.ok
    # Measured elapsed, then the ceiling it broke. The old string reported the configured
    # ceiling as though it were the duration, which is how the ledger came to hold
    # "timeout after 180s" beside a duration_ms of 257,293.
    assert re.match(r"timeout after \d+s \(limit 2s, killed\)$", result.error), result.error
    assert elapsed < 45, "the stub sleeps for 120s; the kill did not land"


async def test_an_answer_already_submitted_survives_the_kill(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The defect that cost run c4566ed2 its evolution: the deadline landed after the model
    had submitted its `StructuredOutput` payload and before the CLI wrote its result line,
    and a finished answer was reported as a transport failure. Both evolution attempts died
    that way, which is why the run has no lineage at all."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "answered_then_hangs")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=3.0))

    assert result.ok, result.error
    assert result.data["ok"] is True
    assert result.degraded, "an answer taken off a killed process is not an ordinary success"
    summary = summary_of(result)
    assert summary["salvaged_after_timeout"] in {"killed", "exited", "vanished"}
    assert summary["structured_from_transcript"] is True


async def test_the_grace_window_only_starts_when_the_answer_lands(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """A call with nothing submitted is still killed on the role's ceiling — the grace is
    for the CLI's wrap-up, not a blanket extension."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "spent_then_hangs")
    runner, _ = make_runner(tmp_path)

    started = time.monotonic()
    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=2.0))
    elapsed = time.monotonic() - started

    assert not result.ok
    assert elapsed < 45, "the ceiling was extended for a call that never answered"


async def test_a_killed_call_still_reports_what_it_spent(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """13 of 30 calls in c4566ed2 wrote tokens_in=0, tokens_out=0 and cost_usd NULL after
    burning 65 minutes of Opus time, so 43% of the run was invisible to the dollar ceiling
    that was meant to be governing it. The result envelope never arrives on that path; the
    per-turn usage does."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "spent_then_hangs")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=2.0))

    assert not result.ok
    assert result.usage.tokens_in == 7
    assert result.usage.tokens_out == 250
    assert result.usage.cache_read == 40000
    assert result.usage.tokens_total > 0
    assert summary_of(result)["usage_estimated"] is True


async def test_an_exited_child_ends_the_call_even_with_its_pipe_held_open(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """`_read_stdout` waits for EOF, and a grandchild holding the handle never delivers one.
    Two tool-less ranking calls in c4566ed2 waited 257s on a process that had already
    exited, against a configured ceiling of 180s."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "exit_with_open_pipe")
    runner, _ = make_runner(tmp_path)

    started = time.monotonic()
    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=60.0))
    elapsed = time.monotonic() - started

    assert result.ok, result.error
    assert elapsed < 20, f"the reader outlived the child by {elapsed:.1f}s"


async def test_a_call_close_to_its_ceiling_says_so(tmp_path: Path, record_file: Path):
    """Both of c4566ed2's surviving generation calls finished past 0.9x of their ceiling and
    nothing anywhere said so; the same calls started dying the next round."""
    runner, _ = make_runner(tmp_path)

    comfortable = await runner.run_role("ranking", "prompt", cfg(timeout_s=600.0))
    tight = await runner.run_role("ranking", "prompt", cfg(timeout_s=0.001))

    assert summary_of(comfortable)["near_timeout"] is False
    assert summary_of(tight)["near_timeout"] is True


# --- the substitution guard -----------------------------------------------------------
#
# There is no --fallback-model, so a call answered by a different model means the CLI
# substituted one silently — the failure mode that `--model claude-fable-5` exhibits live.
# Four cases, and the two that must *not* fire matter as much as the two that must.


async def test_the_pinned_model_answering_under_its_dated_id_is_not_a_substitution(
    tmp_path: Path, record_file: Path
):
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok and not result.degraded
    assert summary_of(result)["model_substituted"] is False
    assert summary_of(result)["model_ran"] is None


async def test_the_fable_alias_answering_as_its_family_id_is_not_a_substitution(
    tmp_path: Path, record_file: Path
):
    """The stable fable setting is pinned to 5.1 in argv and in the response guard."""
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(model="fable"))

    assert result.ok and not result.degraded


def test_fable_setting_launches_the_exact_5_1_model():
    argv = build_claude_argv(cfg(model="fable"), "test-session", exe="claude")
    assert argv[argv.index("--model") + 1] == "claude-fable-5-1"


async def test_the_housekeeping_model_alone_does_not_trip_the_guard(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The CLI bills its own bookkeeping turns to a small model and reports them in the
    same map. A map with nothing but housekeeping in it is no evidence either way, and
    failing a call on absent evidence would fail runs for a CLI release that renamed a key.
    """
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "housekeeping_only")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok and not result.degraded
    assert summary_of(result)["model_substituted"] is False


async def test_a_missing_model_usage_map_does_not_trip_the_guard(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "no_model_usage")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok and not result.degraded


async def test_a_sideways_swap_degrades_the_call_but_keeps_the_answer(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """Fable answered by Opus 5 — still above the floor, so the work is worth keeping. Both
    names go into telemetry so the Activity tab can say "requested X, ran Y"."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "substituted")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(model="fable"))
    summary = summary_of(result)

    assert result.ok, "a sideways swap is reported, not fatal"
    assert result.degraded
    assert summary["model_requested"] == "fable"
    assert summary["model_ran"] == "claude-opus-5-20260601"
    assert summary["model_substituted"] is True
    assert summary["model_below_floor"] is False


async def test_a_model_newer_than_the_rank_table_degrades_instead_of_failing_the_call(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The live time-bomb: the day the CLI reports an Anthropic id this engine's table has
    never seen, every role call in the run goes through this path. Unrankable is not a
    downgrade — there is no rank to compare — and it is not another provider either, so the
    answer is kept, flagged, and named in telemetry rather than thrown away."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "unrecognised_model")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())
    summary = summary_of(result)

    assert result.ok, "a model newer than the table must not fail the run"
    assert result.error is None
    assert result.degraded, "kept, but the run is flagged and both names are recorded"
    assert summary["model_ran"] == "claude-opus-6-20270301"
    assert summary["model_substituted"] is True
    assert summary["model_cross_provider"] is False
    assert summary["model_below_floor"] is False
    assert summary["model_below_request"] is False


async def test_a_downgrade_below_the_opus_floor_fails_the_call(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """A weaker judge answering is worse than no answer: its verdict would enter the same
    Elo table as the rest with nothing in the artefacts to say it came from elsewhere."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "downgraded")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())
    summary = summary_of(result)

    assert not result.ok
    assert "below the Opus floor" in result.error
    assert "requested claude-opus-5, ran claude-sonnet-5-20260601" in result.error
    assert result.degraded
    assert summary["model_below_floor"] is True
    assert summary["model_ran"] == "claude-sonnet-5-20260601"


# --- backpressure -------------------------------------------------------------------------


async def test_a_rate_limit_event_mid_stream_raises_backpressure_without_losing_the_answer(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "rate_limit")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok, "the call completed; only the next one has to wait"
    assert result.rate_limited


async def test_a_rate_limit_event_that_says_it_is_not_biting_is_not_backpressure(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "rate_limit_allowed")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok and not result.rate_limited


# --- tolerating the stream ----------------------------------------------------------------


async def test_unknown_envelopes_and_unparsable_lines_do_not_fail_a_call(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """A future envelope type must never be able to fail a run."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "garbage")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok
    assert summary_of(result)["unparsable_lines"] >= 2


async def test_non_ascii_and_invalid_bytes_survive_as_replacements(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """cp1252 pipes are how five of the twelve archived runs got mojibake in their titles."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "unicode")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok
    assert result.data["text"] == "π — café — 日本語 — ✓"


async def test_an_envelope_larger_than_the_read_buffer_is_reassembled(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """Debates and schema echoes run well past the 64KB chunk the reader pulls at a time."""
    monkeypatch.setenv("STUB_CLAUDE_SCENARIO", "big")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok


# --- the cap that survives an orchestrator bug --------------------------------------------


async def test_a_refused_budget_stops_the_call_before_a_process_exists(
    tmp_path: Path, record_file: Path
):
    def guard() -> None:
        raise Refused("budget")

    runner, _ = make_runner(tmp_path, budget_guard=guard)

    with pytest.raises(Refused):
        await runner.run_role("ranking", "prompt", cfg())

    assert not record_file.exists(), "the CLI ran past an exhausted budget"


async def test_a_role_that_disagrees_with_its_config_is_a_programming_error(tmp_path: Path):
    runner, _ = make_runner(tmp_path)

    with pytest.raises(ValueError, match="does not match its config"):
        await runner.run_role("generation", "prompt", cfg("ranking"))


# --- probe --------------------------------------------------------------------------------


async def test_probe_only_asks_for_a_version(tmp_path: Path):
    runner = ClaudeCliRunner(workdir=tmp_path, budget_guard=lambda: None, exe=PYTHON)

    result = await runner.probe()

    assert result["name"] == "claude"
    assert result["installed"] is True
    assert re.fullmatch(r"\d+\.\d+\.\d+", result["version"])
