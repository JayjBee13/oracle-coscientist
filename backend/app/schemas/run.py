from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

HarnessName = Literal["claude", "codex", "fake"]
EngineVersion = Literal["v1", "v2"]
GroundingMode = Literal["built_in_web", "perplexity"]
GroundingDepth = Literal["shallow", "standard", "deep"]
PromptMode = Literal["prompt_workshop", "co_scientist_batch", "finish"]
HarnessEventType = Literal["status", "process_event", "artifact_changed", "error"]
RunControlAction = Literal["pause", "stop", "continue", "extend", "finish", "clone"]
RunLifecycle = Literal[
    "draft",
    "refining",
    "prompt_ready",
    "queued",
    "running",
    "pause_requested",
    "paused",
    "stopping",
    "stopped",
    "finishing",
    "completed",
    "failed",
    "abandoned",
    "imported",
    "unknown",
]
ArtifactKind = Literal[
    "state_json",
    "research_overview",
    "ideas_ranked_csv",
    "hypothesis_markdown",
    "other",
]


class GroundingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: GroundingMode = "built_in_web"
    depth: GroundingDepth = "standard"
    allow_fallback: bool = False


class BatchConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    max_rounds: int = Field(
        default=1,
        ge=1,
        le=25,
        validation_alias=AliasChoices("max_rounds", "rounds"),
    )
    budget: int = Field(default=20, ge=1, le=10_000)
    matches_per_round: int = Field(default=3, ge=1, le=100)
    top_k: int = Field(default=3, ge=1, le=25)
    stop_after_current_round: bool = False


# The launch and control request bodies that used to live here are now in
# `schemas/launch.py`, shaped to plan C5: a run is created from a question and a config,
# not from a `version` and a `final_prompt`, and the control actions are C4's table rather
# than this module's `continue`/`extend`/`clone`. What remains below is the vocabulary the
# legacy artifact reader still speaks, which goes away with the legacy tables.


class RunDryRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    process_id: str
    engine_run_id: str
    harness: Literal["claude", "codex"]
    version: EngineVersion
    engine_root: str
    cwd: str
    argv: list[str]
    real_runs_enabled: bool
    would_execute: bool = False


class PromptPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: PromptMode
    version: EngineVersion
    engine_root: Path
    engine_run_id: str | None = None
    final_prompt: str = Field(min_length=1)
    constraints: list[str] = Field(default_factory=list)
    rejected_directions: list[str] = Field(default_factory=list)
    grounding: GroundingConfig = Field(default_factory=GroundingConfig)
    batch: BatchConfig = Field(default_factory=BatchConfig)
    required_outputs: list[str] = Field(default_factory=lambda: ["state.json", "hypotheses"])

    @field_validator("engine_root")
    @classmethod
    def engine_root_must_be_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("engine_root must be an absolute path")
        return value


class HarnessEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sequence: int = Field(ge=1)
    event_type: HarnessEventType
    stream: str = "system"
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any]


class HarnessResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harness: HarnessName
    engine_run_id: str
    success: bool
    status_line: str
    events: list[HarnessEvent] = Field(min_length=1)


class RunPaths(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str
    run_dir: str
    state_json: str


class RunSettingsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grounding_mode: GroundingMode = "built_in_web"
    grounding_depth: GroundingDepth = "standard"
    rounds_target: int | None = None
    budget: int | None = None
    matches_per_round: int | None = None
    top_k: int | None = None


class RunProgressDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iteration: int | None = None
    calls_used: int | None = None
    budget: int | None = None
    phase: str = "idle"
    percent_budget: int | None = None


class RunCountsDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: int = 0
    rejected: int = 0
    archived: int = 0
    total: int = 0
    matches: int = 0
    clusters: int = 0


class HypothesisSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    file: str | None = None
    elo: float | None = None
    matches: int = 0
    wins: int = 0
    status: str = "unknown"
    verdict: str | None = None
    cluster: str | None = None
    created_iter: int | None = None
    parent: str | None = None


class GraftDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    applicable: bool
    enabled: bool = False
    fired_count: int = 0
    pending_injection: bool = False


class ArtifactSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    path: str
    kind: ArtifactKind
    size: int | None = None
    mtime: datetime | None = None


class ProcessSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active: bool = False
    pid: int | None = None
    state: str = "not_tracked"
    last_event_at: datetime | None = None


class NormalizedRunDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    engine_run_id: str
    harness: Literal["claude", "codex"] = "claude"
    version: EngineVersion
    lifecycle: RunLifecycle
    title: str
    goal: str
    paths: RunPaths
    settings: RunSettingsDTO
    progress: RunProgressDTO
    counts: RunCountsDTO
    top: list[HypothesisSummary] = Field(default_factory=list)
    graft: GraftDTO
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    process: ProcessSummary = Field(default_factory=ProcessSummary)
    warnings: list[str] = Field(default_factory=list)
