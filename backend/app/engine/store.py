"""Transactional access to engine state.

`RunStore` is the only thing that writes the engine's tables. Every public method runs in
exactly one transaction and returns plain JSON-safe dicts — never ORM instances — so
callers cannot accidentally hold a detached object across a transaction boundary or hand
a lazy relationship to a serialiser.

Two disciplines this module exists to enforce:

**`emit` is the single writer of `run_events`.** `seq` is allocated at INSERT time, but a
subscriber resuming from "everything after seq N" only sees a consistent stream if commit
order matches seq order. Two concurrent writers can allocate 5 then 6 and commit 6 then 5,
and a subscriber that read up to 6 in that window never sees 5 again. The orchestrator
therefore runs role calls in parallel but returns their results to the main coroutine,
which records and emits them serially. Out-of-band writers (the controls API, the
reconciler) must only emit when no supervisor is alive for that run.

**Interventions are queued, not applied by the caller.** `add_note` appends to
`pending_interventions`; the orchestrator drains it at the round boundary with
`claim_interventions` and emits `note_added` from its own single-writer position.

The engine's canonical event types and their payload shapes live in `engine/events.py`;
this module stores whatever type string it is given.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.engine_models import (
    LANE_LIFECYCLES,
    LIFECYCLES,
    BudgetLedger,
    GraftEvent,
    Hypothesis,
    Match,
    Review,
    Run,
    RunContextDoc,
    RunEvent,
    User,
)
from app.db.session import get_session_factory
from app.engine.run_metadata import elapsed_seconds, model_level

# `run_events.payload` is capped here rather than in DDL: a check constraint would abort
# the whole transaction over a verbose debate text, and losing the event is worse than
# losing its tail.
MAX_PAYLOAD_BYTES = 8192

CONTROL_ACTIONS: tuple[str, ...] = ("pause", "resume", "stop", "finish", "force_stop")

# Lifecycles in which a supervisor may still be working. Wider than LANE_LIFECYCLES:
# a paused run releases its harness lane but is still the scientist's live run.
LIVE_LIFECYCLES: frozenset[str] = frozenset(LANE_LIFECYCLES) | {"paused"}

TERMINAL_LIFECYCLES: frozenset[str] = frozenset({"completed", "stopped", "failed", "lost"})
"""Lifecycles from which no supervisor will ever move the run again.

Lives here rather than in the orchestrator because the read path needs it too: a round
summary cannot say "running" about a run that ended last week, and the orchestrator is not
imported by anything that reads."""

TITLE_MAX = 60


class Unchanged:
    """The third state of an optional argument: "leave this exactly as it is".

    `None` cannot carry that meaning for a ceiling, because `None` is a ceiling in its own
    right — "no ceiling at all" — and since dollars stopped governing runs it is the answer
    a caller most often wants. A parameter that must tell "raise it to 12" from "remove it"
    from "do not touch it" needs three values, and this is the third.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover — a debugging aid, not behaviour
        return "UNCHANGED"


UNCHANGED = Unchanged()
"""The singleton `Unchanged`. Compare with `isinstance`, never with `==`."""


def usd_ceiling(value: Decimal | float | None) -> float | None:
    """A stored dollar ceiling as everything above the database reads it.

    `runs.budget_usd` is NOT NULL, so a run without a ceiling stores zero — but zero is not
    a ceiling of nothing, it is the absence of one, and an audit record that spells the two
    the same way says a run was capped at $0.00 when it was never capped at all. Anything
    that leaves this module answers `None`.
    """
    number = _float(value)
    return number or None


class RunNotFound(LookupError):
    """No run row with that id (it may have been hard-deleted)."""


class LifecycleConflict(RuntimeError):
    """The run's lifecycle moved out from under a transition that expected to find it.

    Raised by the write paths that read, decide and write in one transaction — `extend_run`
    is the only one so far. The caller checked the lifecycle before it asked; this says the
    row had moved by the time the lock was taken, which is the race that check cannot close
    on its own.
    """

    def __init__(self, run_id: UUID, lifecycle: str, expected: Sequence[str]) -> None:
        self.run_id = run_id
        self.lifecycle = lifecycle
        self.expected = tuple(expected)
        super().__init__(
            f"Run {run_id} is {lifecycle}, not one of {', '.join(self.expected) or '(none)'}."
        )


