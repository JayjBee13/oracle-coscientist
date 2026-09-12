"""`CodexCliRunner` against a stub CLI: the argv law, and every way a Codex call can end.

No test here runs the real `codex`. The stub replays envelopes recorded from it on
2026-08-11, which is enough to pin the two things that matter — that we send a command line
nobody has to reason about twice, and that we believe an answer only when the CLI has said
the turn completed and written the file the answer lives in.

The Claude suite is the model for this one, deliberately: the two runners must fail the same
ways, and a scenario one of them has and the other does not is usually a gap rather than a
difference between the CLIs.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from app.engine.codex_runner import (
    FORBIDDEN_FLAGS,
    CodexCliRunner,
    _assert_argv_law,
    build_codex_argv,
    build_codex_stdin,
    codex_output_schema,
)
from app.engine.models import ModelPolicyError
from app.engine.runners import ROLES, AgentRunner, RoleConfig, role_config
from app.engine.schemas import PROXIMITY_SCHEMA, RANKING_SCHEMA
from app.engine.spawn import spawn_role_process

STUB = Path(__file__).resolve().parents[3] / "tests" / "support" / "stub_codex.py"
PYTHON = str(Path(sys.executable).resolve())

HEAVY = "gpt-5.6-sol"
LIGHT = "gpt-5.6-luna"


class Refused(RuntimeError):
    """Stands in for BudgetExhausted."""


def cfg(role: str = "ranking", **overrides) -> RoleConfig:
    base = role_config(
        role,
        model=HEAVY,
        effort="medium",
        system_prompt="SYSTEM INSTRUCTIONS.",
        round=2,
        unit="h001",
        json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )
    return replace(base, **overrides) if overrides else base


def make_runner(tmp_path: Path, *, budget_guard=lambda: None) -> tuple[CodexCliRunner, list]:
    """A runner whose spawn puts the stub where `codex.exe` would be.

    The argv the runner built is passed through untouched and recorded, so the argv law is
    asserted against exactly what would have reached the real CLI.
    """
    seen: list[list[str]] = []
    exe = tmp_path / "codex.exe"
    exe.write_bytes(b"MZ")

    async def spawn(argv, **kwargs):
        seen.append(list(argv))
        return await spawn_role_process(
            [PYTHON, "-X", "utf8", str(STUB), *argv], allowed_binaries=[PYTHON], **kwargs
        )

    runner = CodexCliRunner(
        workdir=tmp_path, budget_guard=budget_guard, exe=exe, spawn=spawn
    )
    return runner, seen


@pytest.fixture
def record_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "stub-record.jsonl"
    monkeypatch.setenv("STUB_CODEX_RECORD", str(path))
    monkeypatch.delenv("STUB_CODEX_SCENARIO", raising=False)
    return path


def records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def summary_of(result) -> dict:
    return json.loads(result.raw_tail.splitlines()[0])


def value(argv: list[str], flag: str) -> str | None:
    for index, item in enumerate(argv):
        if item == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def config_of(argv: list[str]) -> dict[str, str]:
    found = {}
    for index, item in enumerate(argv):
        if item == "-c" and index + 1 < len(argv):
            key, _, val = argv[index + 1].partition("=")
            found[key] = val
    return found


# --- the argv law -------------------------------------------------------------------------


def test_astra_argv_preserves_the_exact_model_and_effort(tmp_path: Path):
    argv = build_codex_argv(cfg(model="gpt-6-astra", effort="max"),
                            exe="C:/codex.exe", scratch=tmp_path)
    assert value(argv, "-m") == "gpt-6-astra"
    assert config_of(argv)["model_reasoning_effort"] == "max"


def test_a_grounded_call_asks_for_search_through_the_config_override(tmp_path: Path):
    """`codex exec` has no `--search`; that flag exists only on the interactive command."""
    argv = build_codex_argv(cfg("generation", effort="high"), exe="C:/codex.exe", scratch=tmp_path)

    assert config_of(argv)["tools.web_search"] == "true"
    assert "--search" not in argv


def test_a_judging_call_states_that_it_is_not_searching_rather_than_omitting_it(tmp_path: Path):
    """Availability is intent. An absent toggle and a false one look identical afterwards."""
    argv = build_codex_argv(cfg("ranking"), exe="C:/codex.exe", scratch=tmp_path)

    assert config_of(argv)["tools.web_search"] == "false"


def test_effort_travels_as_a_config_override_because_there_is_no_effort_flag(tmp_path: Path):
    argv = build_codex_argv(cfg("ranking", effort="xhigh"), exe="C:/codex.exe", scratch=tmp_path)

    assert config_of(argv)["model_reasoning_effort"] == "xhigh"
    assert "--effort" not in argv


def test_the_sandbox_and_the_operators_own_config_are_pinned_on_every_call(tmp_path: Path):
    """`~/.codex/config.toml` sets danger-full-access and a notify hook globally. Without
    these four flags every role call would inherit unsandboxed shell access and fire an
    executable on every turn."""
    argv = build_codex_argv(cfg("generation"), exe="C:/codex.exe", scratch=tmp_path)

    assert value(argv, "-s") == "read-only"
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "--ephemeral" in argv


def test_approval_travels_as_a_config_override_because_exec_has_no_approval_flag(
    tmp_path: Path,
):
    """`-a`/`--ask-for-approval` belongs to the interactive command. Against codex-cli
    0.146.0 `codex exec -a never` is `error: unexpected argument '-a' found`, exit 2, before
    a single token is spent — which killed the whole OpenAI lane in 0.04s."""
    argv = build_codex_argv(cfg("generation"), exe="C:/codex.exe", scratch=tmp_path)

    assert config_of(argv)["approval_policy"] == '"never"'
    assert "-a" not in argv and "--ask-for-approval" not in argv


def test_the_law_refuses_an_argv_that_could_be_held_open_by_an_approval_prompt():
    """A role call has no tty. Inheriting an interactive approval policy means waiting on a
    prompt nobody can answer until the ceiling — the same class of silent inheritance the
    sandbox and web-search checks exist for."""
    with pytest.raises(ValueError, match="approval_policy"):
        _assert_argv_law(
            ["codex.exe", "exec", "-s", "read-only", "-c", "model_reasoning_effort=low",
             "-c", "tools.web_search=false", "--ignore-user-config", "--ephemeral",
             "--skip-git-repo-check", "-"]
        )


def test_the_law_refuses_an_approval_policy_that_is_not_never():
    with pytest.raises(ValueError, match="approval_policy"):
        _assert_argv_law(
            ["codex.exe", "exec", "-s", "read-only", "-c", "model_reasoning_effort=low",
             "-c", "tools.web_search=false", "-c", 'approval_policy="on-request"',
             "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "-"]
        )


@pytest.mark.parametrize("role", ROLES)
def test_no_role_can_produce_an_argv_that_breaks_a_rule_the_probe_paid_to_learn(
    role: str, tmp_path: Path
):
    prompt = "the task prompt, which must never appear on a command line"
    argv = build_codex_argv(cfg(role), exe="C:/codex.exe", scratch=tmp_path)

    assert argv[-1] == "-", "the prompt is read from stdin, never argv"
    assert not FORBIDDEN_FLAGS & set(argv)
    assert not any(token in item for item in argv for token in ("danger-full-access", "://"))
    assert prompt not in argv


def test_the_law_refuses_an_argv_that_would_inherit_the_operators_sandbox(tmp_path: Path):
    with pytest.raises(ValueError, match="-s read-only"):
        _assert_argv_law(
            ["codex.exe", "exec", "-c", "model_reasoning_effort=low",
             "-c", "tools.web_search=false", "--ignore-user-config", "--ephemeral",
             "--skip-git-repo-check", "-"]
        )


def test_the_law_refuses_an_argv_that_would_leave_the_prompt_ambiguous():
    """A prompt argument and piped stdin are concatenated, so `-` is what makes it exact."""
    with pytest.raises(ValueError, match="must end with `-`"):
        _assert_argv_law(
            ["codex.exe", "exec", "-s", "read-only", "-c", "model_reasoning_effort=low",
             "-c", "tools.web_search=false", "--ignore-user-config", "--ephemeral",
             "--skip-git-repo-check", "the prompt"]
        )


def test_the_law_refuses_an_argv_that_would_write_outside_the_scratch_directory():
    with pytest.raises(ValueError, match="--ephemeral"):
        _assert_argv_law(
            ["codex.exe", "exec", "-s", "read-only", "-c", "model_reasoning_effort=low",
             "-c", "tools.web_search=false", "--ignore-user-config",
             "--skip-git-repo-check", "-"]
        )


@pytest.mark.parametrize("flag", sorted(FORBIDDEN_FLAGS))
def test_the_law_refuses_each_flag_that_would_undo_the_sandbox(flag: str):
    with pytest.raises(ValueError, match="forbidden flag"):
        _assert_argv_law(
            ["codex.exe", "exec", "-s", "read-only", "-c", "model_reasoning_effort=low",
             "-c", "tools.web_search=false", "--ignore-user-config", "--ephemeral",
             "--skip-git-repo-check", flag, "-"]
        )


def test_the_runner_satisfies_the_protocol_the_orchestrator_depends_on(tmp_path: Path):
    runner, _ = make_runner(tmp_path)

    assert isinstance(runner, AgentRunner)
    assert runner.name == "codex"


# --- the system prompt has nowhere else to go ---------------------------------------------


def test_the_system_prompt_travels_on_stdin_because_exec_has_no_flag_for_it():
    """The Claude runner replaces the CLI persona with `--system-prompt`. `codex exec` has no
    equivalent, so dropping it would send a role prompt to a coding agent with no idea what
    it is for — and the failure would look like a bad model rather than a missing
    instruction."""
    text = build_codex_stdin(cfg("ranking"), "Which wins?", schema_in_prompt=False)

    assert text.startswith("SYSTEM INSTRUCTIONS.")
    assert "ROLE: ranking." in text
    assert text.endswith("Which wins?")


async def test_the_prompt_and_the_role_reach_the_model_over_stdin_not_argv(
    tmp_path: Path, record_file: Path
):
    runner, seen = make_runner(tmp_path)
    prompt = "Compare h001 and h002 on novelty."

    await runner.run_role("ranking", prompt, cfg())

    entry = records(record_file)[-1]
    assert "SYSTEM INSTRUCTIONS." in entry["stdin"]
    assert prompt in entry["stdin"]
    assert not any(prompt in item for item in seen[0])


# --- the schema ---------------------------------------------------------------------------


def test_a_role_schema_is_rewritten_for_strict_structured_output():
    """Strict mode wants every property required, no extra properties, and none of the
    validation keywords. The engine re-validates the payload itself either way."""
    strict = codex_output_schema(RANKING_SCHEMA)

    assert strict["additionalProperties"] is False
    assert sorted(strict["required"]) == ["debate", "winner"]


def test_a_schema_with_arbitrary_keys_cannot_be_strict_and_says_so_rather_than_mangling_it():
    """Clustering answers with hypothesis-id → label, whose keys are unknowable when the
    schema is written. Strict mode has no way to express that, so the flag is dropped and
    the contract is stated in the prompt instead — quietly rewriting it would cost every
    clustering call in every Codex run."""
    assert codex_output_schema(PROXIMITY_SCHEMA) is None


async def test_the_schema_is_written_to_the_scratch_dir_and_never_inlined_into_argv(
    tmp_path: Path, record_file: Path
):
    runner, seen = make_runner(tmp_path)

    await runner.run_role("ranking", "prompt", cfg(json_schema=RANKING_SCHEMA))

    entry = records(record_file)[-1]
    assert entry["schema"]["additionalProperties"] is False
    assert not any("debate" in item for item in seen[0]), "the schema went on the command line"


async def test_the_role_that_cannot_be_strict_states_its_contract_in_the_prompt_instead(
    tmp_path: Path, record_file: Path
):
    runner, seen = make_runner(tmp_path)

    result = await runner.run_role(
        "proximity", "h001 | a | b", cfg("proximity", json_schema=PROXIMITY_SCHEMA)
    )

    assert "--output-schema" not in seen[0]
    assert "conforming to this JSON Schema" in records(record_file)[-1]["stdin"]
    assert summary_of(result)["schema_mode"] == "prompt"


async def test_a_fenced_answer_is_still_an_answer(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """Bare JSON is asked for and a fence is the one deviation models reliably make anyway."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "fenced")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok, result.error
    assert result.data["ok"] is True


