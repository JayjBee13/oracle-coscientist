import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.schemas.run import PromptPackage


@dataclass(frozen=True)
class FakeRunSettings:
    rounds: int = 1
    budget: int = 20
    matches_per_round: int = 3
    top_k: int = 3


@dataclass(frozen=True)
class FakeRunRequest:
    engine_run_id: str
    version: str
    goal: str
    settings: FakeRunSettings
    prompt_package: PromptPackage | None = None


class FakeRunWriter:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write_run(self, request: FakeRunRequest) -> Path:
        run_dir = self.write_progress_run(request, rounds_completed=request.settings.rounds)
        status = _status_payload(
            engine_run_id=request.engine_run_id,
            phase="complete",
            rounds_completed=request.settings.rounds,
            stop_reason="completed",
        )

        (run_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        (run_dir / "research_overview.md").write_text("# Fake Run Overview\n", encoding="utf-8")
        return run_dir

    def write_finished_run(
        self,
        request: FakeRunRequest,
        rounds_completed: int,
        stop_reason: str,
    ) -> Path:
        run_dir = self.write_progress_run(request, rounds_completed=rounds_completed)
        status = _status_payload(
            engine_run_id=request.engine_run_id,
            phase="complete",
            rounds_completed=rounds_completed,
            stop_reason=stop_reason,
        )

        (run_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        (run_dir / "research_overview.md").write_text("# Fake Run Overview\n", encoding="utf-8")
        return run_dir

    def write_initial_run(self, request: FakeRunRequest) -> Path:
        run_dir = self.root / "runs" / request.engine_run_id
        hypotheses_dir = run_dir / "hypotheses"
        hypotheses_dir.mkdir(parents=True, exist_ok=True)

        status = _status_payload(
            engine_run_id=request.engine_run_id,
            phase="running",
            rounds_completed=0,
            stop_reason=None,
        )
        state = _state_payload(request, rounds_completed=0, include_hypothesis=False)

        (run_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        (run_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        prompt_package_payload = (
            request.prompt_package.model_dump(mode="json") if request.prompt_package else None
        )
        if prompt_package_payload is not None:
            (run_dir / "prompt_package.json").write_text(
                json.dumps(prompt_package_payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return run_dir

    def write_progress_run(self, request: FakeRunRequest, rounds_completed: int) -> Path:
        run_dir = self.write_initial_run(request)
        hypothesis_path = run_dir / "hypotheses" / "h001.md"
        hypothesis_path.write_text(
            (
                "# Fake Hypothesis\n"
                "**Claim:** Deterministic fake run artifact.\n"
                "**Test:** Parser should normalize this artifact.\n"
            ),
            encoding="utf-8",
        )

        state = _state_payload(
            request,
            rounds_completed=rounds_completed,
            include_hypothesis=True,
        )
        status = _status_payload(
            engine_run_id=request.engine_run_id,
            phase="running",
            rounds_completed=rounds_completed,
            stop_reason=None,
        )
        (run_dir / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        (run_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        return run_dir

    def write_terminal_status(
        self,
        request: FakeRunRequest,
        phase: str,
        rounds_completed: int,
        stop_reason: str,
    ) -> Path:
        run_dir = self.root / "runs" / request.engine_run_id
        status = _status_payload(
            engine_run_id=request.engine_run_id,
            phase=phase,
            rounds_completed=rounds_completed,
            stop_reason=stop_reason,
        )
        (run_dir / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
        return run_dir


def _state_payload(
    request: FakeRunRequest,
    rounds_completed: int,
    include_hypothesis: bool,
) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat()
    hypotheses = {}
    if include_hypothesis:
        hypotheses["h001"] = {
            "id": "h001",
            "title": "Fake Hypothesis",
            "file": f"runs/{request.engine_run_id}/hypotheses/h001.md",
            "elo": 1200,
            "matches": 0,
            "wins": 0,
            "status": "active",
            "review": {"verdict": "pass", "note": "fake run"},
            "cluster": "c-fake",
            "created_iter": 1,
            "parent": None,
        }

    state = {
        "run_id": request.engine_run_id,
        "goal": request.goal,
        "config": {
            "initial_elo": 1200,
            "k_factor": 32,
            "max_llm_calls": request.settings.budget,
            "matches_per_round": request.settings.matches_per_round,
            "evolve_top_k": request.settings.top_k,
        },
        "calls_used": min(request.settings.budget, rounds_completed * 2),
        "iteration": rounds_completed,
        "next_id": 2 if include_hypothesis else 1,
        "hypotheses": hypotheses,
        "matches": [],
        "feedback": "fake run complete" if include_hypothesis else "fake run started",
        "created": now,
        "updated": now,
    }
    if request.version == "v2":
        state["collapse"] = {"history": [], "last_fired_iter": None, "fired_count": 0}
        state["pending_injection"] = ""
        state["config"]["graft"] = {"enabled": True}
    return state


def _status_payload(
    engine_run_id: str,
    phase: str,
    rounds_completed: int,
    stop_reason: str | None,
) -> dict[str, Any]:
    return {
        "engine_run_id": engine_run_id,
        "phase": phase,
        "rounds_completed": rounds_completed,
        "stop_reason": stop_reason,
    }