class BudgetExhausted(RuntimeError):
    """A run reached one of its ceilings.

    Raised by `spend` *after* the call it was given has been recorded — the tokens were
    already burned, so the ledger tells the truth and the exception stops the next step —
    and by `check_budget` before a step that could not be afforded.

    In practice this now means the *call* ceiling. The dollar ceiling is optional and unset
    by default; the path below still enforces one when a run carries it, for whoever points
    this engine at a metered API key.
    """

    def __init__(
        self,
        run_id: UUID,
        reason: str,
        *,
        calls_used: int,
        budget_calls: int,
        spend_usd: float,
        budget_usd: float,
    ) -> None:
        self.run_id = run_id
        self.reason = reason
        self.calls_used = calls_used
        self.budget_calls = budget_calls
        self.spend_usd = spend_usd
        self.budget_usd = budget_usd
        if reason == "calls":
            detail = f"{calls_used}/{budget_calls} calls"
        else:
            detail = f"${spend_usd:.4f}/${budget_usd:.2f}"
        super().__init__(f"Run {run_id} exhausted its {reason} budget ({detail}).")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decimal(value: float | Decimal | int | None) -> Decimal | None:
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _float(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def _json_safe(value: Any) -> Any:
    """Round-trip through JSON so the value is guaranteed insertable into jsonb."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _encoded_size(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))


def _shrink_longest_string(payload: dict[str, Any]) -> bool:
    strings = [(len(v), k) for k, v in payload.items() if isinstance(v, str)]
    if not strings:
        return False
    length, key = max(strings)
    if length <= 64:
        return False
    payload[key] = payload[key][: max(64, length // 2)] + "… [truncated]"
    return True


def cap_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fit a payload under `MAX_PAYLOAD_BYTES`, flagging it when anything was cut."""
    safe = _json_safe(dict(payload or {}))
    if _encoded_size(safe) <= MAX_PAYLOAD_BYTES:
        return safe

    safe["truncated"] = True
    while _encoded_size(safe) > MAX_PAYLOAD_BYTES and _shrink_longest_string(safe):
        pass
    if _encoded_size(safe) > MAX_PAYLOAD_BYTES:
        return {"truncated": True, "dropped": True, "keys": sorted(str(k) for k in safe)}
    return safe


def derive_title(question: str) -> str:
    text = " ".join(question.split())
    if len(text) <= TITLE_MAX:
        return text or "Untitled run"
    return text[: TITLE_MAX - 1].rstrip() + "…"


def new_engine_run_id() -> str:
    return f"{datetime.now(UTC):run-%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"


class RunStore:
    """One transaction per method. Construct once and share; it holds no run state."""

    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session_factory(settings)

    @contextmanager
    def _tx(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _run(session: Session, run_id: UUID, *, lock: bool = False) -> Run:
        query = select(Run).where(Run.id == run_id)
        if lock:
            query = query.with_for_update()
        run = session.execute(query).scalar_one_or_none()
        if run is None:
            raise RunNotFound(f"No run {run_id}")
        return run

    # --- creation ---------------------------------------------------------------------

    def create_run(
        self,
        *,
        question: str,
        prompt: str,
        config: Mapping[str, Any] | None = None,
        harness: str = "claude",
        title: str | None = None,
        source: str = "app",
        source_version: str | None = None,
        engine_run_id: str | None = None,
        base_prompt_hash: str | None = None,
        root_path: str = "",
        lifecycle: str = "queued",
        rounds_target: int | None = None,
        budget_calls: int | None = None,
        budget_usd: float | Decimal | None = None,
        context_docs: Sequence[Mapping[str, str]] | None = None,
        engine_state: Mapping[str, Any] | None = None,
        owner_id: UUID | None = None,
    ) -> UUID:
        """Insert a run (and its context documents) and return its id.

        Raises `sqlalchemy.exc.IntegrityError` when the harness lane is already held by an
        active run — the launcher turns that into the 409 the API promises, reading the
        conflicting run id back out of the index.
        """
        config = dict(config or {})
        if lifecycle not in LIFECYCLES:
            raise ValueError(f"Unknown lifecycle {lifecycle!r}")

        rounds_target = rounds_target if rounds_target is not None else config.get("rounds", 0)
        budget_calls = budget_calls if budget_calls is not None else config.get("budget_calls", 0)
        budget_usd = budget_usd if budget_usd is not None else config.get("budget_usd", 0)

        # The call ceiling is mandatory for a run this app will actually execute: it is the
        # governor that stops a runaway loop. The dollar ceiling is optional and off by
        # default — these are headless CLI calls on a subscription, so a dollar figure is
        # API-equivalent telemetry rather than money, and the only thing enforcing one
        # reliably did was stop a run one step short of the report it had already produced.
        # A stored 0 means "no ceiling", which is how imported runs have always been stored.
        if source == "app":
            if not budget_calls or int(budget_calls) <= 0:
                raise ValueError("budget_calls must be greater than 0 for an app run")
            if budget_usd is not None and Decimal(str(budget_usd)) < 0:
                raise ValueError("budget_usd cannot be negative")

        with self._tx() as session:
            run = Run(
                owner_id=owner_id,
                engine_run_id=engine_run_id or new_engine_run_id(),
                source=source,
                source_version=source_version,
                title=title or derive_title(question),
                question=question,
                prompt=prompt,
                base_prompt_hash=base_prompt_hash,
                harness=harness,
                lifecycle=lifecycle,
                rounds_target=int(rounds_target or 0),
                budget_calls=int(budget_calls or 0),
                budget_usd=_decimal(budget_usd) or Decimal(0),
                config=_json_safe(config),
                engine_state=_json_safe(dict(engine_state or {})),
                root_path=root_path,
            )
            session.add(run)
            session.flush()
            for doc in context_docs or []:
                content = doc.get("text", doc.get("content", ""))
                session.add(
                    RunContextDoc(
                        run_id=run.id,
                        name=doc.get("name", "document"),
                        chars=len(content),
                        content=content,
                    )
                )
            return run.id

    # --- hypotheses -------------------------------------------------------------------

    def add_hypothesis(
        self,
        run_id: UUID,
        *,
        title: str,
        body_md: str,
        created_round: int,
        status: str = "active",
        elo: float = 1200.0,
        cluster: str | None = None,
        parent_ids: Sequence[str] | None = None,
        operator: str | None = None,
        seed_id: str | None = None,
        source: str = "agent",
        identity: UUID | None = None,
    ) -> dict[str, Any]:
        """Insert a hypothesis, assigning the next `hNNN` id within the run."""
        with self._tx() as session:
            self._run(session, run_id, lock=True)  # serialise hid assignment
            if identity is not None:
                existing = session.get(Hypothesis, identity)
                if existing is not None:
                    if existing.run_id != run_id:
                        raise ValueError("hypothesis identity belongs to another run")
                    return _hypothesis_dict(existing)
            last = session.execute(
                select(func.max(Hypothesis.hid)).where(Hypothesis.run_id == run_id)
            ).scalar_one_or_none()
            index = int(last[1:]) + 1 if last else 1
            hypothesis = Hypothesis(
                **({"id": identity} if identity is not None else {}),
                run_id=run_id,
                hid=f"h{index:03d}",
                title=title,
                body_md=body_md,
                status=status,
                elo=elo,
                cluster=cluster,
                parent_ids=list(parent_ids or []),
                operator=operator,
                seed_id=seed_id,
                created_round=created_round,
                source=source,
            )
            session.add(hypothesis)
            session.flush()
            return _hypothesis_dict(hypothesis)

    def list_hypotheses(
        self, run_id: UUID, *, statuses: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        query = select(Hypothesis).where(Hypothesis.run_id == run_id).order_by(Hypothesis.hid)
        if statuses:
            query = query.where(Hypothesis.status.in_(tuple(statuses)))
        with self._tx() as session:
            return [_hypothesis_dict(row) for row in session.execute(query).scalars()]

    def list_hypotheses_without_review(self, run_id: UUID) -> list[dict[str, Any]]:
        """Reflection's resume unit: hypotheses nobody has reviewed yet."""
        reviewed = select(Review.hypothesis_id).where(Review.hypothesis_id == Hypothesis.id)
        query = (
            select(Hypothesis)
            .where(Hypothesis.run_id == run_id, ~reviewed.exists())
            .order_by(Hypothesis.hid)
        )
        with self._tx() as session:
            return [_hypothesis_dict(row) for row in session.execute(query).scalars()]

    def record_review(
        self,
        hypothesis_id: UUID,
        *,
        verdict: str,
        novelty_level: str | None = None,
        novelty_note: str | None = None,
        correctness: str | None = None,
        testability: str | None = None,
        key_risk: str | None = None,
        note: str | None = None,
        model: str | None = None,
        retire: bool = True,
    ) -> dict[str, Any]:
        """Store a review; a `reject` verdict retires the hypothesis in the same commit."""
        with self._tx() as session:
            hypothesis = session.get(Hypothesis, hypothesis_id)
            if hypothesis is None:
                raise RunNotFound(f"No hypothesis {hypothesis_id}")
            review = Review(
                hypothesis_id=hypothesis_id,
                verdict=verdict,
                novelty_level=novelty_level,
                novelty_note=novelty_note,
                correctness=correctness,
                testability=testability,
                key_risk=key_risk,
                note=note,
                model=model,
            )
            session.add(review)
            if retire and verdict == "reject" and hypothesis.status == "active":
                hypothesis.status = "rejected"
            session.flush()
            return {
                "id": str(review.id),
                "hypothesis_id": str(hypothesis_id),
                "hid": hypothesis.hid,
                "verdict": verdict,
                "novelty_level": novelty_level,
                "novelty_note": novelty_note,
                "correctness": correctness,
                "testability": testability,
                "key_risk": key_risk,
                "note": note,
                "model": model,
                "created_at": _iso(review.created_at),
            }

    def list_reviews(
        self, run_id: UUID, *, hids: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Reviews for a run, oldest first, each carrying the hypothesis `hid`.

        The orchestrator feeds these to the meta-review in full (all five fields, never
        truncated) and shows the latest one beside each hypothesis in a ranking prompt.
        Passing `hids=[]` returns nothing rather than everything — an empty selection is a
        selection.

        Prose fields are unwrapped on the way **out** as well as on the way in. The write
        side was fixed and nothing else was: rows already stored as JSON literals — run
        c4566ed2's h006 holds the literal two-character string `{}` in four of its five
        fields — were still served verbatim to the GUI under a human label, and would be
        recomposed into every future ranking prompt of a continued run, since h006 is
        active at 1184.8 with three matches. There is no backfill migration on purpose: the
        owner's stored data is not rewritten, it is read honestly.
        """
        if hids is not None and not hids:
            return []
        query = (
            select(Review, Hypothesis.hid, Hypothesis.created_round)
            .join(Hypothesis, Hypothesis.id == Review.hypothesis_id)
            .where(Hypothesis.run_id == run_id)
            .order_by(Review.created_at, Hypothesis.hid)
        )
        if hids is not None:
            query = query.where(Hypothesis.hid.in_(tuple(hids)))
        with self._tx() as session:
            return [
                {
                    "id": str(review.id),
                    "hypothesis_id": str(review.hypothesis_id),
                    "hid": hid,
                    "created_round": created_round,
                    "verdict": review.verdict,
                    "novelty_level": review.novelty_level,
                    "novelty_note": _readable(review.novelty_note),
                    "correctness": _readable(review.correctness),
                    "testability": _readable(review.testability),
                    "key_risk": _readable(review.key_risk),
                    "note": _readable(review.note),
                    "model": review.model,
                    "created_at": _iso(review.created_at),
                }
                for review, hid, created_round in session.execute(query)
            ]

    def apply_clusters(
        self,
        run_id: UUID,
        clusters: Mapping[str, str],
        duplicates: Mapping[str, str] | None = None,
    ) -> dict[str, int]:
        """Label hypotheses with their cluster and mark duplicates.

        Duplicates are archived rather than deleted and keep a `duplicate_of` pointer, so
        a mistaken proximity call can be undone without losing the hypothesis.

        The pointer is kept *current*, which it was not. It was written once and never
        re-aimed, so a survivor archived by a later round left every hypothesis that
        pointed at it naming a non-survivor — 13 such rows in run d282dda7 alone, and the
        idea graph renders that pointer to the scientist as "set aside, merged into h013"
        about a hypothesis that was itself set aside two rounds later. Two passes fix it:
        a champion is resolved through any chain to a row that is still standing, and every
        existing pointer at something being archived now is re-aimed at the same champion.
        """
        duplicates = dict(duplicates or {})
        with self._tx() as session:
            rows = {
                row.hid: row
                for row in session.execute(
                    select(Hypothesis).where(Hypothesis.run_id == run_id)
                ).scalars()
            }
            labelled = 0
            for hid, label in clusters.items():
                if hid in rows:
                    rows[hid].cluster = label
                    labelled += 1

            resolved = {
                hid: _survivor(original, rows, retiring=set(duplicates))
                for hid, original in duplicates.items()
            }
            archived = 0
            for hid, original in resolved.items():
                row = rows.get(hid)
                if row is None or hid == original:
                    continue
                row.duplicate_of = original
                if row.status == "active":
                    row.status = "archived"
                archived += 1

            for row in rows.values():
                # Anything already pointing at a hid this call is retiring now points past
                # it, to the champion that hid was merged into.
                if row.duplicate_of in resolved and row.hid != resolved[row.duplicate_of]:
                    row.duplicate_of = resolved[row.duplicate_of]
            return {"labelled": labelled, "duplicates": archived}

    def archive_hypothesis(self, run_id: UUID, hid: str) -> dict[str, Any]:
        """Archive a hypothesis on the scientist's instruction.

        The status change lands immediately (so it also works on a finished run, where no
        supervisor will ever drain a queue), and a live run additionally gets an
        intervention record so the orchestrator emits `hypothesis_archived` in sequence.

        Pointers are re-aimed the way `apply_clusters` re-aims them. Without that, a
        scientist setting aside a champion recreated exactly the state `apply_clusters`
        exists to prevent: rows still naming it as the survivor they were merged into, and
        the idea graph telling the reader "set aside, merged into hNNN" about a hypothesis
        that is itself set aside.
        """
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            rows = {
                row.hid: row
                for row in session.execute(
                    select(Hypothesis).where(Hypothesis.run_id == run_id)
                ).scalars()
            }
            hypothesis = rows.get(hid)
            if hypothesis is None:
                raise RunNotFound(f"No hypothesis {hid} in run {run_id}")
            hypothesis.status = "archived"
            champion = _survivor(hid, rows, retiring={hid})
            for row in rows.values():
                if row.duplicate_of == hid and row.hid != champion:
                    row.duplicate_of = champion if champion != hid else None
            if run.lifecycle in LIVE_LIFECYCLES:
                run.pending_interventions = [
                    *run.pending_interventions,
                    _intervention("hypothesis_archived", hid=hid),
                ]
            session.flush()
            return _hypothesis_dict(hypothesis)

    # --- matches ----------------------------------------------------------------------

    def plan_matches(
        self,
        run_id: UUID,
        round: int,
        pairs: Sequence[tuple[str, str]],
        *,
        extend: bool = False,
    ) -> list[dict[str, Any]]:
        """Persist the round's pair plan as `planned` rows before any match executes.

        Idempotent by round: if a plan already exists it is returned unchanged, so a resume
        replays the original pairings instead of re-deriving them from moved standings.

        `extend=True` adds to an existing plan instead of returning it untouched, which is
        how a round resumed after it generated more hypotheses gets those hypotheses into
        its tournament. Nothing already planned is touched, and a pair already present is
        not planned twice.
        """
        with self._tx() as session:
            self._run(session, run_id, lock=True)
            existing = list(
                session.execute(
                    select(Match)
                    .where(Match.run_id == run_id, Match.round == round)
                    .order_by(Match.ts, Match.id)
                ).scalars()
            )
            if existing and not extend:
                return [_match_dict(row) for row in existing]

            from app.engine.core import meeting_key

            # Only what the round already holds is excluded. The caller's own list is
            # planned as given — a round may legitimately schedule the same two hypotheses
            # twice, and it is not this method's business to overrule the pairing.
            seen = {meeting_key(row.hid_a, row.hid_b) for row in existing}
            created = []
            for hid_a, hid_b in pairs:
                if meeting_key(hid_a, hid_b) in seen:
                    continue
                match = Match(
                    run_id=run_id, round=round, hid_a=hid_a, hid_b=hid_b, status="planned"
                )
                session.add(match)
                created.append(match)
            session.flush()
            return [_match_dict(row) for row in (*existing, *created)]

    def list_matches(
        self, run_id: UUID, *, round: int | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = select(Match).where(Match.run_id == run_id).order_by(Match.round, Match.ts)
        if round is not None:
            query = query.where(Match.round == round)
        if status is not None:
            query = query.where(Match.status == status)
        with self._tx() as session:
            return [_match_dict(row) for row in session.execute(query).scalars()]

    def record_match(
        self,
        match_id: UUID,
        *,
        winner: int | None,
        debate_md: str | None = None,
        judge_model: str | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Settle a planned match: Elo for both sides, counters, and the debate text.

        `winner=None` records a skip (a contract violation the orchestrator gave up on)
        and moves no rating. Re-recording a settled match is refused, because a resume that
        replayed one would silently double-count its Elo.
        """
        from app.engine import core  # imported here: core is a peer module built in parallel

        with self._tx() as session:
            match = session.get(Match, match_id, with_for_update=True)
            if match is None:
                raise RunNotFound(f"No match {match_id}")
            if match.status != "planned":
                raise ValueError(f"Match {match_id} is already {match.status}")

            match.debate_md = debate_md
            match.judge_model = judge_model
            match.tokens_in = tokens_in
            match.tokens_out = tokens_out
            match.ts = datetime.now(UTC)

            if winner is None:
                match.status = status or "skipped"
                session.flush()
                return _match_dict(match)
            if winner not in (1, 2):
                raise ValueError(f"winner must be 1, 2 or None, got {winner!r}")

            hids = (match.hid_a, match.hid_b)
            rows = {
                row.hid: row
                for row in session.execute(
                    select(Hypothesis)
                    .where(Hypothesis.run_id == match.run_id, Hypothesis.hid.in_(hids))
                    .with_for_update()
                ).scalars()
            }
            side_a, side_b = rows.get(match.hid_a), rows.get(match.hid_b)
            if side_a is None or side_b is None:
                raise RunNotFound(f"Match {match_id} references a missing hypothesis")

            # The ranking schema speaks in 1/2 (and so does the matches table); core
            # speaks in 'a'/'b'.
            k = core.k_for(side_a.matches, side_b.matches)
            elo_a, elo_b = core.elo_update(
                side_a.elo, side_b.elo, "a" if winner == 1 else "b", k
            )

            match.status = status or "completed"
            match.winner = winner
            match.k = k
            match.elo_a_before, match.elo_a_after = side_a.elo, elo_a
            match.elo_b_before, match.elo_b_after = side_b.elo, elo_b

            side_a.elo, side_b.elo = elo_a, elo_b
            side_a.matches += 1
            side_b.matches += 1
            if winner == 1:
                side_a.wins += 1
            else:
                side_b.wins += 1
            session.flush()
            return _match_dict(match)

    # --- graft ------------------------------------------------------------------------

    def record_graft(
        self,
        run_id: UUID,
        round: int,
        *,
        fired: bool,
        n_clusters: int | None = None,
        hhi: float | None = None,
        signals: Mapping[str, Any] | None = None,
        votes: int | None = None,
        abstained_reason: str | None = None,
        source_domain: str | None = None,
        skeleton: str | None = None,
        seed_framing: str | None = None,
        seed_id: str | None = None,
    ) -> dict[str, Any]:
        """Record a collapse check. A firing one also parks its seed on the run."""
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            event = GraftEvent(
                run_id=run_id,
                round=round,
                n_clusters=n_clusters,
                hhi=hhi,
                signals=_json_safe(dict(signals or {})),
                votes=votes,
                fired=fired,
                abstained_reason=abstained_reason,
                source_domain=source_domain,
                skeleton=skeleton,
                seed_framing=seed_framing,
                seed_id=seed_id,
            )
            session.add(event)
            if fired:
                state = dict(run.graft_state)
                state["fired_count"] = int(state.get("fired_count", 0)) + 1
                state["last_fired_round"] = round
                state["pending_seed"] = {
                    "seed_id": seed_id,
                    "round": round,
                    "source_domain": source_domain,
                    "skeleton": skeleton,
                    "seed_framing": seed_framing,
                }
                run.graft_state = _json_safe(state)
            session.flush()
            return _graft_dict(event)

    def set_graft_state(self, run_id: UUID, patch: Mapping[str, Any]) -> dict[str, Any]:
        """Shallow-merge into `graft_state` — how the orchestrator clears a consumed seed."""
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            run.graft_state = _json_safe({**run.graft_state, **dict(patch)})
            return dict(run.graft_state)

    # --- budget -----------------------------------------------------------------------

    def spend(
        self,
        run_id: UUID,
        *,
        role: str,
        model: str | None = None,
        round: int | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cache_creation: int = 0,
        cache_read: int = 0,
        cost_usd: float | Decimal | None = None,
        duration_ms: int | None = None,
        status: str = "ok",
        calls: int = 1,
    ) -> dict[str, Any]:
        """Record one call against both ceilings.

        All four token classes are stored: input alone under-reports usage by orders of
        magnitude once prompt caching is in play. The ledger row is committed first and
        `BudgetExhausted` raised afterwards — the money is already spent, so the run's
        books must balance even as the loop stops.
        """
        cost = _decimal(cost_usd)
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            session.add(
                BudgetLedger(
                    run_id=run_id,
                    round=round,
                    role=role,
                    model=model,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cache_creation=cache_creation,
                    cache_read=cache_read,
                    cost_usd=cost,
                    duration_ms=duration_ms,
                    status=status,
                )
            )
            run.calls_used += calls
            run.tokens_in += tokens_in
            run.tokens_out += tokens_out
            if cost is not None:
                run.spend_usd = (run.spend_usd or Decimal(0)) + cost
            session.flush()
            totals = {
                "calls_used": run.calls_used,
                "budget_calls": run.budget_calls,
                "spend_usd": float(run.spend_usd or 0),
                "budget_usd": float(run.budget_usd or 0),
                "tokens_in": run.tokens_in,
                "tokens_out": run.tokens_out,
            }
            reason = _exhausted_reason(
                run.calls_used, run.budget_calls, run.spend_usd, run.budget_usd
            )

        if reason is not None:
            raise BudgetExhausted(
                run_id,
                reason,
                calls_used=totals["calls_used"],
                budget_calls=totals["budget_calls"],
                spend_usd=totals["spend_usd"],
                budget_usd=totals["budget_usd"],
            )
        return totals

    def budget_totals(self, run_id: UUID) -> dict[str, Any]:
        """The four ledger numbers, read without judging them.

        `check_budget` answers "may this step run"; this answers "where does the run
        stand", which is what an event payload needs when something other than the budget
        stopped it.
        """
        with self._tx() as session:
            run = self._run(session, run_id)
            return {
                "calls_used": run.calls_used,
                "budget_calls": run.budget_calls,
                "spend_usd": float(run.spend_usd or 0),
                "budget_usd": float(run.budget_usd or 0),
            }

    def check_budget(self, run_id: UUID, *, cost: int = 1, reserve: int = 0) -> dict[str, Any]:
        """Refuse a step that cannot be afforded, without spending anything.

        `reserve` is how the run keeps two calls for the overview: every step but the
        overview itself asks with `reserve=2`, so the report always gets written.

        **The reserve applies to both ceilings.** It used to apply only to the call
        ceiling, while the dollar gate was a bare `spend >= budget` — so the moment spend
        crossed the line every subsequent check raised, *including the overview's own*.
        Exactly the runs that hit the money ceiling were the ones that lost their only
        deliverable, and they still ended `completed`: run c4566ed2 finished with 37 calls
        to spare, no report, and no error. The dollar equivalent of the reserve is priced
        from what this run's own calls have actually cost, because a per-call estimate that
        came from anywhere else would be wrong for the run it was governing.

        A ceiling of 0 means "no ceiling". That is the normal state of `budget_usd` now —
        the dollar gate is opt-in — and the permanent state of both ceilings on an imported
        run, which never spends anything. `budget_calls` is the ceiling that does the work.
        """
        with self._tx() as session:
            run = self._run(session, run_id)
            totals = {
                "calls_used": run.calls_used,
                "budget_calls": run.budget_calls,
                "spend_usd": float(run.spend_usd or 0),
                "budget_usd": float(run.budget_usd or 0),
            }
            over_calls = (
                run.budget_calls > 0 and run.calls_used + cost + reserve > run.budget_calls
            )
            spend = run.spend_usd or Decimal(0)
            usd_reserve = _usd_reserve(reserve, spend, run.calls_used)
            over_usd = run.budget_usd > 0 and spend + usd_reserve >= run.budget_usd

        if over_calls or over_usd:
            raise BudgetExhausted(
                run_id, "calls" if over_calls else "usd", **totals
            )
        return totals

    # --- events -----------------------------------------------------------------------

    def emit(
        self,
        run_id: UUID,
        type: str,
        payload: Mapping[str, Any] | None = None,
        *,
        round: int | None = None,
    ) -> dict[str, Any]:
        """Append one event. Call this from one place only — see the module docstring."""
        if not type or not isinstance(type, str):
            raise ValueError("event type must be a non-empty string")
        with self._tx() as session:
            event = RunEvent(
                run_id=run_id, round=round, type=type, payload=cap_payload(payload)
            )
            session.add(event)
            session.flush()
            return _event_dict(event)

    def recent_events(self, run_id: UUID, limit: int = 50) -> list[dict[str, Any]]:
        with self._tx() as session:
            return _recent_events(session, run_id, limit)

    # --- lifecycle, controls, heartbeat ------------------------------------------------

    def get_run(self, run_id: UUID) -> dict[str, Any] | None:
        with self._tx() as session:
            run = session.get(Run, run_id)
            return None if run is None else _run_dict(run)

    def active_runs(
        self, *, harness: str | None = None, lifecycles: Sequence[str] | None = None
    ) -> list[dict[str, Any]]:
        """Live runs, oldest first. The reconciler's input, and the lane check's answer.

        Defaults to `LIVE_LIFECYCLES` — every state in which a supervisor may still be
        working, which is wider than the set that holds a harness lane. Pass
        `lifecycles=LANE_LIFECYCLES` to ask the narrower question of who holds the lane.
        """
        wanted = tuple(lifecycles) if lifecycles is not None else tuple(sorted(LIVE_LIFECYCLES))
        query = (
            select(Run)
            .where(Run.deleted_at.is_(None), Run.lifecycle.in_(wanted))
            .order_by(Run.created_at)
        )
        if harness is not None:
            query = query.where(Run.harness == harness)
        with self._tx() as session:
            return [_run_dict(row) for row in session.execute(query).scalars()]

    def lane_holder(
        self, harness: str, *, excluding_run_id: UUID | None = None
    ) -> dict[str, Any] | None:
        """The oldest run holding a lane, with the owner's readable name."""
        query = (
            select(Run, User)
            .outerjoin(User, Run.owner_id == User.id)
            .where(
                Run.deleted_at.is_(None),
                Run.harness == harness,
                Run.lifecycle.in_(LANE_LIFECYCLES),
            )
            .order_by(Run.created_at)
        )
        if excluding_run_id is not None:
            query = query.where(Run.id != excluding_run_id)
        with self._tx() as session:
            row = session.execute(query).first()
            if row is None:
                return None
            run, owner = row
            return {
                "id": str(run.id),
                "title": run.title,
                "owner_id": None if run.owner_id is None else str(run.owner_id),
                "owner_display_name": (
                    None if owner is None else owner.display_name or owner.username
                ),
            }

    def set_lifecycle(
        self, run_id: UUID, lifecycle: str, *, error: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Move the run's lifecycle. Transition legality is the controls layer's call."""
        if lifecycle not in LIFECYCLES:
            raise ValueError(f"Unknown lifecycle {lifecycle!r}; expected one of {LIFECYCLES}")
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            run.lifecycle = lifecycle
            if error is not None:
                run.error = _json_safe(dict(error))
            session.flush()
            return _run_dict(run)

    def set_round(self, run_id: UUID, round: int) -> dict[str, Any]:
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            run.round = round
            session.flush()
            return _run_dict(run)

    def set_control(self, run_id: UUID, action: str | None) -> dict[str, Any]:
        if action is not None and action not in CONTROL_ACTIONS:
            raise ValueError(f"Unknown control {action!r}; expected one of {CONTROL_ACTIONS}")
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            run.control_requested = action
            session.flush()
            return _run_dict(run)

    def get_control(self, run_id: UUID) -> str | None:
        with self._tx() as session:
            return self._run(session, run_id).control_requested

    def heartbeat(self, run_id: UUID, *, pid: int | None = None) -> bool:
        """Stamp liveness. False means the run is gone or soft-deleted — stop working.

        The supervisor self-terminates on a False here (or on two consecutive failures),
        which is what makes "delete the run" a working kill switch.
        """
        with self._tx() as session:
            run = session.get(Run, run_id)
            if run is None or run.deleted_at is not None:
                return False
            run.heartbeat_at = datetime.now(UTC)
            if pid is not None:
                run.supervisor_pid = pid
            return True

    def park_for_shutdown(
        self, run_id: UUID, *, expected_pid: int | None
    ) -> dict[str, Any]:
        """Atomically park a supervisor proven gone during managed service shutdown.

        The pid comparison prevents a stale shutdown snapshot from pausing a replacement
        supervisor. A run that completed while its process was being terminated is left
        untouched. The caller emits only when ``changed`` is true, after no supervisor can
        race the event stream's single-writer rule.
        """
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            previous = run.lifecycle
            if previous not in LANE_LIFECYCLES or run.supervisor_pid != expected_pid:
                return {"changed": False, "previous": previous, "run": _run_dict(run)}
            run.lifecycle = "paused"
            run.control_requested = None
            run.supervisor_pid = None
            run.heartbeat_at = None
            session.flush()
            return {"changed": True, "previous": previous, "run": _run_dict(run)}

    def soft_delete(self, run_id: UUID) -> dict[str, Any]:
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            run.deleted_at = datetime.now(UTC)
            session.flush()
            return _run_dict(run)

    def update_run_fields(
        self, run_id: UUID, *, title: str | None = None, archived: bool | None = None
    ) -> dict[str, Any]:
        """Patch the run's mutable presentation fields: title and archived.

        Neither participates in the engine's own state machine — renaming or archiving a
        run does not touch its lifecycle, budget, config or content. `None` means "leave
        this field alone", not "clear it", so a patch naming only one field cannot blank
        the other.
        """
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            if title is not None:
                run.title = title
            if archived is not None:
                run.archived = archived
            session.flush()
            return _run_dict(run)

    def extend_run(
        self,
        run_id: UUID,
        *,
        rounds_target: int,
        budget_calls: int | None = None,
        budget_usd: float | None | Unchanged = UNCHANGED,
        expect_lifecycles: Sequence[str],
        lifecycle: str = "queued",
    ) -> dict[str, Any]:
        """Give an ended run more rounds to run. The one sanctioned mutation of `config`.

        `config` is immutable after launch and that rule earns its keep: a run that changed
        models under itself would have half its Elo ratings from a different judge. This
        method is the deliberate exception, and it is written as narrowly as the exception
        deserves — it replaces exactly three keys, `rounds`, `budget_calls` and
        `budget_usd`, on a copy of the stored config. `model_table`, `model_tier`,
        `model_overrides`, `runner`, `grounding_depth` and the graft settings are carried
        through untouched because nothing here can reach them.

        The two ceilings are governed differently, and the difference is the point.
        `budget_calls` may only be raised: lowering it below what a run has already spent
        would end the continued run on its first check. `budget_usd` may be raised *or
        removed entirely*, and removing it is always permitted — because it is not a
        lowering. Every role call goes through the Claude CLI on a subscription, so
        `spend_usd` is API-equivalent telemetry rather than money, and a dollar ceiling is
        now off by default; clearing one is bringing an old run up to the system default,
        not authorising spend. Lowering a dollar ceiling stays refused for the same reason
        lowering the call ceiling is: it can only cost a process launch and change nothing.

        Everything else the run has learned is left exactly as it is: the hypotheses with
        their Elo and match history, the clusters, `feedback_history`, `graft_state`, the
        context documents, the per-round resume marks. The next round therefore opens on the
        standings and the meta-review guidance it would have had if the run had never
        stopped.

        Three smaller things happen in the same transaction, because each of them is wrong
        on its own:

        * `engine_state["overview_stale"]` is set, which is how the orchestrator knows to
          rewrite the report from the fuller picture rather than keep the one it wrote when
          the run ended. The old markdown is deliberately *not* cleared — a run mid-extension
          should still show the last report it produced.
        * `control_requested` is cleared, so a `stop` that ended the run does not immediately
          end it again.
        * The lifecycle leaves its terminal state, which is also how the harness lane is
          reclaimed: `queued` is a lane lifecycle, so the partial unique index refuses this
          UPDATE with an `IntegrityError` if another run took the lane meanwhile. That is the
          same gate a launch goes through, and for the same reason — a check the caller makes
          first is friendlier, but only the index is a decision.

        Returns `{"before": {...}, "run": {...}}`; the caller emits the audit event from the
        two, and out-of-band emission is safe here because the run is terminal, so no
        supervisor is alive to race with.
        """
        if lifecycle not in LIFECYCLES:
            raise ValueError(f"Unknown lifecycle {lifecycle!r}; expected one of {LIFECYCLES}")

        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            if run.lifecycle not in tuple(expect_lifecycles):
                raise LifecycleConflict(run_id, run.lifecycle, expect_lifecycles)

            completed = int(dict(run.engine_state).get("last_completed_round", 0))
            if rounds_target <= completed:
                raise ValueError(
                    f"rounds_target {rounds_target} adds nothing to run {run_id}: "
                    f"{completed} rounds are already complete"
                )
            if budget_calls is not None and budget_calls < run.budget_calls:
                raise ValueError(
                    f"budget_calls may only be raised, never lowered: "
                    f"{budget_calls} < {run.budget_calls}"
                )
            if not isinstance(budget_usd, Unchanged) and budget_usd is not None:
                if budget_usd <= 0:
                    raise ValueError(
                        f"budget_usd must be greater than 0, got {budget_usd}; pass None "
                        "to remove the ceiling instead"
                    )
                if run.budget_usd and Decimal(str(budget_usd)) < run.budget_usd:
                    raise ValueError(
                        f"budget_usd may only be raised or removed, never lowered: "
                        f"{budget_usd} < {run.budget_usd}"
                    )

            before = {
                "lifecycle": run.lifecycle,
                "rounds_target": run.rounds_target,
                "budget_calls": run.budget_calls,
                "budget_usd": usd_ceiling(run.budget_usd),
            }

            config = dict(run.config)
            config["rounds"] = int(rounds_target)
            if budget_calls is not None:
                config["budget_calls"] = int(budget_calls)
                run.budget_calls = int(budget_calls)
            if not isinstance(budget_usd, Unchanged):
                # Both halves, together: the config is what the next supervisor reads its
                # settings from, and the column is what `check_budget` enforces against.
                # Moving one without the other leaves a run whose settings disagree with
                # the gate that stops it.
                config["budget_usd"] = None if budget_usd is None else float(budget_usd)
                run.budget_usd = Decimal(0) if budget_usd is None else Decimal(str(budget_usd))
            run.config = _json_safe(config)
            run.rounds_target = int(rounds_target)
            run.engine_state = _json_safe({**run.engine_state, "overview_stale": True})
            run.control_requested = None
            run.lifecycle = lifecycle
            session.flush()
            return {"before": before, "run": _run_dict(run)}

    def set_engine_state(self, run_id: UUID, patch: Mapping[str, Any]) -> dict[str, Any]:
        """Shallow-merge into `engine_state` (scratch state, overview markdown, resume marks).

        `config` has no equivalent, because it is immutable once the run is launched. The
        single exception is `extend_run` above, which moves the round target and the two
        ceilings of a run the scientist has asked to continue, and touches nothing else.
        """
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            run.engine_state = _json_safe({**run.engine_state, **dict(patch)})
            return dict(run.engine_state)

    def record_feedback(self, run_id: UUID, round: int, guidance: str) -> list[dict[str, Any]]:
        """Append this round's meta-review guidance; the Report tab plots the trajectory."""
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            run.feedback_history = [
                *run.feedback_history,
                {"round": round, "guidance": guidance},
            ]
            session.flush()
            return list(run.feedback_history)

    # --- interventions ----------------------------------------------------------------

    def add_note(self, run_id: UUID, text: str) -> dict[str, Any]:
        """Queue a scientist note for the next round boundary (the API answers 202)."""
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            intervention = _intervention("note", text=text)
            run.pending_interventions = [*run.pending_interventions, intervention]
            session.flush()
            return intervention

    def claim_interventions(self, run_id: UUID) -> list[dict[str, Any]]:
        """Take everything queued and clear it, in one transaction. Drained at round start."""
        with self._tx() as session:
            run = self._run(session, run_id, lock=True)
            claimed = list(run.pending_interventions)
            if claimed:
                run.pending_interventions = []
            session.flush()
            return claimed

    # --- context documents ------------------------------------------------------------

    def add_context_docs(
        self, run_id: UUID, docs: Sequence[Mapping[str, str]]
    ) -> list[dict[str, Any]]:
        with self._tx() as session:
            self._run(session, run_id)
            stored = []
            for doc in docs:
                content = doc.get("text", doc.get("content", ""))
                row = RunContextDoc(
                    run_id=run_id,
                    name=doc.get("name", "document"),
                    chars=len(content),
                    content=content,
                )
                session.add(row)
                stored.append(row)
            session.flush()
            return [_context_doc_dict(row) for row in stored]

    def get_context_docs(self, run_id: UUID) -> list[dict[str, Any]]:
        with self._tx() as session:
            rows = session.execute(
                select(RunContextDoc)
                .where(RunContextDoc.run_id == run_id)
                .order_by(RunContextDoc.created_at, RunContextDoc.name)
            ).scalars()
            return [_context_doc_dict(row) for row in rows]

    # --- projection -------------------------------------------------------------------

    def snapshot(self, run_id: UUID) -> dict[str, Any]:
        """Everything `GET /runs/{id}/detail` needs, in the frozen RunDetail shape.

        JSON-safe throughout (ISO timestamps, floats, string ids) so it can be serialised
        by the API and written straight into the run's `state.json` projection.
        """
        with self._tx() as session:
            run = self._run(session, run_id)

            hypotheses = list(
                session.execute(
                    select(Hypothesis)
                    .where(Hypothesis.run_id == run_id)
                    .order_by(Hypothesis.elo.desc(), Hypothesis.hid)
                ).scalars()
            )
            novelty = _latest_novelty(session, [h.id for h in hypotheses])
            leaderboard = sorted(
                (
                    {**_hypothesis_dict(row), "novelty_level": novelty.get(row.id)}
                    for row in hypotheses
                ),
                key=_standing_key,
            )

            matches = list(
                session.execute(
                    select(Match).where(Match.run_id == run_id).order_by(Match.round, Match.ts)
                ).scalars()
            )
            graft_events = list(
                session.execute(
                    select(GraftEvent)
                    .where(GraftEvent.run_id == run_id)
                    .order_by(GraftEvent.round, GraftEvent.created_at)
                ).scalars()
            )
            by_role = [
                {
                    "role": role,
                    "calls": calls,
                    "tokens": int(tokens or 0),
                    "usd": float(usd or 0),
                }
                for role, calls, tokens, usd in session.execute(
                    select(
                        BudgetLedger.role,
                        func.count(),
                        func.sum(BudgetLedger.tokens_in + BudgetLedger.tokens_out),
                        func.sum(BudgetLedger.cost_usd),
                    )
                    .where(BudgetLedger.run_id == run_id)
                    .group_by(BudgetLedger.role)
                    .order_by(BudgetLedger.role)
                )
            ]
            degraded_count = session.execute(
                select(func.count())
                .select_from(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.type == "role_degraded")
            ).scalar_one()
            health = _run_health(session, run_id)
            problems = _problem_events(session, run_id)
            reviews_by_round = _reviews_by_round(session, run_id)
            round_marks = _round_marks(session, run_id)
            # `delivered` is the answer to "was my document used", which `chars` alone
            # reads as yes to. The orchestrator writes `context_delivery` once, when it
            # fits the documents into the per-call cap; before that there is nothing to
            # claim, so the state is `pending` rather than a guess.
            delivery = dict(run.engine_state.get("context_delivery") or {})
            cap = run.engine_state.get("context_char_cap")
            context_docs = [
                {
                    "name": name,
                    "chars": chars,
                    "delivered": delivery.get(name, "pending"),
                    "cap_chars": cap,
                }
                for name, chars in session.execute(
                    select(RunContextDoc.name, RunContextDoc.chars)
                    .where(RunContextDoc.run_id == run_id)
                    .order_by(RunContextDoc.created_at, RunContextDoc.name)
                )
            ]
            events = _recent_events(session, run_id, 50)
            timing_events = [
                {"type": type_, "payload": payload, "ts": ts}
                for type_, payload, ts in session.execute(
                    select(RunEvent.type, RunEvent.payload, RunEvent.ts)
                    .where(
                        RunEvent.run_id == run_id,
                        RunEvent.type.in_(("lifecycle_changed", "run_finished")),
                    )
                    .order_by(RunEvent.seq)
                )
            ]

            owner = session.get(User, run.owner_id) if run.owner_id is not None else None
            summary = _run_summary(
                run,
                leaderboard,
                matches,
                health,
                owner_display_name=(
                    None if owner is None else owner.display_name or owner.username
                ),
                run_elapsed_seconds=elapsed_seconds(
                    timing_events, lifecycle=run.lifecycle
                ),
            )
            from app.engine.research import research_view

            return {
                "research": research_view(dict(run.engine_state.get("research") or {}), leaderboard)
                if run.config.get("workflow") == "adaptive" else None,
                "run": summary,
                "config": dict(run.config),
                "model_table": list(run.config.get("model_table") or []),
                "leaderboard": leaderboard,
                "rounds": _round_summaries(
                    run, hypotheses, matches, graft_events, reviews_by_round, round_marks
                ),
                "recent_events": events,
                "budget": {
                    "calls_used": run.calls_used,
                    "budget_calls": run.budget_calls,
                    "spend_usd": float(run.spend_usd or 0),
                    "budget_usd": float(run.budget_usd or 0),
                    "by_role": by_role,
                },
                "graft_events": [_graft_summary(row) for row in graft_events],
                "context_docs": context_docs,
                "feedback_history": list(run.feedback_history),
                # Kept, and no longer standing in for anything: `role_degraded` counts
                # model/effort *substitutions* and has never once fired in 29 app runs,
                # while the counters beside it count work the run actually lost.
                "degraded_count": degraded_count,
                "failed_calls": health["failed_calls"],
                "lost_steps": health["lost_steps"],
                "retried_calls": health["retried_calls"],
                # Unfiltered by the 50-row `recent_events` window: on a long run the whole
                # failure record can sit outside it (87% of one run's log did), and the
                # forensics panel must not depend on where a failure happened to land.
                "problems": problems,
            }


# --- row → dict -------------------------------------------------------------------------


def _intervention(kind: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": secrets.token_hex(8),
        "kind": kind,
        "ts": datetime.now(UTC).isoformat(),
        **fields,
    }


def _readable(value: str | None) -> str | None:
    """A stored critique field as prose, whatever shape the model wrote it in.

    `None` stays `None` — an absent field and an unreadable one are different facts — but a
    JSON literal becomes the sentence inside it, or the empty string when there is no
    sentence, so a reader is never shown `{}` under the heading "Correctness".
    """
    if value is None:
        return None
    from app.engine.schemas import unwrap_prose

    return unwrap_prose(value)


def _survivor(hid: str, rows: Mapping[str, Any], *, retiring: set[str]) -> str:
    """Follow `duplicate_of` to the first hypothesis that is still standing.

    Bounded by the number of rows, so a cycle written by an older engine cannot hang the
    transaction; it stops on the last hid it saw, which is the same answer the unflattened
    code would have given.
    """
    seen: set[str] = set()
    current = hid
    for _ in range(len(rows) + 1):
        row = rows.get(current)
        if row is None or current in seen:
            return current
        seen.add(current)
        pointer = row.duplicate_of
        stale = row.status in ("archived", "rejected") or current in retiring
        if not stale or not pointer or pointer == current:
            return current
        current = pointer
    return current


def _usd_reserve(reserve: int, spend: Decimal, calls_used: int) -> Decimal:
    """What `reserve` calls are worth in dollars, at this run's own observed rate.

    Zero until the run has spent something, which is the honest answer: with no calls
    behind it there is no rate to extrapolate from, and inventing one would refuse the
    first step of every run with a tight ceiling.
    """
    if reserve <= 0 or calls_used <= 0 or spend <= 0:
        return Decimal(0)
    return (spend / Decimal(calls_used)) * Decimal(reserve)


def _exhausted_reason(
    calls_used: int, budget_calls: int, spend_usd: Decimal | None, budget_usd: Decimal | None
) -> str | None:
    if budget_calls and calls_used >= budget_calls:
        return "calls"
    if budget_usd and (spend_usd or Decimal(0)) >= budget_usd:
        return "usd"
    return None


def _run_dict(run: Run) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "engine_run_id": run.engine_run_id,
        "source": run.source,
        "source_version": run.source_version,
        "title": run.title,
        "question": run.question,
        "prompt": run.prompt,
        "base_prompt_hash": run.base_prompt_hash,
        "harness": run.harness,
        "lifecycle": run.lifecycle,
        "round": run.round,
        "rounds_target": run.rounds_target,
        "calls_used": run.calls_used,
        "budget_calls": run.budget_calls,
        "budget_usd": _float(run.budget_usd),
        "spend_usd": _float(run.spend_usd),
        "tokens_in": run.tokens_in,
        "tokens_out": run.tokens_out,
        "config": dict(run.config),
        "engine_state": dict(run.engine_state),
        "feedback_history": list(run.feedback_history),
        "graft_state": dict(run.graft_state),
        "pending_interventions": list(run.pending_interventions),
        "root_path": run.root_path,
        "control_requested": run.control_requested,
        "supervisor_pid": run.supervisor_pid,
        "heartbeat_at": _iso(run.heartbeat_at),
        "error": run.error,
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
        "deleted_at": _iso(run.deleted_at),
        "archived": run.archived,
    }


def _hypothesis_dict(row: Hypothesis) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "hid": row.hid,
        "title": row.title,
        "body_md": row.body_md,
        "status": row.status,
        "elo": float(row.elo),
        "matches": row.matches,
        "wins": row.wins,
        "cluster": row.cluster,
        "duplicate_of": row.duplicate_of,
        "parent_ids": list(row.parent_ids),
        "operator": row.operator,
        "seed_id": row.seed_id,
        "created_round": row.created_round,
        "source": row.source,
    }


def _match_dict(row: Match) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "round": row.round,
        "hid_a": row.hid_a,
        "hid_b": row.hid_b,
        "status": row.status,
        "winner": row.winner,
        "elo_a_before": _float(row.elo_a_before),
        "elo_a_after": _float(row.elo_a_after),
        "elo_b_before": _float(row.elo_b_before),
        "elo_b_after": _float(row.elo_b_after),
        "k": row.k,
        "debate_md": row.debate_md,
        "judge_model": row.judge_model,
        "tokens_in": row.tokens_in,
        "tokens_out": row.tokens_out,
        "ts": _iso(row.ts),
    }


def _graft_dict(row: GraftEvent) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "round": row.round,
        "n_clusters": row.n_clusters,
        "hhi": _float(row.hhi),
        "signals": dict(row.signals),
        "votes": row.votes,
        "fired": row.fired,
        "abstained_reason": row.abstained_reason,
        "source_domain": row.source_domain,
        "skeleton": row.skeleton,
        "seed_framing": row.seed_framing,
        "seed_id": row.seed_id,
        "created_at": _iso(row.created_at),
    }


def _graft_summary(row: GraftEvent) -> dict[str, Any]:
    """The GraftEvent DTO — skeleton and seed_id stay out of the API shape."""
    return {
        "round": row.round,
        "fired": row.fired,
        "votes": row.votes,
        "n_clusters": row.n_clusters,
        "hhi": _float(row.hhi),
        "abstained_reason": row.abstained_reason,
        "source_domain": row.source_domain,
        "seed_framing": row.seed_framing,
    }


def _event_dict(row: RunEvent) -> dict[str, Any]:
    return {
        "seq": row.seq,
        "run_id": str(row.run_id),
        "round": row.round,
        "type": row.type,
        "payload": dict(row.payload),
        "ts": _iso(row.ts),
    }


def _context_doc_dict(row: RunContextDoc) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "chars": row.chars,
        "content": row.content,
        "created_at": _iso(row.created_at),
    }