# --- the happy path -----------------------------------------------------------------------


async def test_a_successful_call_returns_the_answer_from_the_last_message_file(
    tmp_path: Path, record_file: Path
):
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "Which wins, h001 or h002?", cfg())

    assert result.ok and result.error is None
    assert result.data["ok"] is True
    assert result.data["model"] == HEAVY
    assert not result.degraded and not result.rate_limited


async def test_codex_usage_counters_are_mapped_by_name_not_by_position(
    tmp_path: Path, record_file: Path
):
    """Codex's field names are not Claude's: `cached_input_tokens` is the cache *read* and
    `cache_write_input_tokens` is the creation. Swapping them would under-report a warm
    prompt by two orders of magnitude in one direction or the other."""
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert (result.usage.tokens_in, result.usage.tokens_out) == (16891, 733)
    assert result.usage.cache_read == 12004
    assert result.usage.cache_creation == 402
    assert result.usage.cost_usd is None, "codex reports no cost; a number here would be a guess"
    assert summary_of(result)["usage_reported"] is True


async def test_each_call_runs_in_its_own_empty_scratch_directory_inside_the_workdir(
    tmp_path: Path, record_file: Path
):
    runner, _ = make_runner(tmp_path)

    await runner.run_role("ranking", "prompt", cfg())

    cwd = Path(records(record_file)[-1]["cwd"]).resolve()
    assert cwd.parent == (tmp_path / "calls").resolve()
    assert list((tmp_path / "calls").iterdir()) == [], "the scratch directory was left behind"


