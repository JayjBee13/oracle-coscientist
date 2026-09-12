"""The research workspace shown to a scientist; full provenance stays in engine state."""

from pydantic import BaseModel, Field


class ResearchApproach(BaseModel):
    id: str
    framing: str
    method: str
    assumptions: list[str]


class ResearchSubproblem(BaseModel):
    id: str
    question: str
    depends_on: list[str]
    acceptance_test: str


class ResearchDecision(BaseModel):
    checkpoint: int
    action: str
    reason: str


class ResearchDimension(BaseModel):
    grade: str
    rationale: str


class ResearchSource(BaseModel):
    url: str
    finding: str
    relation: str


class ResearchClaim(BaseModel):
    id: str
    claim: str
    status: str
    rationale: str
    sources: list[ResearchSource]


class ResearchCalculation(BaseModel):
    claim_id: str
    expression: str
    expected: float
    tolerance: float
    assumption: str
    result: float | None = None
    passed: bool
    error: str | None = None


class ResearchCandidate(BaseModel):
    hid: str
    title: str
    approach: str
    readiness: str
    dimensions: dict[str, ResearchDimension]
    claims: list[ResearchClaim]
    next_test: str
    calculation_results: list[ResearchCalculation]


class ResearchDependency(BaseModel):
    subproblem_id: str
    solution_hids: list[str]
    status: str
    rationale: str


class ResearchContradiction(BaseModel):
    hid: str
    title: str
    claims: list[ResearchClaim]


class ResearchSourceStrategy(BaseModel):
    published_research_percent: int = Field(ge=0, le=100)
    rationale: str
    scholarly_queries: list[str]
    other_source_queries: list[str]


class ResearchView(BaseModel):
    source_strategy: ResearchSourceStrategy | None = None
    contradictions: list[ResearchContradiction] = Field(default_factory=list)
    phase: str = "queued"
    objective: str = ""
    approaches: list[ResearchApproach] = Field(default_factory=list)
    subproblems: list[ResearchSubproblem] = Field(default_factory=list)
    decisions: list[ResearchDecision] = Field(default_factory=list)
    portfolio: list[ResearchCandidate] = Field(default_factory=list)
    synthesis_round: int | None = None
    synthesis_markdown: str = ""
    challenge_assessment: str = ""
    blocking_issues: list[str] = Field(default_factory=list)
    dependencies: list[ResearchDependency] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    recommendation_ready: bool = False