# --- snapshot helpers -------------------------------------------------------------------


PROBLEM_EVENT_TYPES: tuple[str, ...] = (
    "contract_violation",
    "step_failed",
    "report_skipped",
    "role_degraded",
    "budget_warning",
)
"""Events that describe something going wrong. Collected in full, never windowed."""

MAX_PROBLEM_EVENTS = 200


def _standing_key(row: Mapping[str, Any]) -> tuple[int, int, float, str]:
    """Rank order for the standings: evidence first, then rating.

    Elo alone is not a ranking. A hypothesis review *rejected* keeps whatever rating it
    had, and one that has never played a match sits at the untouched default of 1200 —
    above every hypothesis that competed and lost. Both used to occupy rank positions the
    UI printed as ordinals: 29 runs showed a rejected or archived idea in the rendered top
    eight, and 19 showed an unplayed one above ideas that had fought and lost.

    Every row is still returned; what changes is where an unranked one sits.
    """
    return (
        0 if row.get("status") == "active" else 1,
        0 if int(row.get("matches") or 0) > 0 else 1,
        -float(row.get("elo") or 0.0),
        str(row.get("hid") or ""),
    )


def _run_health(session: Session, run_id: UUID) -> dict[str, int]:
    """What the run lost, counted from the event log.

    `degraded_count` — the only failure aggregate this API used to expose — counts a model
    or effort *substitution*, which a timed-out or unusable call can never be. It has been
    0 for every run ever executed. These are the numbers that are not.
    """
    failed_calls = session.execute(
        select(func.count())
        .select_from(RunEvent)
        .where(
            RunEvent.run_id == run_id,
            RunEvent.type == "call_finished",
            RunEvent.payload["ok"].astext == "false",
        )
    ).scalar_one()
    lost_steps = session.execute(
        select(func.count())
        .select_from(RunEvent)
        .where(
            RunEvent.run_id == run_id,
            RunEvent.type == "contract_violation",
            # A violation that lost no work does not count as a lost step. The evolution
            # lineage warning is the case: the offspring is still added, only its parentage
            # is incomplete, and counting it here both overstated `lost_steps` and — via
            # `retried_calls = failed - lost` — understated the retries.
            RunEvent.payload["lost"].astext.is_distinct_from("false"),
        )
    ).scalar_one()
    return {
        "failed_calls": int(failed_calls),
        "lost_steps": int(lost_steps),
        # A failed call the retry rescued cost time and budget but lost no work; only the
        # difference between the two is a hole in the result.
        "retried_calls": max(0, int(failed_calls) - int(lost_steps)),
    }


