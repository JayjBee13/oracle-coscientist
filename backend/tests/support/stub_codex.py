"""A fake `codex` for tests: replays recorded JSONL, on demand.

Every envelope shape here was observed from the real CLI on 2026-08-11 (codex-cli 0.146.0,
five live `codex exec` calls) — the `thread.started`/`turn.started`/`item.*`/`turn.completed`
sequence, the five usage counter names, the `web_search` item with its `action.query` and
`action.queries` forms, the two-stage unknown-model failure and its doubly-encoded error
payload. Tests select a scenario with `$STUB_CODEX_SCENARIO` and read back what the runner
sent from `$STUB_CODEX_RECORD`.

The stub writes the last-message file itself, because *that* is where the answer comes from
and its absence is how a failed turn is recognised. A scenario that fails writes no file, as
the real CLI does not.

This is a script, not a module: it is executed as `python stub_codex.py <codex argv...>`, so
it must not import anything from the application.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

USAGE = {
    "input_tokens": 16891,
    "cached_input_tokens": 12004,
    "cache_write_input_tokens": 402,
    "output_tokens": 733,
    "reasoning_output_tokens": 103,
}


def emit(envelope: dict) -> None:
    sys.stdout.buffer.write(json.dumps(envelope, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def flag(argv: list[str], name: str, default: str = "") -> str:
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
    return default


def overrides(argv: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for index, item in enumerate(argv):
        if item == "-c" and index + 1 < len(argv) and "=" in argv[index + 1]:
            key, _, value = argv[index + 1].partition("=")
            found[key.strip()] = value.strip()
    return found


def record(argv: list[str], stdin_text: str) -> None:
    path = os.environ.get("STUB_CODEX_RECORD")
    if not path:
        return
    entry = {
        "argv": argv,
        "stdin": stdin_text,
        "cwd": os.getcwd(),
        "env": dict(os.environ),
        "pid": os.getpid(),
        "schema": _schema(argv),
    }
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _schema(argv: list[str]) -> dict | None:
    path = flag(argv, "--output-schema")
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_answer(argv: list[str], payload: object) -> None:
    """The `-o` last-message file. Bare JSON, no fence, no trailing newline — as observed."""
    path = flag(argv, "-o")
    if not path:
        return
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False))


def api_error(status: int, message: str) -> dict:
    """The doubly-encoded failure the real CLI emits: a JSON string inside `message`."""
    return {
        "type": "error",
        "message": json.dumps(
            {
                "type": "error",
                "status": status,
                "error": {"type": "invalid_request_error", "message": message},
            }
        ),
    }


def main() -> int:
    argv = sys.argv[1:]
    if "--version" in argv:
        sys.stdout.buffer.write(b"codex-cli 0.146.0\n")
        return 0

    stdin_text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    record(argv, stdin_text)

    scenario = os.environ.get("STUB_CODEX_SCENARIO", "ok")
    config = overrides(argv)
    head = stdin_text.splitlines()[0] if stdin_text.splitlines() else ""
    payload = {
        "ok": True,
        "stdin_head": head,
        "model": flag(argv, "-m"),
        "effort": config.get("model_reasoning_effort", ""),
        "web_search": config.get("tools.web_search", ""),
    }

    emit({"type": "thread.started", "thread_id": "019ff108-a9cc-7bf2-803f-188fbf1bec87"})
    emit({"type": "turn.started"})

    if scenario == "unknown_model":
        # The real two-stage failure. The first line is the decoy: client-side metadata only,
        # which the runner must treat as a refusal in its own right.
        emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "error",
                    "message": (
                        f"Model metadata for `{flag(argv, '-m')}` not found. Defaulting to "
                        "fallback metadata; this can degrade performance and cause issues."
                    ),
                },
            }
        )
        message = (
            f"The '{flag(argv, '-m')}' model is not supported when using Codex with a "
            "ChatGPT account."
        )
        emit(api_error(400, message))
        emit({"type": "turn.failed", "error": {"message": api_error(400, message)["message"]}})
        return 1

    if scenario in ("fallback_metadata_then_ok", "fallback_metadata_then_killed"):
        # The nastier half of the same trap: metadata missing, turn succeeds anyway, and the
        # answer came from a model running with another one's context window.
        # `_then_killed` adds the second half of the trap — the same refusal on a call that
        # is then killed at its ceiling with its answer already written, which is the one
        # path that used to return it as a good result.
        emit(
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "error",
                    "message": (
                        "Model metadata for `gpt-5.6-sol` not found. Defaulting to fallback "
                        "metadata; this can degrade performance and cause issues."
                    ),
                },
            }
        )

    if scenario == "bad_effort":
        message = (
            "[ReasoningEffortParam] [reasoning.effort] [invalid_enum_value] Invalid value: "
            f"'{config.get('model_reasoning_effort')}'. Supported values are: 'none', "
            "'minimal', 'low', 'medium', 'high', 'xhigh', and 'max'."
        )
        emit(api_error(400, message))
        emit({"type": "turn.failed", "error": {"message": api_error(400, message)["message"]}})
        return 1

    if scenario == "rate_limited":
        message = "Rate limit reached for this account; try again later."
        emit(api_error(429, message))
        emit({"type": "turn.failed", "error": {"message": api_error(429, message)["message"]}})
        return 1

    if scenario == "slow":
        time.sleep(120)
        return 0

    if scenario in ("answered_then_hangs", "fallback_metadata_then_killed"):
        # The answer file is written and the CLI has not yet said `turn.completed`. The
        # Claude runner lost a whole run's evolution to exactly this window.
        write_answer(argv, payload)
        emit(
            {
                "type": "item.completed",
                "item": {"id": "item_1", "type": "agent_message", "text": json.dumps(payload)},
            }
        )
        time.sleep(120)
        return 0

    if scenario.startswith("empty_object"):
        # The `-o` file holds a bare `{}`. No role schema admits that — every one of them
        # requires at least one property — so it is the CLI saying the shape was not
        # honoured, not an answer. Four combinations, because `{}` reaches the runner by
        # four different routes: `_file` puts the real payload in the transcript beside the
        # empty file (the case where the file must not win), and `_then_hangs` makes the
        # whole thing arrive through the salvage path instead of a completed turn.
        path = flag(argv, "-o")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{}")
        if "_file" in scenario:
            emit(
                {
                    "type": "item.completed",
                    "item": {
                        "id": "item_1",
                        "type": "agent_message",
                        "text": json.dumps(payload, ensure_ascii=False),
                    },
                }
            )
        if scenario.endswith("_then_hangs"):
            time.sleep(120)
            return 0
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if scenario == "spent_then_hangs":
        # Tokens burned, no answer, no `turn.completed`, no last-message file.
        emit({"type": "item.started", "item": {"id": "item_1", "type": "agent_message"}})
        time.sleep(120)
        return 0

    if scenario == "exit_with_open_pipe":
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", "import time; time.sleep(20)"],
            stdin=subprocess.DEVNULL,
        )
        write_answer(argv, payload)
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if scenario == "no_turn_completed":
        sys.stderr.buffer.write(b"codex: fatal: connection reset by peer\n")
        sys.stderr.buffer.flush()
        return 3

    if scenario == "no_answer_file":
        # `turn.completed` with nothing written. Must never read back as an empty answer.
        emit({"type": "turn.completed", "usage": USAGE})
        return 0

    if scenario == "benign_stderr":
        # Verbatim from the live probe: an ERROR line on a call that exits 0.
        sys.stderr.buffer.write(
            b"2026-08-11T13:35:04.371050Z ERROR codex_models_manager::cache: failed to load "
            b"models cache: missing field `base_instructions` at line 94 column 5\n"
        )
        sys.stderr.buffer.flush()

    if config.get("tools.web_search") == "true":
        emit(
            {
                "type": "item.started",
                "item": {"id": "exec-a", "type": "web_search", "query": "",
                         "action": {"type": "other"}},
            }
        )
        emit(
            {
                "type": "item.completed",
                "item": {"id": "exec-a", "type": "web_search", "query": "first check",
                         "action": {"type": "search", "query": "first check"}},
            }
        )
        emit(
            {
                "type": "item.completed",
                "item": {"id": "exec-b", "type": "web_search",
                         "action": {"type": "search", "queries": ["second check", "third"]}},
            }
        )

    if scenario == "garbage":
        sys.stdout.buffer.write(b"not json at all\n")
        sys.stdout.buffer.write(b"\xff\xfe \x00truncated binary\n")
        sys.stdout.buffer.flush()
        emit({"type": "some_future_envelope", "payload": {"unknown": True}})

    if scenario == "unicode":
        payload["text"] = "π — café — 日本語 — ✓"
        sys.stdout.buffer.write(b'{"type":"item.started","item":"caf' + b"\xe9" + b' bad"}\n')
        sys.stdout.buffer.flush()

    if scenario == "fenced":
        # Prompt-stated contract, and the model wrapped it anyway. A correct payload must
        # not be lost to three backticks.
        path = flag(argv, "-o")
        if path:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("```json\n" + json.dumps(payload, ensure_ascii=False) + "\n```")
    else:
        write_answer(argv, payload)

    emit(
        {
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": json.dumps(payload)},
        }
    )

    if scenario == "no_usage":
        emit({"type": "turn.completed"})
        return 0

    emit({"type": "turn.completed", "usage": USAGE})
    return 0


if __name__ == "__main__":
    sys.exit(main())
