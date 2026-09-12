"""engine schema (Revision B, destructive)

The other half of Revision A. Revision A added the engine's tables beside the legacy ones;
this one moves the data worth keeping out of the legacy schema, drops it, and renames
`runs2` to `runs` now that the name is free.

**Step A runs before anything is dropped.** `prompt_workshops` (11 rows) and
`prompt_options` (14) hold the owner's real prompt-design work and exist in no archive —
unlike the runs, which can be re-imported from `archive/imported-runs/` at any time. They
are migrated into `workshops` / `workshop_options` keeping their ids, so a row can still be
traced to the one it came from.

Field mapping, workshops:

    intent          → question
    harness         → harness      (unchanged; 'codex' rows included)
    status          → state        (below)
    created_at      → created_at
    updated_at      → updated_at
    final_prompt    → the CHOSEN option's `prompt` (see below)
    selected_option → the chosen option's `chosen` flag

`status` → `state`, mapping the five values the legacy CHECK constraint allowed onto the
four of plan C5. Counts are what the live table held when this was written:

    draft         → refining        (0 rows — never reached the model)
    refining      → refining        (0 rows)
    options_ready → options_ready   (4 rows)
    prompt_ready  → chosen          (3 rows)
    failed        → failed          (4 rows)

Field mapping, options:

    label                → strategy ("Option A"/"Option B"), and ordinal (A=0, B=1)
    prompt               → prompt, EXCEPT on the chosen option — see below
    scoring_criteria     → optimizes_for / excludes / rationale
    recommended_settings → recommended_settings, re-keyed
    created_at           → created_at

**final_prompt.** The new schema has no `final_prompt` column, by design: `choose()` writes
the scientist's edited text over the chosen option's `prompt`, so the chosen option's prompt
IS the final prompt. This migration does the same thing to the historical rows. In all three
live `prompt_ready` rows the two strings are already identical, so nothing is overwritten in
practice — but the rule is applied rather than assumed, because a workshop where they differ
is exactly the case where the edit is the thing worth keeping.

**scoring_criteria** occurs in two shapes. The later one — `{primary, secondary, rejection}`
— maps cleanly onto `optimizes_for` / `rationale` / `excludes`. The earlier one used
free-form criterion names (`novelty`, `revenue`, `why_now`), which have no counterpart, so
the whole set is rendered into `optimizes_for` as prose rather than discarded.

**recommended_settings** is re-keyed to C5's frozen four fields: legacy `budget` becomes
`budget_calls`, and `top_k` / `grounding_mode` are dropped because the shape the launch
wizard prefills from has no room for them. Missing values take the Standard preset's
defaults (3 rounds / 60 calls / 6 matches / standard), never zeros.

**Step B** drops `processes`, `process_events`, `artifact_index`, `run_settings`,
`prompt_workshops`, `prompt_options`, `comparison_groups`, `comparison_members` and the
legacy `runs` table, then renames `runs2` → `runs` along with its indexes and constraints —
a table called `runs` carrying indexes called `ux_runs2_*` is a trap for the next reader.
Foreign keys pointing at `runs2` follow the rename automatically; their own names never
mentioned it.

The 20 rows in legacy `runs` are 15 smoke-test leftovers plus the 5 runs that matter, and
all 5 were imported into `runs2` by Wave 3B.2 before this ran — verified at 15 rows in
`runs2`, every one of them re-importable from the archive.

**Downgrade restores the schema, not the data.** Dropped rows come back from
`archive/db-pre-revision-b.dump`, which is what it is for; what `downgrade()` guarantees is
that Alembic is left somewhere it can move from, in either direction.

Revision ID: 20260801_0004
Revises: 20260801_0003
Create Date: 2026-08-01
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op

revision: str = "20260801_0004"
down_revision: str | Sequence[str] | None = "20260801_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EXPECTED_DATABASE = "ai_coscientist_gui"

STATE_BY_LEGACY_STATUS = {
    "draft": "refining",
    "refining": "refining",
    "options_ready": "options_ready",
    "prompt_ready": "chosen",
    "failed": "failed",
}

# What the Standard preset recommends. Used for options that recorded nothing, so the
# wizard prefills something workable instead of a run of zero rounds.
DEFAULT_RECOMMENDED = {
    "rounds": 3,
    "budget_calls": 60,
    "matches_per_round": 6,
    "grounding_depth": "standard",
}

LEGACY_FAILURE = {
    "code": "legacy_failure",
    "message": (
        "This workshop failed before the overhaul. The engine of the time did not record "
        "why."
    ),
}

# Children first: every one of these is referenced by the table below it.
DROP_ORDER = (
    "process_events",
    "processes",
    "artifact_index",
    "run_settings",
    "comparison_members",
    "comparison_groups",
    "prompt_options",
    "prompt_workshops",
    "runs",
)

RENAMED_CONSTRAINTS = (
    ("runs2_pkey", "runs_pkey"),
    ("ck_runs2_source", "ck_runs_source"),
    ("ck_runs2_harness", "ck_runs_harness"),
    ("ck_runs2_lifecycle", "ck_runs_lifecycle"),
)

RENAMED_INDEXES = (
    ("ux_runs2_engine_run_id_live", "ux_runs_engine_run_id_live"),
    ("ux_runs2_active_lane_per_harness", "ux_runs_active_lane_per_harness"),
    ("ix_runs2_deleted_lifecycle_updated", "ix_runs_deleted_lifecycle_updated"),
)


def _assert_database_identity() -> None:
    """Refuse to touch anything but the app's own database.

    This server also hosts the owner's production databases, and this revision drops nine
    tables. A downgrade cannot undo it being pointed at the wrong DSN.
    """
    database = op.get_bind().execute(sa.text("select current_database()")).scalar_one()
    if database != EXPECTED_DATABASE:
        raise RuntimeError(
            f"Refusing to migrate {database!r}: this revision only runs against "
            f"{EXPECTED_DATABASE!r}. Check DATABASE_URL."
        )


# --- Step A: the data ---------------------------------------------------------------------


def _criteria_fields(criteria: Any) -> tuple[str | None, str | None, str | None]:
    """`scoring_criteria` → (optimizes_for, excludes, rationale)."""
    if not isinstance(criteria, dict) or not criteria:
        return None, None, None

    primary = criteria.get("primary")
    secondary = criteria.get("secondary")
    rejection = criteria.get("rejection")
    if primary or secondary or rejection:
        return primary or None, rejection or None, secondary or None

    # The earlier vocabulary: criterion names chosen per workshop. Nothing here is
    # "what it excludes" or "why", so it all becomes what the option optimizes for.
    rendered = "; ".join(f"{key}: {value}" for key, value in sorted(criteria.items()))
    return rendered or None, None, None


def _recommended_settings(legacy: Any) -> dict[str, Any]:
    """Legacy settings → C5's frozen four fields."""
    source = legacy if isinstance(legacy, dict) else {}
    budget = source.get("budget_calls") or source.get("budget")
    return {
        "rounds": _positive_int(source.get("rounds"), DEFAULT_RECOMMENDED["rounds"]),
        "budget_calls": _positive_int(budget, DEFAULT_RECOMMENDED["budget_calls"]),
        "matches_per_round": _positive_int(
            source.get("matches_per_round"), DEFAULT_RECOMMENDED["matches_per_round"]
        ),
        "grounding_depth": str(
            source.get("grounding_depth") or DEFAULT_RECOMMENDED["grounding_depth"]
        ),
    }