def _problem_events(session: Session, run_id: UUID) -> list[dict[str, Any]]:
    rows = session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.type.in_(PROBLEM_EVENT_TYPES))
        .order_by(RunEvent.seq)
        .limit(MAX_PROBLEM_EVENTS)
    ).scalars()
    return [_event_dict(row) for row in rows]


def _recent_events(session: Session, run_id: UUID, limit: int) -> list[dict[str, Any]]:
    rows = session.execute(
        select(RunEvent)
        .where(RunEvent.run_id == run_id)
        .order_by(RunEvent.seq.desc())
        .limit(limit)
    ).scalars()
    return [_event_dict(row) for row in reversed(list(rows))]


def _latest_novelty(session: Session, hypothesis_ids: Sequence[UUID]) -> dict[UUID, str | None]:
    if not hypothesis_ids:
        return {}
    rows = session.execute(
        select(Review.hypothesis_id, Review.novelty_level)
        .where(Review.hypothesis_id.in_(tuple(hypothesis_ids)))
        .order_by(Review.created_at)
    )
    latest: dict[UUID, str | None] = {}
    for hypothesis_id, level in rows:
        if level is not None or hypothesis_id not in latest:
            latest[hypothesis_id] = level
    return latest


def _reviews_by_round(session: Session, run_id: UUID) -> dict[int, int]:
    """Reviews per round, counted by the round the reflection call actually ran in.

    Attribution used to go through `Hypothesis.created_round`, which is the round the
    *hypothesis* was born in — a different fact, and wrong for every hypothesis reviewed
    later than it was created. Evolution's offspring are born at step 6 and reviewed by the
    next round, so on run c4566ed2 the ladder reported reviews of {1:6, 2:9, 3:6} for calls
    that were actually made {1:6, 2:6, 3:9}.
    """
    rows = session.execute(
        select(RunEvent.round, func.count())
        .where(
            RunEvent.run_id == run_id,
            RunEvent.type == "review_recorded",
            RunEvent.round.is_not(None),
        )
        .group_by(RunEvent.round)
    )
    by_round = {round_: count for round_, count in rows}
    if by_round:
        return by_round
    # An imported run has reviews and no event log at all, so there is nothing better to
    # attribute them by than the round each hypothesis was created in.
    legacy = session.execute(
        select(Hypothesis.created_round, func.count())
        .select_from(Review)
        .join(Hypothesis, Hypothesis.id == Review.hypothesis_id)
        .where(Hypothesis.run_id == run_id)
        .group_by(Hypothesis.created_round)
    )
    return {round_: count for round_, count in legacy}


