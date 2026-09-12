"""The JSON contract every role must satisfy.

Each schema here is handed to the CLI as `--json-schema` (plan C1) *and* re-validated on
the way back in. Both halves matter: the flag makes the model emit the right shape, and
`validate_role_output` catches the case where it did not — a silently reshaped payload is
how the archived runs ended up with unusable titles and matches that never scored.

`validate_role_output` returns a human-readable error string rather than raising, because
that string is what the orchestrator appends to the prompt for its one repair re-ask. A
second failure is a `contract_violation` event and a skipped unit, never a crash.

The validator is a deliberately small subset of JSON Schema — the constructs these
contracts actually use (`type`, `required`, `properties`, `additionalProperties`, `items`,
`enum`, `minItems`, `maxItems`, `maxLength`, `minLength`). Adding a dependency to check nine literal
dicts would be a poor trade; anything this validator does not understand is ignored rather
than silently treated as satisfied, and the schemas below are covered by unit tests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.engine.core import HYPOTHESIS_FIELD_ORDER
from app.engine.research_contracts import RESEARCH_SCHEMAS

__all__ = [
    "CARTOGRAPHER_SCHEMA",
    "CLAIM_MAX_LENGTH",
    "EVOLUTION_SCHEMA",
    "GENERATION_SCHEMA",
    "HYPOTHESIS_FIELDS",
    "META_REVIEW_SCHEMA",
    "OPERATORS",
    "OVERVIEW_SCHEMA",
    "PROXIMITY_SCHEMA",
    "RANKING_SCHEMA",
    "REFLECTION_SCHEMA",
    "SCHEMA_BY_ROLE",
    "TITLE_MAX_LENGTH",
    "WORKSHOP_SCHEMA",
    "hypothesis_fields",
    "schema_for",
    "unwrap_prose",
    "validate_role_output",
]

TITLE_MAX_LENGTH = 120

CLAIM_MAX_LENGTH = 1_000
"""One or two sentences, with generous room. Declared because `claim` is not only read.

`prompts.claim_of` lifts this field out of the body and it is the entire content of the
`id | title | claim` line proximity clusters on. generation.md constrains it to "the core
hypothesis in 1–2 sentences"; evolution.md said only "the same six fields the Generation
agent produces", so run c4566ed2's evolved rows put a prior-art dossier there — 1,631,
1,701 and 1,917 characters each, all three beginning "PRIOR ART & RESIDUAL WEDGE: (1) …"
with the actual claim ~1,400 characters in. The clustering call was reading citations.