async def test_searches_are_counted_from_web_search_items_never_inferred_from_prose(
    tmp_path: Path, record_file: Path
):
    """The probe saw an `item.started` and an `item.completed` for one search, and a later
    item carrying `action.queries` as an array of parallel queries. Counting envelopes rather
    than items would report three searches for two."""
    runner, _ = make_runner(tmp_path)

    grounded = await runner.run_role("generation", "prompt", cfg("generation"))
    judging = await runner.run_role("ranking", "prompt", cfg("ranking"))

    assert summary_of(grounded)["web_searches"] == 2
    assert summary_of(grounded)["web_search_queries"] == ["first check", "second check", "third"]
    assert summary_of(judging)["web_searches"] == 0


async def test_stderr_on_a_successful_call_is_not_a_failure(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """Live probe call 1 exited 0 while logging `ERROR codex_models_manager::cache: failed to
    load models cache` — benign version skew that self-healed on the next call. Keying
    success off an empty stderr would have failed a healthy call."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "benign_stderr")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok, result.error
    assert "models cache" in summary_of(result)["stderr"]


# --- the effort ladder --------------------------------------------------------------------


async def test_an_effort_the_model_does_not_have_is_clamped_and_reported(
    tmp_path: Path, record_file: Path
):
    """The clamp is driven by the model's own published ladder, and it is never silent: a
    call that ran at a different effort than the tier states is `degraded` and carries
    `effort_clamped` in telemetry."""
    runner, seen = make_runner(tmp_path, budget_guard=lambda: None)

    result = await runner.run_role("ranking", "prompt", cfg(model=LIGHT, effort="max"))

    summary = summary_of(result)
    assert config_of(seen[0])["model_reasoning_effort"] in ("max", "high")
    assert summary["effort_requested"] == "max"
    assert summary["effort"] == config_of(seen[0])["model_reasoning_effort"]
    assert summary["effort_clamped"] is (summary["effort"] != "max")


async def test_xhigh_is_sent_verbatim_because_codex_accepts_it(
    tmp_path: Path, record_file: Path
):
    """The plan said Codex effort tops out at `high`. The probe ran `xhigh` on Luna live and
    it exited 0, so clamping it would silently downgrade every heavy role at the max tier —
    which is the substitution this engine exists to refuse."""
    runner, seen = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(effort="xhigh"))

    assert config_of(seen[0])["model_reasoning_effort"] == "xhigh"
    assert summary_of(result)["effort_clamped"] is False
    assert not result.degraded


# --- the ways a call fails ----------------------------------------------------------------


async def test_an_unknown_model_fails_loudly_and_is_never_answered_from_a_stale_file(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """Verified live: exit 1, `turn.failed`, and **no last-message file at all**."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "unknown_model")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok and result.data is None
    assert "fallback model metadata" in result.error


async def test_a_model_off_the_allowlist_never_reaches_a_process(
    tmp_path: Path, record_file: Path
):
    """The closed allowlist is the whole substitution guarantee on this provider, because
    Codex's stream names no model anywhere. Checking it after the spawn would mean paying
    for the call that proves it."""
    runner, _ = make_runner(tmp_path)

    with pytest.raises(ModelPolicyError):
        await runner.run_role("ranking", "prompt", cfg(model="gpt-5.6-terra"))

    assert not record_file.exists(), "codex ran with a model this engine does not offer"


async def test_fallback_metadata_refuses_the_call_even_when_the_turn_succeeds(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The decoy stage of the unknown-model trap. It is client-side metadata only — it does
    not substitute a model — but the turn then ran with another model's context window and
    tool shapes, which is not the call that was configured."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "fallback_metadata_then_ok")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok
    assert "fallback model metadata" in result.error


async def test_an_invalid_effort_is_reported_with_the_vocabulary_the_server_named(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "bad_effort")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok
    assert "invalid_enum_value" in result.error
    assert summary_of(result)["api_error_status"] == 400


async def test_a_turn_that_never_completed_reports_the_exit_and_the_stderr(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "no_turn_completed")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok
    assert "no turn.completed event (exit 3)" in result.error
    assert "connection reset by peer" in result.error


async def test_a_missing_answer_file_is_a_hard_failure_never_an_empty_result(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The failure mode the probe found by accident: on a failed turn the `-o` file is not
    created at all. An absent file read as `{}` would enter the pipeline as a real answer."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "no_answer_file")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok and result.data is None
    assert "no answer file was written" in result.error


async def test_a_call_that_hangs_is_killed_and_reported_as_a_timeout(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "slow")
    runner, _ = make_runner(tmp_path)

    started = time.monotonic()
    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=2.0))
    elapsed = time.monotonic() - started

    assert not result.ok
    assert "timeout after" in result.error and "killed" in result.error
    assert elapsed < 45, "the stub sleeps for 120s; the kill did not land"


async def test_an_answer_already_written_survives_the_kill(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The defect that cost run c4566ed2 its evolution, ported to the other CLI before it
    could happen twice: the deadline landing between the answer and the CLI's wrap-up."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "answered_then_hangs")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=3.0))

    assert result.ok, result.error
    assert result.data["ok"] is True
    assert result.degraded, "an answer taken off a killed process is not an ordinary success"
    assert summary_of(result)["salvaged_after_timeout"] in {"killed", "exited", "vanished"}


async def test_a_salvaged_answer_that_carried_a_refusal_is_still_a_refusal(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The kill excuses the missing `turn.completed` and nothing else.

    `fallback metadata` is a refusal *even on a turn that goes on to succeed* — the client
    ran with another model's context window and tool shapes. Returning the salvaged answer
    without asking made the timeout path the one route by which a refused call entered the
    pipeline as a good one, complete with an `ok: true` payload."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "fallback_metadata_then_killed")
    # The grace window itself is pinned by `test_an_answer_already_written_survives_the_kill`
    # at its full 90s. What is under test here is how the salvaged call is *judged*, so the
    # window is shortened rather than waited out.
    monkeypatch.setattr("app.engine.codex_runner.ANSWER_GRACE_S", 0.25)
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=3.0))

    assert not result.ok and result.data is None
    assert "fallback model metadata" in result.error
    assert summary_of(result)["salvaged_after_timeout"] in {"killed", "exited", "vanished"}


async def test_an_empty_object_is_never_an_answer(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """No role schema admits `{}` — every one of them requires at least one property — so an
    empty object is the CLI saying the shape was not honoured, not a result. It used to pass
    the `data is None` check and enter the pipeline as a hypothesis-shaped hole."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "empty_object")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok and result.data is None
    assert "the schema was not honoured" in result.error


async def test_an_empty_answer_file_does_not_shadow_the_answer_in_the_transcript(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The `-o` file wins when it holds an answer. `{}` is not one, and reading it as if it
    were threw away a complete payload sitting in the same call's transcript."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "empty_object_file")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok, result.error
    assert result.data["ok"] is True
    assert summary_of(result)["structured_from_transcript"] is True


async def test_an_empty_object_recovered_from_a_killed_call_is_diagnosed_not_written_off(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """`{}` in the answer file is falsy, so `or` used to make the salvage give up on it and
    report the call as a plain transport timeout. It is not one: the CLI wrote a file, and
    what it wrote is the diagnosis — the schema was not honoured, on a call whose whole
    ceiling was spent."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "empty_object_then_hangs")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=3.0))

    assert not result.ok and result.data is None
    assert "the schema was not honoured" in result.error
    assert summary_of(result)["salvaged_after_timeout"] in {"killed", "exited", "vanished"}


async def test_a_salvaged_call_reads_past_an_empty_answer_file_to_the_transcript(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """The salvage decision and the answer it returns have to be the same reading.

    `_salvage` used to recover the transcript's payload (because `{}` is falsy) while
    `_result` went on to read the same `{}` off disk and return *that* — a call reported as a
    successful salvage whose `data` was an empty dict."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "empty_object_file_then_hangs")
    monkeypatch.setattr("app.engine.codex_runner.ANSWER_GRACE_S", 0.25)
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=3.0))

    assert result.ok, result.error
    assert result.data["ok"] is True
    assert result.degraded
    assert summary_of(result)["salvaged_after_timeout"] in {"killed", "exited", "vanished"}


async def test_the_grace_window_only_starts_when_the_answer_lands(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "spent_then_hangs")
    runner, _ = make_runner(tmp_path)

    started = time.monotonic()
    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=2.0))
    elapsed = time.monotonic() - started

    assert not result.ok
    assert elapsed < 45, "the ceiling was extended for a call that never answered"


async def test_a_failed_turn_reports_absent_usage_as_absent_rather_than_as_zero(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failed Codex turn carries no usage block at all. The counters read 0 because the
    ledger sums ints — `usage_reported: False` is what says that 0 is an absence, and
    nothing may read those counters without it."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "unknown_model")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert summary_of(result)["usage_reported"] is False
    assert result.usage.cost_usd is None


async def test_a_completed_turn_without_a_usage_block_is_also_absent_not_zero(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "no_usage")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok, result.error
    assert summary_of(result)["usage_reported"] is False


async def test_nothing_claims_to_have_verified_which_model_answered(
    tmp_path: Path, record_file: Path
):
    """Codex's JSONL names no model anywhere — there is no `modelUsage` equivalent — so the
    substitution guard covers zero Codex calls. That gap is stated in telemetry rather than
    left to a docstring, because a reader comparing a Codex call to a Claude one would
    otherwise assume the same check ran on both."""
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    summary = summary_of(result)
    assert summary["model_verified"] is False
    assert summary["model_substituted"] is False
    assert summary["provider"] == "openai"


async def test_an_exited_child_ends_the_call_even_with_its_pipe_held_open(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """Codex spawns helper executables for shell and search tools, any of which can inherit
    stdout — in which case the pipe never reaches EOF and a reader waits out the whole
    ceiling on a process that already exited."""
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "exit_with_open_pipe")
    runner, _ = make_runner(tmp_path)

    started = time.monotonic()
    result = await runner.run_role("ranking", "prompt", cfg(timeout_s=60.0))
    elapsed = time.monotonic() - started

    assert result.ok, result.error
    assert elapsed < 20, f"the reader outlived the child by {elapsed:.1f}s"


async def test_backpressure_is_recognised_without_losing_the_diagnosis(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "rate_limited")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert not result.ok
    assert result.rate_limited


async def test_unknown_envelopes_and_unparsable_lines_do_not_fail_a_call(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "garbage")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok, result.error
    assert summary_of(result)["unparsable_lines"] >= 2


async def test_non_ascii_and_invalid_bytes_survive_as_replacements(
    tmp_path: Path, record_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("STUB_CODEX_SCENARIO", "unicode")
    runner, _ = make_runner(tmp_path)

    result = await runner.run_role("ranking", "prompt", cfg())

    assert result.ok, result.error
    assert result.data["text"] == "π — café — 日本語 — ✓"


# --- the cap that survives an orchestrator bug --------------------------------------------


async def test_a_refused_budget_stops_the_call_before_a_process_exists(
    tmp_path: Path, record_file: Path
):
    def guard() -> None:
        raise Refused("budget")

    runner, _ = make_runner(tmp_path, budget_guard=guard)

    with pytest.raises(Refused):
        await runner.run_role("ranking", "prompt", cfg())

    assert not record_file.exists(), "codex ran past an exhausted budget"


async def test_a_role_that_disagrees_with_its_config_is_a_programming_error(tmp_path: Path):
    runner, _ = make_runner(tmp_path)

    with pytest.raises(ValueError, match="does not match its config"):
        await runner.run_role("generation", "prompt", cfg("ranking"))


async def test_the_argv_of_every_call_is_written_to_the_run_ledger(
    tmp_path: Path, record_file: Path
):
    runner, seen = make_runner(tmp_path)

    await runner.run_role("ranking", "prompt", cfg())

    logged = [
        json.loads(line)
        for line in (tmp_path / "spawn-argv.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ][-1]
    assert logged["argv"][-len(seen[0]) :] == seen[0]
    assert not any("://" in item for item in logged["argv"])


# --- probe --------------------------------------------------------------------------------


async def test_probe_only_asks_for_a_version(tmp_path: Path):
    runner = CodexCliRunner(workdir=tmp_path, budget_guard=lambda: None, exe=PYTHON)

    result = await runner.probe()

    assert result["name"] == "codex"