def _round_marks(session: Session, run_id: UUID) -> dict[int, dict[str, str | None]]:
    """Round start/end timestamps, read back off the event log rather than stored twice.

    `max` for the start and not `min`: a round re-entered after a continue emits a second
    `round_started`, and taking the earliest made the round inherit the abandoned pass's
    clock. Run c4566ed2's round 2 was reported as running for 33 hours 36 minutes, which is
    the gap between two supervisor sessions rather than any work. The completion is a `max`
    for the same reason — the live attempt is the one that describes the round.
    """
    rows = session.execute(
        select(RunEvent.round, RunEvent.type, func.max(RunEvent.ts))
        .where(
            RunEvent.run_id == run_id,
            RunEvent.round.is_not(None),
            RunEvent.type.in_(("round_started", "round_completed")),
        )
        .group_by(RunEvent.round, RunEvent.type)
    )
    marks: dict[int, dict[str, str | None]] = {}
    for round_, type_, ts in rows:
        mark = marks.setdefault(round_, {"started_at": None, "completed_at": None})
        mark["started_at" if type_ == "round_started" else "completed_at"] = _iso(ts)
    return marks


def _round_summaries(
    run: Run,
    hypotheses: Sequence[Hypothesis],
    matches: Sequence[Match],
    graft_events: Sequence[GraftEvent],
    reviews_by_round: Mapping[int, int],
    round_marks: Mapping[int, Mapping[str, str | None]],
) -> list[dict[str, Any]]:
    rounds = {row.created_round for row in hypotheses}
    rounds |= {row.round for row in matches}
    rounds |= {row.round for row in graft_events}
    rounds |= set(reviews_by_round) | set(round_marks)
    rounds |= set(range(1, run.round + 1))
    rounds.discard(0)

    summaries = []
    for round_ in sorted(rounds):
        marks = round_marks.get(round_, {})
        completed_at = marks.get("completed_at")
        in_round = [row for row in matches if row.round == round_]
        summaries.append(
            {
                "round": round_,
                "started_at": marks.get("started_at"),
                "completed_at": completed_at,
                "hypotheses_added": sum(1 for row in hypotheses if row.created_round == round_),
                "matches_completed": sum(1 for row in in_round if row.status == "completed"),
                "matches_planned": len(in_round),
                "reviews": reviews_by_round.get(round_, 0),
                "graft_fired": any(row.fired for row in graft_events if row.round == round_),
                # A run that has been terminal for days has no round in progress. The
                # ladder used to compare `round_ >= run.round` and nothing else, so the
                # last round of any run that died mid-round read "running" forever —
                # `incomplete` was already the right word and was unreachable for it.
                "status": (
                    "completed"
                    if completed_at
                    else "running"
                    if round_ >= run.round and run.lifecycle not in TERMINAL_LIFECYCLES
                    else "incomplete"
                ),
            }
        )
    return summaries


