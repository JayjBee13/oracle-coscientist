"""Bounded contracts for the adaptive workspace; no model controls execution."""

from __future__ import annotations

import ast
import math
import operator
from typing import Any
from urllib.parse import urlparse


def obj(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def string(limit: int = 4000) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": limit}


def array(item: dict, maximum: int = 12, minimum: int = 0) -> dict:
    return {"type": "array", "items": item, "maxItems": maximum, "minItems": minimum}


DIMENSIONS = ("originality", "usefulness", "feasibility", "upside")
GRADES = ("unknown", "low", "moderate", "high")
FRAMING_SCHEMA = obj(
    objective=string(),
    source_strategy=obj(
        published_research_percent={"type": "integer", "minimum": 0, "maximum": 100},
        rationale=string(1600),
        scholarly_queries=array(string(500), 4),
        other_source_queries=array(string(500), 4),
    ),
    success_criteria=array(string(), 8, 1),
    approaches=array(
        obj(id=string(40), framing=string(), method=string(), assumptions=array(string(), 8, 1)),
        4,
        3,
    ),
    subproblems=array(
        obj(
            id=string(40),
            question=string(),
            depends_on=array(string(40), 8),
            acceptance_test=string(),
        ),
        8,
    ),
    holistic_route=string(),
)
VERIFICATION_SCHEMA = obj(
    claims=array(
        obj(
            id=string(40),
            claim=string(),
            status={"enum": ["supported", "contradicted", "unresolved"]},
            rationale=string(),
            sources=array(
                obj(
                    url=string(2000),
                    finding=string(),
                    relation={"enum": ["supports", "contradicts"]},
                ),
                6,
            ),
        ),
        10,
        1,
    ),
    calculations=array(
        obj(
            claim_id=string(40),
            expression=string(300),
            expected={"type": "number"},
            tolerance={"type": "number"},
            assumption=string(),
        ),
        6,
    ),
    dimensions=obj(
        **{name: obj(grade={"enum": list(GRADES)}, rationale=string()) for name in DIMENSIONS}
    ),
    open_questions=array(string(), 10),
    next_test=string(),
)
SYNTHESIS_SCHEMA = obj(
    markdown=string(28000),
    used_hids=array(string(40), 12, 1),
    dependencies=array(
        obj(
            subproblem_id=string(40),
            solution_hids=array(string(40), 8),
            status={"enum": ["addressed", "open"]},
            rationale=string(),
        ),
        8,
    ),
    incompatibilities=array(string(), 10),
    unresolved=array(string(), 10),
)
CHALLENGE_SCHEMA = obj(
    assessment=string(12000),
    blocking_issues=array(string(), 10),
    next_action={"enum": ["reframe", "develop", "verify", "none"]},
    target_hids=array(string(40), 12),
    tests=array(string(), 10),
)
RESEARCH_SCHEMAS = {
    "framing": FRAMING_SCHEMA,
    "verification": VERIFICATION_SCHEMA,
    "synthesis": SYNTHESIS_SCHEMA,
    "challenge": CHALLENGE_SCHEMA,
}


def validate_framing(data: dict) -> str | None:
    approaches = [row["id"] for row in data["approaches"]]
    if len(set(approaches)) != len(approaches):
        return "approach ids must be unique"
    graph = {row["id"]: row["depends_on"] for row in data["subproblems"]}
    if len(graph) != len(data["subproblems"]):
        return "subproblem ids must be unique"
    visited, visiting = set(), set()

    def visit(node: str) -> bool:
        if node not in graph or node in visiting:
            return False
        if node in visited:
            return True
        visiting.add(node)
        if not all(visit(child) for child in graph[node]):
            return False
        visiting.remove(node)
        visited.add(node)
        return True

    if not all(visit(node) for node in graph):
        return "subproblem dependencies must reference existing ids and be acyclic"
    return None


def calculate(expression: str) -> float:
    """Only bounded real arithmetic. No names, calls, attributes, powers or evaluation."""
    if len(expression) > 300:
        raise ValueError("expression too long")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 80:
        raise ValueError("expression too complex")
    binary = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
    }

    def evaluate(node: ast.AST) -> float:
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = float(node.value)
        elif isinstance(node, ast.BinOp) and type(node.op) in binary:
            result = binary[type(node.op)](evaluate(node.left), evaluate(node.right))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            result = evaluate(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        else:
            raise ValueError("only numeric +, -, *, / and parentheses are allowed")
        if not math.isfinite(result) or abs(result) > 1e100:
            raise ValueError("arithmetic outside finite bounds")
        return result

    return evaluate(tree.body)


def assess(data: dict, *, searched: bool) -> dict:
    """Evidence labels are conservative; arithmetic never proves its own premises."""
    checks = []
    for check in data["calculations"]:
        record = dict(check)
        try:
            result = calculate(check["expression"])
            tolerance = float(check["tolerance"])
            expected = float(check["expected"])
            if not math.isfinite(expected) or not 0 <= tolerance <= 1e-3 * max(1, abs(expected)):
                raise ValueError("invalid expected value or excessive tolerance")
            record.update(result=result, passed=abs(result - expected) <= tolerance)
        except (ValueError, SyntaxError, OverflowError, ZeroDivisionError, RecursionError) as exc:
            record.update(passed=False, error=str(exc)[:200])
        checks.append(record)
    claims = []
    for claim in data["claims"]:
        sources = [source for source in claim["sources"] if valid_source_url(source["url"])]
        linked = [check for check in checks if check["claim_id"] == claim["id"]]
        status = claim["status"]
        # Claimed citations alone are not evidence that a search actually happened.
        relation = "supports" if status == "supported" else "contradicts"
        if status != "unresolved" and not (
            searched and any(s["relation"] == relation for s in sources)
        ):
            status = "unresolved"
        if status == "supported" and any(not check["passed"] for check in linked):
            status = "unresolved"
        if status == "supported" and any(s["relation"] == "contradicts" for s in sources):
            status = "unresolved"
        claims.append({**claim, "sources": sources, "status": status})
    contradicted = any(c["status"] == "contradicted" for c in claims)
    supported = bool(claims) and all(c["status"] == "supported" for c in claims)
    return {
        **data,
        "claims": claims,
        "calculation_results": checks,
        "searched": searched,
        "readiness": "contradicted"
        if contradicted
        else ("source_supported" if supported and not data["open_questions"] else "provisional"),
        "evidence_limit": "Source relationships are model-assessed; arithmetic checks only "
        "the stated expression. No empirical experiment was performed.",
    }


def valid_source_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return bool(parsed.scheme in ("http", "https") and parsed.hostname)
    except ValueError:
        return False


def validate_verification(data: dict) -> str | None:
    ids = [claim["id"] for claim in data["claims"]]
    if len(set(ids)) != len(ids):
        return "claim ids must be unique"
    for check in data["calculations"]:
        if check["claim_id"] not in ids:
            return "calculation references an unknown claim"
        try:
            if any(not math.isfinite(float(check[key])) for key in ("expected", "tolerance")):
                return "calculation numbers must be finite"
        except (ValueError, OverflowError):
            return "calculation numbers must be finite"
    return None


def portfolio(rows: list[dict], membership: dict, assessments: dict, limit: int = 6) -> list[dict]:
    """Approach coverage, then nondominated dimensions. Elo never enters selection."""
    available = [
        row
        for row in rows
        if row["status"] == "active"
        and assessments.get(row["hid"], {}).get("readiness") != "contradicted"
    ]

    def vector(row: dict) -> tuple:
        record = assessments.get(row["hid"], {})
        return (
            int(record.get("readiness") == "source_supported"),
            *(
                GRADES.index(record.get("dimensions", {}).get(key, {}).get("grade", "unknown"))
                for key in DIMENSIONS
            ),
        )

    vectors = {row["hid"]: vector(row) for row in available}
    unique = set(vectors.values())
    dominated = {
        values
        for values in unique
        if any(
            all(a >= b for a, b in zip(other, values, strict=True)) and other != values
            for other in unique
        )
    }

    ordered = sorted(
        available,
        key=lambda row: (
            vectors[row["hid"]] in dominated,
            -sum(vectors[row["hid"]]),
            -int(row.get("created_round") or 0),
            row["hid"],
        ),
    )
    selected, covered = [], set()
    for row in ordered:
        approach = membership.get(row["hid"], "unassigned")
        if approach not in covered:
            selected.append(row)
            covered.add(approach)
            if len(selected) == limit:
                return selected
    return (selected + [row for row in ordered if row not in selected])[:limit]


def next_action(number: int, research: dict) -> tuple[str, str]:
    """A bounded scheduler with explicit breadth recovery, rather than model discretion."""
    if not research.get("membership"):
        return "explore", "Establish independent approaches before selecting a winner."
    challenge = research.get("challenge") or {}
    if challenge.get("blocking_issues") and challenge.get("next_action") == "reframe":
        return "reframe", "The complete-answer challenge disputes a problem assumption."
    decisions = research.get("decisions") or {}
    previous = (decisions.get(str(number - 1)) or {}).get("action")
    if previous in ("develop", "verify", "reframe"):
        return "explore", "Protect fresh creative search after targeted critical work."
    if challenge.get("blocking_issues"):
        action = challenge.get("next_action", "develop")
        return (
            action if action in ("develop", "verify") else "develop",
            "Resolve the previous complete answer's decisive weakness.",
        )
    return "develop", "Integrate complementary strengths and solve remaining dependencies."