def _positive_int(value: Any, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return fallback
    return parsed if parsed > 0 else fallback


def _ordinal(label: str, position: int) -> int:
    """A=0, B=1. Anything else keeps the order it was stored in."""
    if len(label) == 1 and "A" <= label <= "Z":
        return ord(label) - ord("A")
    return position


def _migrate_workshops(bind: sa.Connection) -> tuple[int, int]:
    workshops = bind.execute(
        sa.text(
            "select id, harness, intent, status, selected_option, final_prompt, "
            "created_at, updated_at from prompt_workshops order by created_at, id"
        )
    ).mappings().all()

    for row in workshops:
        state = STATE_BY_LEGACY_STATUS.get(row["status"], "failed")
        bind.execute(
            sa.text(
                """
                insert into workshops
                    (id, question, state, harness, context_docs, error, created_at, updated_at)
                values (:id, :question, :state, :harness, '[]'::jsonb,
                        cast(:error as jsonb), :created_at, :updated_at)
                """
            ),
            {
                "id": row["id"],
                "question": row["intent"],
                "state": state,
                "harness": row["harness"],
                # A failed workshop with a null error renders as a failure with no reason.
                "error": json.dumps(LEGACY_FAILURE) if state == "failed" else None,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
        )

    options = bind.execute(
        sa.text(
            "select id, workshop_id, label, prompt, scoring_criteria, recommended_settings, "
            "created_at from prompt_options order by workshop_id, label, created_at"
        )
    ).mappings().all()

    final_prompts = {
        row["id"]: (row["selected_option"], row["final_prompt"]) for row in workshops
    }
    seen_per_workshop: dict[Any, int] = {}
    for row in options:
        position = seen_per_workshop.get(row["workshop_id"], 0)
        seen_per_workshop[row["workshop_id"]] = position + 1

        selected_label, final_prompt = final_prompts.get(row["workshop_id"], (None, None))
        chosen = selected_label is not None and row["label"] == selected_label
        optimizes_for, excludes, rationale = _criteria_fields(row["scoring_criteria"])

        bind.execute(
            sa.text(
                """
                insert into workshop_options
                    (id, workshop_id, ordinal, prompt, strategy, optimizes_for, excludes,
                     rationale, recommended_settings, chosen, rejected, note, created_at)
                values (:id, :workshop_id, :ordinal, :prompt, :strategy, :optimizes_for,
                        :excludes, :rationale, cast(:recommended as jsonb), :chosen, false,
                        null, :created_at)
                """
            ),
            {
                "id": row["id"],
                "workshop_id": row["workshop_id"],
                "ordinal": _ordinal(row["label"], position),
                # The chosen option carries the scientist's edited final prompt, which is
                # where `choose()` puts it and where the API reads it back from.
                "prompt": final_prompt if (chosen and final_prompt) else row["prompt"],
                "strategy": f"Option {row['label']}",
                "optimizes_for": optimizes_for,
                "excludes": excludes,
                "rationale": rationale,
                "recommended": json.dumps(_recommended_settings(row["recommended_settings"])),
                "chosen": chosen,
                "created_at": row["created_at"],
            },
        )

    return len(workshops), len(options)


# --- Step B: the schema -------------------------------------------------------------------


def upgrade() -> None:
    _assert_database_identity()

    bind = op.get_bind()
    workshops, options = _migrate_workshops(bind)
    print(f"Revision B: migrated {workshops} workshop(s) and {options} option(s)")

    for table in DROP_ORDER:
        op.drop_table(table)

    op.rename_table("runs2", "runs")
    for old, new in RENAMED_CONSTRAINTS:
        op.execute(f'ALTER TABLE runs RENAME CONSTRAINT "{old}" TO "{new}"')
    for old, new in RENAMED_INDEXES:
        op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')


def downgrade() -> None:
    _assert_database_identity()

    for old, new in RENAMED_INDEXES:
        op.execute(f'ALTER INDEX "{new}" RENAME TO "{old}"')
    for old, new in RENAMED_CONSTRAINTS:
        op.execute(f'ALTER TABLE runs RENAME CONSTRAINT "{new}" TO "{old}"')
    op.rename_table("runs", "runs2")

    for statement in LEGACY_SCHEMA_DDL:
        op.execute(statement)


# The legacy schema as revisions 0001 and 0002 left it, so a downgrade lands on something
# those revisions can themselves undo. Empty: the rows are in the pre-migration dump.
LEGACY_SCHEMA_DDL: tuple[str, ...] = (
    """
    CREATE TABLE runs (
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        engine_run_id text NOT NULL,
        harness text NOT NULL,
        version text NOT NULL,
        lifecycle text NOT NULL,
        title text NOT NULL,
        goal text,
        final_prompt text,
        root_path text NOT NULL,
        run_path text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT runs_pkey PRIMARY KEY (id),
        CONSTRAINT uq_runs_version_engine_run_id UNIQUE (version, engine_run_id),
        CONSTRAINT ck_runs_harness CHECK (harness IN ('claude', 'codex')),
        CONSTRAINT ck_runs_version CHECK (version IN ('v1', 'v2')),
        CONSTRAINT ck_runs_lifecycle CHECK (lifecycle IN ('draft', 'refining', 'prompt_ready',
            'queued', 'running', 'pause_requested', 'paused', 'stopping', 'stopped',
            'finishing', 'completed', 'failed', 'abandoned', 'imported', 'unknown'))
    )
    """,
    "CREATE INDEX ix_runs_engine_identity ON runs (version, engine_run_id)",
    "CREATE INDEX ix_runs_lifecycle_updated ON runs (lifecycle, updated_at)",
    """
    CREATE TABLE prompt_workshops (
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        harness text NOT NULL,
        intent text NOT NULL,
        status text NOT NULL,
        selected_option text,
        final_prompt text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT prompt_workshops_pkey PRIMARY KEY (id),
        CONSTRAINT ck_prompt_workshops_harness CHECK (harness IN ('claude', 'codex')),
        CONSTRAINT ck_prompt_workshops_status CHECK (status IN ('draft', 'refining',
            'options_ready', 'prompt_ready', 'failed'))
    )
    """,
    """
    CREATE TABLE comparison_groups (
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        name text NOT NULL,
        base_prompt text,
        notes text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT comparison_groups_pkey PRIMARY KEY (id)
    )
    """,
    """
    CREATE TABLE prompt_options (
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        workshop_id uuid NOT NULL,
        label text NOT NULL,
        prompt text NOT NULL,
        scoring_criteria jsonb NOT NULL DEFAULT '{}'::jsonb,
        recommended_settings jsonb NOT NULL DEFAULT '{}'::jsonb,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT prompt_options_pkey PRIMARY KEY (id),
        CONSTRAINT uq_prompt_options_workshop_label UNIQUE (workshop_id, label),
        CONSTRAINT prompt_options_workshop_id_fkey FOREIGN KEY (workshop_id)
            REFERENCES prompt_workshops (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE run_settings (
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        run_id uuid NOT NULL,
        grounding_mode text NOT NULL,
        grounding_depth text NOT NULL,
        allow_fallback boolean NOT NULL DEFAULT false,
        revision integer NOT NULL DEFAULT 1,
        is_current boolean NOT NULL DEFAULT true,
        rounds integer,
        budget integer,
        matches integer,
        top_k integer,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT run_settings_pkey PRIMARY KEY (id),
        CONSTRAINT uq_run_settings_run_revision UNIQUE (run_id, revision),
        CONSTRAINT ck_run_settings_grounding_mode
            CHECK (grounding_mode IN ('built_in_web', 'perplexity')),
        CONSTRAINT ck_run_settings_grounding_depth
            CHECK (grounding_depth IN ('shallow', 'standard', 'deep')),
        CONSTRAINT ck_run_settings_revision_positive CHECK (revision > 0),
        CONSTRAINT ck_run_settings_rounds_positive CHECK (rounds IS NULL OR rounds > 0),
        CONSTRAINT ck_run_settings_budget_positive CHECK (budget IS NULL OR budget > 0),
        CONSTRAINT ck_run_settings_matches_positive CHECK (matches IS NULL OR matches > 0),
        CONSTRAINT ck_run_settings_top_k_positive CHECK (top_k IS NULL OR top_k > 0),
        CONSTRAINT run_settings_run_id_fkey FOREIGN KEY (run_id)
            REFERENCES runs (id) ON DELETE CASCADE
    )
    """,
    """
    CREATE UNIQUE INDEX ux_run_settings_one_current_per_run
        ON run_settings (run_id) WHERE is_current
    """,
    """
    CREATE TABLE processes (
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        run_id uuid,
        workshop_id uuid,
        harness text NOT NULL,
        mode text NOT NULL,
        pid integer,
        state text NOT NULL,
        command text NOT NULL,
        cwd text NOT NULL,
        started_at timestamptz,
        ended_at timestamptz,
        exit_code integer,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT processes_pkey PRIMARY KEY (id),
        CONSTRAINT ck_processes_harness CHECK (harness IN ('claude', 'codex')),
        CONSTRAINT ck_processes_mode
            CHECK (mode IN ('prompt_workshop', 'co_scientist_batch', 'finish')),
        CONSTRAINT ck_processes_state CHECK (state IN ('starting', 'running',
            'pause_requested', 'paused', 'stopping', 'stopped', 'finishing', 'completed',
            'failed', 'lost', 'killed')),
        CONSTRAINT ck_processes_has_owner
            CHECK ((run_id IS NOT NULL) OR (workshop_id IS NOT NULL)),
        CONSTRAINT processes_run_id_fkey FOREIGN KEY (run_id)
            REFERENCES runs (id) ON DELETE CASCADE,
        CONSTRAINT processes_workshop_id_fkey FOREIGN KEY (workshop_id)
            REFERENCES prompt_workshops (id) ON DELETE CASCADE
    )
    """,
    # Revision 0002's form of this index, with 'finishing' in the predicate: a downgrade
    # from here lands on 0003, which is after 0002, not before it.
    """
    CREATE UNIQUE INDEX ux_one_active_process_per_harness ON processes (harness)
        WHERE state IN ('starting', 'running', 'pause_requested', 'stopping', 'finishing')
    """,
    """
    CREATE TABLE artifact_index (
        id uuid NOT NULL DEFAULT gen_random_uuid(),
        run_id uuid NOT NULL,
        path text NOT NULL,
        kind text NOT NULL,
        size bigint,
        mtime timestamptz,
        content_hash text,
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT artifact_index_pkey PRIMARY KEY (id),
        CONSTRAINT uq_artifact_index_run_path UNIQUE (run_id, path),
        CONSTRAINT ck_artifact_index_kind CHECK (kind IN ('state_json', 'research_overview',
            'ideas_ranked_csv', 'hypothesis_markdown', 'other')),
        CONSTRAINT ck_artifact_index_size_nonnegative CHECK (size IS NULL OR size >= 0),
        CONSTRAINT artifact_index_run_id_fkey FOREIGN KEY (run_id)
            REFERENCES runs (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_artifacts_run_kind ON artifact_index (run_id, kind)",
    """
    CREATE TABLE process_events (
        id bigserial NOT NULL,
        process_id uuid NOT NULL,
        ts timestamptz NOT NULL DEFAULT now(),
        stream text NOT NULL,
        event_type text NOT NULL,
        payload jsonb NOT NULL DEFAULT '{}'::jsonb,
        CONSTRAINT process_events_pkey PRIMARY KEY (id),
        CONSTRAINT ck_process_events_stream CHECK (stream IN ('stdout', 'stderr', 'system')),
        CONSTRAINT ck_process_events_event_type CHECK (event_type IN ('status',
            'process_event', 'artifact_changed', 'error', 'control')),
        CONSTRAINT process_events_process_id_fkey FOREIGN KEY (process_id)
            REFERENCES processes (id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_process_events_process_id_id ON process_events (process_id, id)",
    """
    CREATE TABLE comparison_members (
        group_id uuid NOT NULL,
        run_id uuid NOT NULL,
        role text,
        created_at timestamptz NOT NULL DEFAULT now(),
        CONSTRAINT comparison_members_pkey PRIMARY KEY (group_id, run_id),
        CONSTRAINT comparison_members_group_id_fkey FOREIGN KEY (group_id)
            REFERENCES comparison_groups (id) ON DELETE CASCADE,
        CONSTRAINT comparison_members_run_id_fkey FOREIGN KEY (run_id)
            REFERENCES runs (id) ON DELETE CASCADE
    )
    """,
)