def _run_summary(
    run: Run,
    leaderboard: Sequence[Mapping[str, Any]],
    matches: Sequence[Match],
    health: Mapping[str, int] | None = None,
    owner_display_name: str | None = None,
    run_elapsed_seconds: int | None = None,
) -> dict[str, Any]:
    counts = {
        "active": sum(1 for row in leaderboard if row["status"] == "active"),
        "rejected": sum(1 for row in leaderboard if row["status"] == "rejected"),
        "archived": sum(1 for row in leaderboard if row["status"] == "archived"),
        "matches": sum(1 for row in matches if row.status == "completed"),
    }
    # A run whose hypotheses were all rejected still has a story to tell, so `top` falls
    # back to any status rather than rendering an empty leaderboard.
    ranked = [row for row in leaderboard if row["status"] == "active"] or list(leaderboard)
    graft_state = dict(run.graft_state)
    level, custom = model_level(run.config, source=run.source)
    return {
        "id": str(run.id),
        "engine_run_id": run.engine_run_id,
        "source": run.source,
        "source_version": run.source_version,
        "title": run.title,
        "question": run.question,
        "owner_display_name": owner_display_name,
        "harness": run.harness,
        "lifecycle": run.lifecycle,
        "round": run.round,
        "rounds_target": run.rounds_target,
        "calls_used": run.calls_used,
        "budget_calls": run.budget_calls,
        "spend_usd": float(run.spend_usd or 0),
        "tokens_total": (run.tokens_in or 0) + (run.tokens_out or 0),
        "model_level": level,
        "model_level_custom": custom,
        "elapsed_seconds": run_elapsed_seconds,
        "counts": counts,
        "top": [
            {"hid": row["hid"], "title": row["title"], "elo": row["elo"], "status": row["status"]}
            for row in ranked[:3]
        ],
        "graft": {
            "enabled": bool((run.config.get("graft") or {}).get("enabled", False)),
            "fired_count": int(graft_state.get("fired_count", 0)),
            "pending": bool(graft_state.get("pending_seed")),
        },
        "archived": run.archived,
        "has_overview": bool(run.engine_state.get("overview_md")),
        # The header and every row of the run list are built from this object, and until
        # now it carried no failure signal of any kind: a run that lost three steps, wrote
        # no report and blew its cost ceiling rendered identically to one that did
        # everything asked.
        "failed_calls": int((health or {}).get("failed_calls", 0)),
        "lost_steps": int((health or {}).get("lost_steps", 0)),
        "ended_reason": run.engine_state.get("ended_reason"),
        "overview_skipped_reason": run.engine_state.get("overview_skipped_reason"),
        "created_at": _iso(run.created_at),
        "updated_at": _iso(run.updated_at),
    }
