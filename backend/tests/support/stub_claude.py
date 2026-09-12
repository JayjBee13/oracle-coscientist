"""A fake `claude` for tests: replays recorded stream-json, on demand.

Every envelope shape here was observed from the real CLI on 2026-08-01 (plan C1's live
verification) — the field names, the four token classes, `structured_output` beside the
prose `result`, the `system/init` report of tools and permission mode. Tests select a
scenario with `$STUB_CLAUDE_SCENARIO` and read back what the runner sent from
`$STUB_CLAUDE_RECORD`.

This is a script, not a module: it is executed as `python stub_claude.py <claude argv...>`,
so it must not import anything from the application.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

# The CLI runs its own housekeeping turns on a small model and reports them in `modelUsage`
# beside the role model. It is not a substitution and must not be read as one.
MODEL_HOUSEKEEPING = "claude-haiku-4-5-20251001"

# What a real downgrade looks like: a model below the Opus floor answered the call.
MODEL_DOWNGRADE = "claude-sonnet-5-20260601"

# The day after the next Anthropic release: an id the engine's rank table has never seen,
# still plainly Anthropic's. Unrankable is not a downgrade and not another provider.
MODEL_UNRECOGNISED = "claude-opus-6-20270301"


def emit(envelope: dict) -> None:
    write(json.dumps(envelope, ensure_ascii=False).encode("utf-8") + b"\n")


def write(raw: bytes) -> None:
    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def flag(argv: list[str], name: str, default: str = "") -> str:
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
    return default


def tools_of(argv: list[str]) -> list[str]:
    raw = flag(argv, "--tools")
    return [part.strip() for part in raw.split(",") if part.strip()]


def init_envelope(argv: list[str], **overrides: object) -> dict:
    envelope = {
        "type": "system",
        "subtype": "init",
        "cwd": os.getcwd(),
        "session_id": flag(argv, "--session-id"),
        "tools": tools_of(argv),
        "mcp_servers": [],
        "model": flag(argv, "--model"),
        "permissionMode": "default",
        "slash_commands": [],
        "apiKeySource": "none",
        "output_style": "default",
    }
    envelope.update(overrides)
    return envelope


def reported_model(argv: list[str]) -> str:
    """What `modelUsage` names for the model we asked for.

    `fable` is an alias — the CLI reports the resolved family id, never the alias, which is
    exactly why the substitution guard has to canonicalise before comparing.
    """
    model = flag(argv, "--model") or "claude-opus-5"
    return "claude-fable-5" if model == "fable" else model


def result_envelope(argv: list[str], structured: object, **overrides: object) -> dict:
    model = reported_model(argv)
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 3812,
        "duration_api_ms": 3604,
        "num_turns": 1,
        "result": json.dumps(structured, ensure_ascii=False),
        "structured_output": structured,
        "session_id": flag(argv, "--session-id"),
        "total_cost_usd": 0.0158,
        "usage": {
            "input_tokens": 12,
            "cache_creation_input_tokens": 4102,
            "cache_read_input_tokens": 58610,
            "output_tokens": 733,
            "server_tool_use": {"web_search_requests": 2},
        },
        # Dated suffix and a housekeeping entry beside the role model — both are what the
        # real CLI reports, and both have to be tolerated by the substitution check.
        "modelUsage": {
            f"{model}-20260601": {"inputTokens": 12, "outputTokens": 733},
            MODEL_HOUSEKEEPING: {"inputTokens": 40, "outputTokens": 8},
        },
        "permission_denials": [],
        "stop_reason": "end_turn",
        "terminal_reason": "success",
    }
    envelope.update(overrides)
    return envelope


def record(argv: list[str], stdin_text: str) -> None:
    path = os.environ.get("STUB_CLAUDE_RECORD")
    if not path:
        return
    entry = {
        "argv": argv,
        "stdin": stdin_text,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "pid": os.getpid(),
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def spawn_grandchild() -> int:
    """A descendant that only `taskkill /T` reaches — proof the whole tree died."""
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(45)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return child.pid


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        write(b"2.1.220 (Claude Code)\n")
        return 0

    stdin_text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    record(argv, stdin_text)

    scenario = os.environ.get("STUB_CLAUDE_SCENARIO", "ok")
    head = stdin_text.splitlines()[0] if stdin_text.splitlines() else ""
    payload = {"ok": True, "stdin_head": head, "argv_len": len(argv)}

    if scenario == "no_init":
        emit(result_envelope(argv, payload))
        return 0

    if scenario == "bad_init_mcp":
        emit(init_envelope(argv, mcp_servers=[{"name": "perplexity", "status": "connected"}]))
    elif scenario == "bad_init_tools":
        emit(init_envelope(argv, tools=[*tools_of(argv), "Bash"]))
    elif scenario == "bad_init_missing":
        emit(init_envelope(argv, tools=[]))
    elif scenario == "structured_output_tool":
        # What the real CLI reports whenever --json-schema is passed (verified 2026-08-01).
        emit(init_envelope(argv, tools=[*tools_of(argv), "StructuredOutput"]))
    elif scenario == "bad_init_bypass":
        emit(init_envelope(argv, permissionMode="bypassPermissions"))
    elif scenario == "bad_init_slash":
        emit(init_envelope(argv, slash_commands=["/compact", "/cost"]))
    else:
        emit(init_envelope(argv))

    if scenario.startswith("bad_init"):
        # The real CLI would keep going and keep billing; the runner is expected to have
        # stopped reading and killed us before this lands.
        time.sleep(30)
        emit(result_envelope(argv, payload))
        return 0

    if scenario == "slow":
        time.sleep(120)
        return 0

    if scenario == "answered_then_hangs":
        # What run c4566ed2 was killed in the middle of: the model has submitted its
        # structured answer and the CLI has not yet written its result line. Three of that
        # run's ten timeouts died exactly here, with the whole payload already on the wire.
        emit(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input_tokens": 9,
                        "output_tokens": 611,
                        "cache_creation_input_tokens": 4102,
                        "cache_read_input_tokens": 58610,
                    },
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_out",
                            "name": "StructuredOutput",
                            "input": payload,
                        }
                    ],
                },
            }
        )
        time.sleep(120)
        return 0

    if scenario == "spent_then_hangs":
        # Tokens burned, no answer, no result line — the shape that used to be recorded as
        # zero tokens and a NULL cost.
        emit(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "usage": {
                        "input_tokens": 7,
                        "output_tokens": 250,
                        "cache_creation_input_tokens": 1000,
                        "cache_read_input_tokens": 40000,
                    },
                    "content": "still thinking",
                },
            }
        )
        time.sleep(120)
        return 0

    if scenario == "exit_with_open_pipe":
        # A grandchild inherits stdout and outlives us, so the pipe never reaches EOF. The
        # reader must notice the child exited instead of waiting out the whole ceiling —
        # two ranking calls in c4566ed2 sat here for 257s against a 180s limit.
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(20)"],
            stdin=subprocess.DEVNULL,
        )
        emit(result_envelope(argv, payload))
        return 0

    if scenario == "slow_tree":
        emit({"type": "stub", "grandchild_pid": spawn_grandchild()})
        time.sleep(120)
        return 0

    if scenario == "no_result":
        sys.stderr.buffer.write(b"claude: fatal: connection reset by peer\n")
        sys.stderr.buffer.flush()
        return 3

    emit({"type": "assistant", "message": {"role": "assistant", "content": "thinking"}})

    # A grounded call searches, and the transcript is the only place that shows it: the
    # real CLI runs WebSearch locally, so the result envelope's server-side counter stays
    # at zero however many searches happened (verified 2026-08-02).
    if "WebSearch" in tools_of(argv):
        for index, query in enumerate(("first check", "second check")):
            emit({"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": f"toolu_{index}", "name": "WebSearch",
                 "input": {"query": query}}]}})
            emit({"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": f"toolu_{index}", "content": "results"}]}})

    if scenario == "garbage":
        write(b"not json at all\n")
        write(b"\xff\xfe \x00truncated binary\n")
        emit({"type": "user", "message": {"role": "user", "content": "tool result"}})
        emit({"type": "system", "subtype": "thinking_tokens", "tokens": 512})
        emit({"type": "some_future_envelope", "payload": {"unknown": True}})

    if scenario == "unicode":
        payload["text"] = "π — café — 日本語 — ✓"
        # A transcript line with a byte that is not valid UTF-8 at all.
        write(b'{"type":"assistant","message":"caf' + b"\xe9" + b' invalid"}\n')

    if scenario == "big":
        emit({"type": "assistant", "message": {"content": "x" * 250_000}})

    if scenario == "rate_limit":
        emit({"type": "rate_limit_event", "rate_limit": {"status": "rejected", "resetsAt": 1}})
    if scenario == "rate_limit_allowed":
        emit({"type": "rate_limit_event", "rate_limit": {"status": "allowed", "resetsAt": 1}})

    if scenario == "is_error":
        emit(
            result_envelope(
                argv,
                None,
                subtype="error_during_execution",
                is_error=True,
                api_error_status=529,
                stop_reason="error",
                terminal_reason="api_error",
                result="Overloaded",
                structured_output=None,
            )
        )
        return 1

    if scenario == "denied":
        emit(
            result_envelope(
                argv,
                payload,
                permission_denials=[
                    {"tool_name": "WebSearch", "tool_use_id": "toolu_017", "reason": "not allowed"}
                ],
            )
        )
        return 0

    if scenario == "no_structured":
        emit(result_envelope(argv, None, structured_output=None, result="here is some prose"))
        return 0

    if scenario == "downgraded":
        # A model below the Opus floor answered — the failure the guard exists to catch.
        emit(
            result_envelope(
                argv,
                payload,
                modelUsage={
                    MODEL_DOWNGRADE: {"inputTokens": 12, "outputTokens": 733},
                    MODEL_HOUSEKEEPING: {"inputTokens": 40, "outputTokens": 8},
                },
            )
        )
        return 0

    if scenario == "substituted":
        # A swap that is not a downgrade: `fable` answered by Opus 5, which is what the
        # CLI does for the `claude-fable-5` spelling. Degrade and carry on.
        emit(
            result_envelope(
                argv, payload, modelUsage={"claude-opus-5-20260601": {"inputTokens": 12}}
            )
        )
        return 0

    if scenario == "unrecognised_model":
        # A model newer than the engine's rank table. The CLI is behaving; the table is out
        # of date. Report it, keep the answer, and do not fail the run for being current.
        emit(
            result_envelope(
                argv,
                payload,
                modelUsage={
                    MODEL_UNRECOGNISED: {"inputTokens": 12, "outputTokens": 733},
                    MODEL_HOUSEKEEPING: {"inputTokens": 40, "outputTokens": 8},
                },
            )
        )
        return 0

    if scenario == "housekeeping_only":
        # Nothing but the CLI's own bookkeeping model. Unverifiable, not a substitution.
        emit(result_envelope(argv, payload, modelUsage={MODEL_HOUSEKEEPING: {"inputTokens": 4}}))
        return 0

    if scenario == "no_model_usage":
        emit(result_envelope(argv, payload, modelUsage=None))
        return 0

    emit(result_envelope(argv, payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