The number is set above the whole observed *generated* distribution (493–918 characters on
that run, produced with no limit declared at all) and below every evolved dossier, so it
constrains the failure without costing a repair call on the behaviour that was already
fine. `hypothesis_fields` clips as well, so a model that overshoots twice still stores a
usable body rather than losing the unit."""

HYPOTHESIS_FIELDS: dict[str, Any] = {
    "title": {"type": "string", "maxLength": TITLE_MAX_LENGTH},
    "claim": {"type": "string", "maxLength": CLAIM_MAX_LENGTH},
    "mechanism": {"type": "string"},
    "novelty": {"type": "string"},
    "test": {"type": "string"},
    "assumptions": {"type": "string"},
}

# The stored body is composed from these fields in this order (`core.compose_hypothesis_md`).
# If the two ever drift, a hypothesis would validate here and lose a section on the way to
# disk, so the parity is asserted at import rather than left to a test.
assert tuple(HYPOTHESIS_FIELDS) == HYPOTHESIS_FIELD_ORDER, (
    "HYPOTHESIS_FIELDS must match core.HYPOTHESIS_FIELD_ORDER exactly, in order"
)

OPERATORS: tuple[str, ...] = ("grounding", "combination", "simplification", "out_of_box")

_HYPOTHESIS_ITEM: dict[str, Any] = {
    "type": "object",
    "required": list(HYPOTHESIS_FIELDS),
    "properties": HYPOTHESIS_FIELDS,
}

GENERATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["hypotheses"],
    "properties": {
        "hypotheses": {"type": "array", "minItems": 1, "items": _HYPOTHESIS_ITEM},
    },
}

# Evolution is generation plus provenance: which hypotheses this variant came from and
# which of the four strategies produced it. The originals are never edited, so the lineage
# has to travel with the child or it is lost.
_EVOLVED_ITEM: dict[str, Any] = {
    "type": "object",
    "required": [*HYPOTHESIS_FIELDS, "derived_from", "operator"],
    "properties": {
        **HYPOTHESIS_FIELDS,
        "derived_from": {"type": "array", "items": {"type": "string"}},
        "operator": {"enum": list(OPERATORS)},
    },
}

EVOLUTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["hypotheses"],
    "properties": {
        "hypotheses": {"type": "array", "minItems": 1, "items": _EVOLVED_ITEM},
    },
}

# The five prose fields carry minimum lengths, and the validator enforces them, because in
# the owner's real run every one of them was corrupted and every one validated cleanly. The
# model wrote JSON-encoded values into fields declared `type: string`: h001's correctness
# was `{"summary":"No fundamental flaw; …"}`, h002's began `{"n":1}`, and for h006 the
# entire critique was four fields each holding the literal two-character string `{}`. A
# schema with no floor cannot tell an empty critique from a critique, so `{}` and `""` sailed
# through and were rendered verbatim into every ranking prompt and into the GUI.
_PROSE_MIN = 40
_NOTE_MIN = 20

REFLECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict", "novelty", "correctness", "testability", "key_risk", "note"],
    "properties": {
        "verdict": {"enum": ["pass", "reject"]},
        "novelty": {
            "type": "object",
            "required": ["level", "note"],
            "properties": {
                "level": {"enum": ["high", "moderate", "low"]},
                "note": {"type": "string", "minLength": _NOTE_MIN},
            },
        },
        "correctness": {"type": "string", "minLength": _PROSE_MIN},
        "testability": {"type": "string", "minLength": _PROSE_MIN},
        "key_risk": {"type": "string", "minLength": _PROSE_MIN},
        "note": {"type": "string", "minLength": _PROSE_MIN},
    },
}

# hid -> short cluster label. Free-form labels (not an enum) because the agent invents the
# grouping; the engine only cares which ids share a string.
#
# `families` is the second, coarser reading of the same list: what KIND of thing each
# hypothesis is, in a handful of buckets, as distinct from the fine mechanism cluster. It is
# **not** in `required`, and that is deliberate rather than an oversight. It exists only to
# be measured — the top-family share of one round's new ideas is recorded on the graft event
# and nothing consumes it yet — so a model that omits it must cost the run nothing. Required
# would buy a repair re-ask, and a second failure marks proximity failed, which would let a
# telemetry field take out the round's clustering. The prompt asks for it in prose; the
# recorded `n_labelled` vs `n_total` says plainly when it did not arrive.
PROXIMITY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["clusters"],
    "properties": {
        "clusters": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"type": "string"},
            "description": "id -> fine mechanism cluster label; ids sharing a label are "
            "the same idea",
        },
        "families": {
            "type": "object",
            "additionalProperties": {"type": "string"},
            "description": "id -> coarse family label (4-8 across the whole list): what "
            "kind of thing the hypothesis is, not which mechanism it proposes",
        },
    },
}

# `winner` is a PRESENTATION POSITION (1 or 2), not a side. The orchestrator maps it onto
# the pair's a/b with `core.winner_side`, because rematches swap the order shown.
RANKING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["debate", "winner"],
    "properties": {
        "debate": {"type": "string"},
        "winner": {"enum": [1, 2]},
    },
}

# The same prose floor as the reflection fields, and for a stronger reason: guidance is the
# only artefact that crosses a round boundary. `''` or `{"summary": …}` would validate, be
# stored in `feedback_history`, and be injected verbatim into both generation shards, the
# evolution call and the next meta-review — a whole round steered by an empty string. The
# floor is what makes the validator's JSON-literal check fire on these fields at all.
META_REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["recurring_issues", "what_wins", "guidance_next_round"],
    "properties": {
        "recurring_issues": {"type": "string", "minLength": _PROSE_MIN},
        "what_wins": {"type": "string", "minLength": _PROSE_MIN},
        "guidance_next_round": {"type": "string", "minLength": _PROSE_MIN},
    },
}

OVERVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["markdown"],
    "properties": {"markdown": {"type": "string"}},
}

CARTOGRAPHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["source_domain", "skeleton", "seed_framing"],
    "properties": {
        "source_domain": {"type": "string"},
        "skeleton": {"type": "string"},
        "seed_framing": {"type": "string"},
    },
}

_RECOMMENDED_SETTINGS: dict[str, Any] = {
    "type": "object",
    "required": ["rounds", "budget_calls", "matches_per_round", "grounding_depth"],
    "properties": {
        "rounds": {"type": "integer"},
        "budget_calls": {"type": "integer"},
        "matches_per_round": {"type": "integer"},
        "grounding_depth": {"enum": ["shallow", "standard", "deep"]},
    },
}

WORKSHOP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["options"],
    "properties": {
        "options": {
            "type": "array",
            "minItems": 2,
            "maxItems": 2,
            "items": {
                "type": "object",
                "required": [
                    "prompt",
                    "strategy",
                    "optimizes_for",
                    "excludes",
                    "rationale",
                    "recommended_settings",
                ],
                "properties": {
                    "prompt": {"type": "string"},
                    "strategy": {"type": "string"},
                    "optimizes_for": {"type": "string"},
                    "excludes": {"type": "string"},
                    "rationale": {"type": "string"},
                    "recommended_settings": _RECOMMENDED_SETTINGS,
                },
            },
        },
    },
}

SCHEMA_BY_ROLE: dict[str, dict[str, Any]] = {
    **RESEARCH_SCHEMAS,
    "generation": GENERATION_SCHEMA,
    "reflection": REFLECTION_SCHEMA,
    "proximity": PROXIMITY_SCHEMA,
    "ranking": RANKING_SCHEMA,
    "evolution": EVOLUTION_SCHEMA,
    "meta_review": META_REVIEW_SCHEMA,
    "overview": OVERVIEW_SCHEMA,
    "cartographer": CARTOGRAPHER_SCHEMA,
    "workshop": WORKSHOP_SCHEMA,
}


def schema_for(role: str) -> dict[str, Any]:
    """The enforced schema for a role. Unknown roles are a programming error."""
    try:
        return SCHEMA_BY_ROLE[role]
    except KeyError:
        raise KeyError(f"no output schema for role {role!r}") from None


def hypothesis_fields(item: Mapping[str, Any]) -> dict[str, str]:
    """The six body fields, stripped, with the title clipped to the schema's limit.

    Clipping rather than rejecting: a title one character over the limit is not worth a
    repair call, and `compose_hypothesis_md` needs all six fields present regardless.
    """
    fields = {name: str(item.get(name, "")).strip() for name in HYPOTHESIS_FIELD_ORDER}
    if len(fields["title"]) > TITLE_MAX_LENGTH:
        fields["title"] = fields["title"][: TITLE_MAX_LENGTH - 1].rstrip() + "…"
    if len(fields["claim"]) > CLAIM_MAX_LENGTH:
        fields["claim"] = fields["claim"][: CLAIM_MAX_LENGTH - 1].rstrip() + "…"
    return fields


# --------------------------------------------------------------------------- validation


def validate_role_output(role: str, data: Any) -> str | None:
    """Check a role's parsed output against its schema.

    Returns `None` when the payload is usable, or a single line naming every problem —
    the exact text the orchestrator appends to the prompt when it re-asks once.
    """
    if data is None:
        return "no structured output was returned"
    errors = _errors(schema_for(role), data, "")
    if not errors:
        return None
    return "; ".join(errors[:6])


def _errors(schema: Mapping[str, Any], value: Any, path: str) -> list[str]:
    where = path or "the response"
    found: list[str] = []

    expected = schema.get("type")
    if expected is not None and not _is_type(value, expected):
        return [f"{where} must be {expected}, got {_name(value)}"]

    if "enum" in schema:
        allowed = schema["enum"]
        if not any(value == option and _same_kind(value, option) for option in allowed):
            return [f"{where} must be one of {allowed!r}, got {value!r}"]

    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum, maximum = schema.get("minimum"), schema.get("maximum")
        if minimum is not None and value < minimum:
            found.append(f"{where} must be at least {minimum}, got {value}")
        if maximum is not None and value > maximum:
            found.append(f"{where} must be at most {maximum}, got {value}")

    if isinstance(value, str):
        limit = schema.get("maxLength")
        if limit is not None and len(value) > limit:
            found.append(f"{where} is {len(value)} characters, the limit is {limit}")
        floor = schema.get("minLength")
        if floor is not None:
            stripped = value.strip()
            if len(stripped) < floor:
                found.append(
                    f"{where} is {len(stripped)} characters of prose; at least {floor} "
                    "are needed — write the critique, do not leave the field empty"
                )
            elif _is_json_literal(stripped):
                # A `minLength` marks a field as prose, and prose is not JSON. The owner's
                # run wrote `{"summary": "…"}` and `{}` into fields the schema declared as
                # strings, and both are strings, so nothing objected.
                found.append(
                    f"{where} must be plain prose, not JSON — write the sentence itself"
                )

    if isinstance(value, Mapping):
        smallest = schema.get("minProperties")
        if smallest is not None and len(value) < smallest:
            found.append(f"{where} needs at least {smallest} entr(ies), got {len(value)}")
        for name in schema.get("required", ()):
            if name not in value:
                found.append(f"{where} is missing required field {name!r}")
        properties = schema.get("properties") or {}
        for name, sub_schema in properties.items():
            if name in value:
                found += _errors(sub_schema, value[name], _join(path, name))
        extra = schema.get("additionalProperties")
        if isinstance(extra, Mapping):
            for name, item in value.items():
                if name not in properties:
                    found += _errors(extra, item, _join(path, name))

    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        minimum, maximum = schema.get("minItems"), schema.get("maxItems")
        if minimum is not None and len(value) < minimum:
            found.append(f"{where} needs at least {minimum} item(s), got {len(value)}")
        if maximum is not None and len(value) > maximum:
            found.append(f"{where} allows at most {maximum} item(s), got {len(value)}")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for index, item in enumerate(value):
                found += _errors(item_schema, item, f"{path}[{index}]" if path else f"item {index}")

    return found


def _is_json_literal(text: str) -> bool:
    """Whether a string is really an encoded object or array rather than a sentence."""
    if not text.startswith(("{", "[")):
        return False
    try:
        json.loads(text)
    except ValueError:
        return False
    return True


def unwrap_prose(value: Any) -> str:
    """Recover readable text from a field the model JSON-encoded anyway.

    The repair re-ask catches this going forward, but a review already stored as
    `{"summary": "No fundamental flaw; …"}` is text nobody can read, in the field a
    scientist reads first. One key means one sentence, so the sentence is taken out;
    anything else is left exactly as it arrived rather than guessed at.
    """
    if not isinstance(value, str):
        return "" if value is None else str(value)
    text = value.strip()
    if not text.startswith(("{", "[", '"')):
        return text
    try:
        decoded = json.loads(text)
    except ValueError:
        return text
    if isinstance(decoded, str):
        return decoded.strip()
    if isinstance(decoded, Mapping) and len(decoded) == 1:
        only = next(iter(decoded.values()))
        return str(only).strip() if only is not None else ""
    if isinstance(decoded, Mapping) and not decoded:
        return ""
    return text


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


def _is_type(value: Any, expected: str) -> bool:
    match expected:
        case "object":
            return isinstance(value, Mapping)
        case "array":
            return isinstance(value, Sequence) and not isinstance(value, str | bytes)
        case "string":
            return isinstance(value, str)
        case "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        case "number":
            return isinstance(value, int | float) and not isinstance(value, bool)
        case "boolean":
            return isinstance(value, bool)
    return True


def _same_kind(value: Any, option: Any) -> bool:
    """`True == 1` in Python; an enum of [1, 2] must not accept a boolean."""
    return isinstance(value, bool) == isinstance(option, bool)


def _name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, Mapping):
        return "object"
    if isinstance(value, str):
        return "string"
    if isinstance(value, Sequence):
        return "array"
    return type(value).__name__
