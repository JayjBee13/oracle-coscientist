"""The one smoke that spends real money, and the only proof the product works.

Everything else in `scripts/smoke` runs against the scripted `FakeRunner`. This drives one
small research run through the real API, the real supervisor and the real Claude CLI, and
then asks the questions that the fake can never answer:

* Did a multi-agent loop actually complete — generation, reflection, clustering, a
  tournament with debates and Elo movement, evolution, meta-review and a written report?
  Until this passes, it never has: every "real" run in this project's history asked the CLI
  for a single hypothesis and stopped.
* Was any call denied? A grounded call with `--tools` but no matching `--allowedTools` is
  refused after burning ~59K tokens, and for a year that failure was invisible. Any denial,
  on any call, fails the run here — and the fix is always the flag, never a weaker
  permission mode.
* Did grounding actually happen? `--setting-sources ""` suppresses the host's config on a
  subscription account, and the open question is whether WebSearch still works underneath
  it. At `deep` grounding, at least one generation call must report a real search.
* Is the system prompt being cached rather than rebuilt every call? A repeat role must show
  cache reads, or every call is paying full price for a prefix it already sent.
* Did anything get written outside the run's own directory? The historical archive and both
  legacy engine checkouts are hashed before and after; in June 2026 a run mutated another
  run's state file, and that is the check that would have caught it.

Run it through `real_tiny_smoke.ps1`, which owns the guard and the server. Reading this
file alone: it assumes a backend on `--api-base` and a database it can read directly.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.config import APP_ROOT, Settings, get_settings
from app.db.session import get_session_factory

# A real hypothesis is full of characters cp1252 has never heard of, and this script's
# stdout is a pipe. Without this the first micro-sign or arrow in a body kills the report
# after the run has already been paid for.
for _stream in (sys.stdout, sys.stderr):
    _stream.reconfigure(encoding="utf-8", errors="replace")

# --------------------------------------------------------------------------- the run

QUESTION = (
    "What are underexplored mechanisms linking sleep fragmentation to metabolic dysfunction?"
)

# Plan 6.2, with two measured corrections. `grounding_depth` is deep on purpose: it is the
# setting that forces a real search, and a smoke that proved the grounded path only at
# `shallow` would prove nothing. `graft` is off because one round cannot collapse. The model
# tier is `maximum` as the table ships — weakening the app's model table for the convenience
# of a smoke would mean testing a system nobody runs, and this is the one test that proves
# the real CLI accepts the `fable` alias the whole model policy rests on.
#
# The first correction is `budget_usd`. The plan says 3.00; the first real run measured what
# this workload actually costs on sonnet-5, which is what the table shipped at the time:
#
#     generation (deep, high)  $0.91    reflection x3  $0.46 / $0.28 / $0.25
#     proximity (haiku)        $0.01    ranking x2     $0.45 / $0.08
#     evolution                $0.66    = $3.10 before the meta-review or the overview
#
# The dollar ceiling worked exactly as designed — the run stopped and ended gracefully — but
# at 3.00 it stopped every time, one step short of the report, which would make the plan's
# own assertions ("overview written", ">=2 matches") permanently unreachable.
#
# Those figures are now telemetry rather than money: role calls go through the CLI on a
# subscription, so nothing here is billed per token. `budget_usd` is therefore unset — the
# ceiling that stopped this smoke one step short of its report twice is simply not imposed
# any more. `budget_calls` is the governor, and `wall_clock_minutes` is the guard against a
# pathological loop. 25 minutes is the plan's own cap, and comfortably above the 7-12 a
# clean pass takes even with every reasoning role now at high effort.
#
# The second correction is `generation_batch`, for the same reason: the plan says 3, and 3
# cannot reliably produce the 2 matches the plan also asks for. A round's matches are
# adjacent pairs among *active* hypotheses, so three hypotheses give two pairs only if all
# three survive — and on the third real run the proximity agent correctly called one a
# duplicate of another, leaving two competitors and therefore exactly one possible match.
# Four generated leaves two pairs standing through one rejection or one merge. The extra
# shard and its review cost two calls, hence 14.
CONFIG: dict[str, Any] = {
    "rounds": 1,
    "generation_batch": 4,
    "matches_per_round": 2,
    "evolve_top_k": 2,
    "budget_calls": 14,
    "wall_clock_minutes": 25,
    "grounding_depth": "deep",
    "graft": {"enabled": False},
    "model_tier": "maximum",
    "runner": "claude",
}

WORKSHOP_QUESTION = "How does intermittent hypoxia during sleep affect insulin sensitivity?"
WORKSHOP_TIMEOUT_S = 300.0

# What "substantive" means, so the smoke cannot be passed by placeholders. The old system's
# idea of a hypothesis was the string "Generated Research Hypothesis".
MIN_BODY_CHARS = 500
MIN_TITLE_CHARS = 20
MIN_DEBATE_CHARS = 400
MIN_OVERVIEW_CHARS = 1200
GENERIC_TITLES = {
    "untitled",
    "hypothesis",
    "generated research hypothesis",
    "research hypothesis",
    "new hypothesis",
}

POLL_SECONDS = 5.0
ARTIFACT_GRACE_S = 90.0
"""How long to wait for the projection after `run_finished`.

The write order at the end of a run is overview → lifecycle → `run_finished` → artifacts,
so the event that says the run is over arrives *before* the files it produced exist.
"""


# ------------------------------------------------------------------------------- http


class ApiFailure(RuntimeError):
    pass


def api(
    method: str, base: str, path: str, body: Any = None, *, timeout: float = 60.0
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(  # noqa: S310 — a loopback URL this script composed
        f"{base}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise ApiFailure(f"{method} {path} -> {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ApiFailure(f"{method} {path} -> {exc.reason}") from exc
    return json.loads(raw) if raw.strip() else None


# ------------------------------------------------------------------------ filesystem


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under a root, by size and mtime. Cheap enough to run on the archive."""
    files: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return files
    for path in root.rglob("*"):
        try:
            if not path.is_file():
                continue
            stat = path.stat()
        except OSError:
            continue
        files[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return files


def guarded_roots(settings: Settings) -> dict[str, Path]:
    """Everything this run must not touch, beyond its own directory under `runs_root`.

    The historical `ai-coscientist*/runs` source roots and the `engines/v1|v2` copies are gone
    — deleted once every run they held was imported and archived — and `Settings` dropped the
    four fields that named them. The archive is now the only copy of that data, so it is the
    root that matters here; `runs_root` catches a run writing outside its own workdir.
    """
    return {
        "archive": APP_ROOT / "archive",
        "runs_root": settings.runs_root,
    }


def changes(
    before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]], *, allow: str = ""
) -> list[str]:
    """Paths that changed, ignoring anything under `allow` (the run's own directory)."""
    return [
        path
        for path in sorted(set(before) | set(after))
        if before.get(path) != after.get(path) and not (allow and path.startswith(allow))
    ]


# ---------------------------------------------------------------------------- database


class Reader:
    """Direct reads of what the run wrote. The API shows a projection; this is the record."""

    def __init__(self, settings: Settings) -> None:
        self._factory = get_session_factory(settings)

    def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        with self._factory() as session:
            return [dict(row) for row in session.execute(text(sql), params).mappings()]

    def one(self, sql: str, **params: Any) -> dict[str, Any] | None:
        rows = self.rows(sql, **params)
        return rows[0] if rows else None

    def identity(self) -> dict[str, str]:
        row = self.one("select current_database() as db, current_user as usr")
        assert row is not None
        return {"database": row["db"], "user": row["usr"]}

    def run(self, run_id: str) -> dict[str, Any]:
        row = self.one(
            "select id::text as id, engine_run_id, lifecycle, round, calls_used, "
            "budget_calls, spend_usd, budget_usd, tokens_in, tokens_out, config, "
            "engine_state, feedback_history, root_path, error, "
            "extract(epoch from (updated_at - created_at)) as elapsed_s "
            "from runs where id = :id",
            id=run_id,
        )
        if row is None:
            raise ApiFailure(f"run {run_id} vanished from the database")
        return row

    def events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        return self.rows(
            "select seq, round, type, payload, ts from run_events "
            "where run_id = :id and seq > :seq order by seq",
            id=run_id,
            seq=after_seq,
        )

    def hypotheses(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "select hid, title, body_md, status, elo, matches, wins, cluster, "
            "duplicate_of, parent_ids, operator, created_round from hypotheses "
            "where run_id = :id order by elo desc, hid",
            id=run_id,
        )

    def reviews(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "select h.hid, r.verdict, r.novelty_level, r.key_risk, r.model "
            "from reviews r join hypotheses h on h.id = r.hypothesis_id "
            "where h.run_id = :id order by h.hid",
            id=run_id,
        )

    def matches(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "select round, hid_a, hid_b, status, winner, elo_a_before, elo_a_after, "
            "elo_b_before, elo_b_after, k, debate_md, judge_model from matches "
            "where run_id = :id order by round, ts",
            id=run_id,
        )

    def ledger(self, run_id: str) -> list[dict[str, Any]]:
        return self.rows(
            "select round, role, model, tokens_in, tokens_out, cache_creation, cache_read, "
            "cost_usd, duration_ms, status from budget_ledger where run_id = :id order by ts",
            id=run_id,
        )


# ------------------------------------------------------------------------- assertions


class Checks:
    """The verdict, accumulated. Every check is named, and every failure carries evidence."""

    def __init__(self, *, demo: bool = False) -> None:
        self.results: list[dict[str, Any]] = []
        self.demo = demo

    def check(self, name: str, passed: bool, detail: str, *, real_only: bool = False) -> bool:
        """Record one verdict.

        `real_only` marks a property only a real CLI call can have — a web search, the
        telemetry the CLI reports, a judge's debate. The scripted runner cannot produce
        those, so in a demo rehearsal they are skipped rather than failed: a free
        rehearsal that always fails teaches nobody anything, and worse, it trains the
        reader to ignore a red line.
        """
        if real_only and self.demo:
            self.results.append(
                {"name": name, "passed": True, "skipped": True, "detail": f"skipped in demo — {detail}"}
            )
            return True
        self.results.append(
            {"name": name, "passed": bool(passed), "skipped": False, "detail": detail}
        )
        return bool(passed)

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [item for item in self.results if not item["passed"]]

    def report(self) -> None:
        print("")
        print("== assertions ==")
        for item in self.results:
            mark = "SKIP" if item.get("skipped") else ("PASS" if item["passed"] else "FAIL")
            print(f"  {mark}  {item['name']}: {item['detail']}")


def is_generic(title: str) -> bool:
    return title.strip().lower() in GENERIC_TITLES


def excerpt(body: str, limit: int = 700) -> str:
    body = body.strip()
    return body if len(body) <= limit else body[:limit].rstrip() + " […]"


# ------------------------------------------------------------------------------ phases


def run_workshop(base: str, checks: Checks, *, harness: str = "claude") -> dict[str, Any]:
    """One live workshop call: the other place the CLI is invoked from the API."""
    print("")
    print(f"== workshop ({harness}) ==")
    started = time.monotonic()
    workshop = api("POST", base, "/api/workshops", {
        "question": WORKSHOP_QUESTION,
        "harness": harness,
    })
    workshop_id = workshop["id"]
    print(f"  workshop {workshop_id} state={workshop['state']}")

    while workshop["state"] == "refining":
        if time.monotonic() - started > WORKSHOP_TIMEOUT_S:
            break
        time.sleep(POLL_SECONDS)
        workshop = api("GET", base, f"/api/workshops/{workshop_id}")

    elapsed = time.monotonic() - started
    options = workshop.get("options") or []
    print(f"  state={workshop['state']} options={len(options)} in {elapsed:.0f}s")
    if workshop.get("error"):
        print(f"  error: {json.dumps(workshop['error'], ensure_ascii=False)[:600]}")

    ready = workshop["state"] == "options_ready" and len(options) == 2
    complete = ready and all(
        (option.get("prompt") or "").strip()
        and (option.get("strategy") or "").strip()
        and isinstance(option.get("recommended_settings"), dict)
        and {"rounds", "budget_calls", "matches_per_round", "grounding_depth"}
        <= set(option["recommended_settings"])
        for option in options
    )
    for option in options:
        print(f"  - [{option.get('strategy')}] {excerpt(option.get('prompt') or '', 240)}")
        print(f"    settings: {json.dumps(option.get('recommended_settings'), sort_keys=True)}")

    checks.check(
        "workshop returns two real options with recommended settings",
        complete,
        f"state={workshop['state']} options={len(options)} in {elapsed:.0f}s",
    )
    return workshop


def watch(reader: Reader, base: str, run_id: str, deadline: float) -> dict[str, Any]:
    """Follow the run's events until it says it is over, or the clock runs out."""
    print("")
    print("== run (live claude) ==")
    seq = 0
    terminal: str | None = None
    started = time.monotonic()

    while time.monotonic() < deadline:
        for event in reader.events(run_id, seq):
            seq = event["seq"]
            payload = event["payload"] or {}
            print(f"  [{time.monotonic() - started:6.0f}s] {event['type']:20s} {_line(payload)}")
            if event["type"] in ("run_finished", "run_failed"):
                terminal = event["type"]
        if terminal is not None:
            break
        time.sleep(POLL_SECONDS)

    if terminal is None:
        print("  !! wall-clock ceiling reached; force-stopping the run")
        try:
            api("POST", base, f"/api/runs/{run_id}/controls", {"action": "force_stop"})
        except ApiFailure as exc:
            print(f"  !! force stop failed: {exc}")
    return {
        "terminal_event": terminal,
        "elapsed_s": time.monotonic() - started,
        "last_seq": seq,
    }


def _line(payload: dict[str, Any]) -> str:
    """One readable line per event — enough to watch a run without a second window."""
    keys = ("role", "hid", "title", "verdict", "winner", "lifecycle", "reason", "round", "ok")
    parts = [
        f"{key}={payload[key]}"
        for key in keys
        if key in payload and payload[key] is not None
    ]
    if payload.get("error"):
        parts.append(f"error={str(payload['error'])[:200]}")
    telemetry = payload.get("telemetry") or {}
    if telemetry.get("web_search_requests"):
        parts.append(f"searches={telemetry['web_search_requests']}")
    if telemetry.get("permission_denials"):
        parts.append(f"DENIALS={json.dumps(telemetry['permission_denials'])[:200]}")
    return " ".join(parts)[:400]


def await_overview(workdir: Path, deadline: float) -> Path | None:
    """The projection lands after `run_finished`; give it its moment before failing it."""
    target = workdir / "research_overview.md"
    while time.monotonic() < deadline:
        if target.exists() and target.stat().st_size > 0:
            return target
        time.sleep(2.0)
    return target if target.exists() else None


# -------------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", required=True, help="e.g. http://127.0.0.1:8791")
    parser.add_argument("--deadline-seconds", type=float, default=1500.0)
    parser.add_argument("--json-out", default="", help="where to write the evidence file")
    parser.add_argument("--skip-workshop", action="store_true")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="rehearse the plumbing against the scripted runner. Spends nothing, proves "
        "nothing about the product: the grounding and telemetry checks cannot pass.",
    )
    args = parser.parse_args()

    harness = "demo" if args.demo else "claude"
    config = {**CONFIG, "runner": harness if args.demo else "claude"}

    settings = get_settings()
    reader = Reader(settings)
    checks = Checks(demo=bool(args.demo))
    base = args.api_base.rstrip("/")

    # --- guards, before a penny is spent ---------------------------------------------
    identity = reader.identity()
    print(f"database={identity['database']} user={identity['user']}")
    if identity["database"] != "ai_coscientist_gui":
        print(
            f"REFUSING: this smoke only ever runs against ai_coscientist_gui, "
            f"got {identity['database']}",
            file=sys.stderr,
        )
        return 2

    health = api("GET", base, "/api/health")
    claude = (health.get("harnesses") or {}).get("claude") or {}
    print(f"backend={base} db_ok={health.get('db_ok')} claude={json.dumps(claude)}")
    if not claude.get("installed"):
        print("REFUSING: the claude CLI is not installed or did not probe", file=sys.stderr)
        return 2

    roots = guarded_roots(settings)
    before = {label: snapshot(root) for label, root in roots.items()}
    print("guarding " + ", ".join(f"{label}({len(before[label])} files)" for label in roots))

    # --- the workshop ------------------------------------------------------------------
    workshop: dict[str, Any] = {}
    if not args.skip_workshop:
        workshop = run_workshop(base, checks, harness=harness)

    # --- the run -----------------------------------------------------------------------
    started_at = time.monotonic()
    deadline = started_at + args.deadline_seconds
    created = api(
        "POST",
        base,
        "/api/runs",
        {
            "question": QUESTION,
            "title": "Real smoke — sleep fragmentation and metabolic dysfunction",
            "harness": harness,
            "config": config,
        },
        timeout=120.0,
    )
    run_id = created["run"]["id"]
    engine_run_id = created["run"]["engine_run_id"]
    workdir = settings.workdir_for(engine_run_id)
    print(f"run {run_id} ({engine_run_id}) workdir={workdir}")
    print("  model table: " + ", ".join(
        f"{row['role']}={row['model']}/{row['effort']}" for row in created["model_table"]
    ))

    watched = watch(reader, base, run_id, deadline)
    wall_clock = time.monotonic() - started_at

    overview_file = await_overview(workdir, time.monotonic() + ARTIFACT_GRACE_S)
    after = {label: snapshot(root) for label, root in roots.items()}

    # --- the evidence ------------------------------------------------------------------
    run = reader.run(run_id)
    hypotheses = reader.hypotheses(run_id)
    reviews = reader.reviews(run_id)
    matches = reader.matches(run_id)
    ledger = reader.ledger(run_id)
    events = reader.events(run_id)
    finished_calls = [event for event in events if event["type"] == "call_finished"]
    telemetry = [(event["payload"] or {}) for event in finished_calls]
    overview_md = (run["engine_state"] or {}).get("overview_md") or ""

    print("")
    print("== evidence ==")
    print(f"lifecycle={run['lifecycle']} round={run['round']} "
          f"calls_used={run['calls_used']}/{run['budget_calls']} "
          f"spend=${float(run['spend_usd'] or 0):.4f}/${float(run['budget_usd'] or 0):.2f} "
          f"tokens_in={run['tokens_in']} tokens_out={run['tokens_out']} "
          f"wall_clock={wall_clock / 60:.1f}min")
    if run.get("error"):
        print(f"error={json.dumps(run['error'], ensure_ascii=False)[:1500]}")

    print("")
    print(f"hypotheses ({len(hypotheses)}):")
    for row in hypotheses:
        print(f"  {row['hid']} elo={float(row['elo']):.1f} matches={row['matches']} "
              f"status={row['status']} round={row['created_round']} "
              f"operator={row['operator'] or '-'} chars={len(row['body_md'])}")
        print(f"    title: {row['title']}")
    if hypotheses:
        print("")
        print(f"body of {hypotheses[0]['hid']} (top of the leaderboard):")
        for line in excerpt(hypotheses[0]["body_md"], 1400).splitlines():
            print(f"    {line}")

    print("")
    print(f"reviews ({len(reviews)}): " + ", ".join(
        f"{row['hid']}:{row['verdict']}/{row['novelty_level']}" for row in reviews
    ))

    print("")
    print(f"matches ({len(matches)}):")
    for row in matches:
        print(f"  r{row['round']} {row['hid_a']} vs {row['hid_b']} status={row['status']} "
              f"winner={row['winner']} k={row['k']} "
              f"elo_a {row['elo_a_before']}->{row['elo_a_after']} "
              f"elo_b {row['elo_b_before']}->{row['elo_b_after']} "
              f"debate_chars={len(row['debate_md'] or '')} judge={row['judge_model']}")
    if matches and matches[0].get("debate_md"):
        print("")
        print("debate excerpt (first match):")
        for line in excerpt(matches[0]["debate_md"], 900).splitlines():
            print(f"    {line}")

    print("")
    print(f"budget ledger ({len(ledger)} rows):")
    for row in ledger:
        print(f"  {row['role']:12s} {str(row['model'] or '-'):18s} in={row['tokens_in']:>6} "
              f"out={row['tokens_out']:>5} cache_create={row['cache_creation']:>6} "
              f"cache_read={row['cache_read']:>7} "
              f"cost=${float(row['cost_usd'] or 0):.4f} {row['duration_ms']}ms {row['status']}")

    print("")
    print("call telemetry:")
    for payload in telemetry:
        facts = payload.get("telemetry") or {}
        print(f"  {payload.get('role'):12s} ok={payload.get('ok')} "
              f"denials={json.dumps(facts.get('permission_denials'))} "
              f"searches={facts.get('web_searches')} tools={json.dumps(facts.get('tool_uses'))} "
              f"turns={facts.get('num_turns')} subtype={facts.get('subtype')}")

    print("")
    print(f"overview: {len(overview_md)} chars in the database, "
          f"file={'present' if overview_file else 'MISSING'}")
    if overview_md:
        print("overview excerpt:")
        for line in excerpt(overview_md, 1200).splitlines():
            print(f"    {line}")

    # --- the assertions ----------------------------------------------------------------
    checks.check(
        "run reached completed, on its own run_finished event",
        watched["terminal_event"] == "run_finished" and run["lifecycle"] == "completed",
        f"terminal_event={watched['terminal_event']} lifecycle={run['lifecycle']}",
    )

    substantive = [
        row
        for row in hypotheses
        if len(row["body_md"]) >= MIN_BODY_CHARS
        and len(row["title"].strip()) >= MIN_TITLE_CHARS
        and not is_generic(row["title"])
    ]
    distinct_titles = len({row["title"].strip().lower() for row in hypotheses})
    checks.check(
        "at least 3 hypotheses with substantive, non-placeholder content",
        len(substantive) >= 3 and distinct_titles == len(hypotheses),
        f"{len(substantive)}/{len(hypotheses)} substantive (body>={MIN_BODY_CHARS} chars, "
        f"title>={MIN_TITLE_CHARS} chars, not generic), {distinct_titles} distinct titles",
    )

    completed_matches = [row for row in matches if row["status"] == "completed"]
    with_debate = [
        row
        for row in completed_matches
        if len(row["debate_md"] or "") >= MIN_DEBATE_CHARS
        and row["elo_a_before"] != row["elo_a_after"]
        and row["elo_b_before"] != row["elo_b_after"]
    ]
    checks.check(
        "at least 2 completed matches, each with a debate and Elo movement",
        len(with_debate) >= 2,
        f"{len(with_debate)}/{len(completed_matches)} completed matches carry a debate "
        f">={MIN_DEBATE_CHARS} chars and moved both sides' Elo",
        real_only=True,
    )

    checks.check(
        "research_overview.md written and non-trivial",
        overview_file is not None and len(overview_md) >= MIN_OVERVIEW_CHARS,
        f"file={'present' if overview_file else 'missing'} db_chars={len(overview_md)} "
        f"(minimum {MIN_OVERVIEW_CHARS})",
    )

    ledger_cost = sum(float(row["cost_usd"] or 0) for row in ledger)
    reconciles = (
        len(ledger) == run["calls_used"] == len(finished_calls)
        and abs(ledger_cost - float(run["spend_usd"] or 0)) < 0.01
    )
    # A dollar ceiling of 0 means there is none, which is now the default: these are CLI
    # calls on a subscription, so the recorded spend is API-equivalent telemetry and there
    # is nothing for it to be under. The call ceiling is the one that has to hold.
    dollar_ceiling = float(run["budget_usd"] or 0)
    within_ceilings = run["calls_used"] <= run["budget_calls"] and (
        dollar_ceiling <= 0 or float(run["spend_usd"] or 0) <= dollar_ceiling
    )
    checks.check(
        "budget ledger reconciles with the calls actually made",
        reconciles and within_ceilings,
        f"{len(ledger)} ledger rows, calls_used={run['calls_used']}, "
        f"{len(finished_calls)} call_finished events, "
        f"ledger ${ledger_cost:.4f} vs run ${float(run['spend_usd'] or 0):.4f}, "
        f"ceiling {run['budget_calls']} calls"
        + (f" / ${dollar_ceiling:.2f}" if dollar_ceiling > 0 else " / no dollar ceiling"),
    )

    reported = [payload for payload in telemetry if payload.get("telemetry")]
    # Every call that returned an answer has to *say* it was not denied. A missing key is
    # not the same as an empty list: it is the absence of evidence, and the whole point of
    # this check is that the denial which cost this project a year was never reported at all.
    answered = [payload for payload in telemetry if payload.get("ok")]
    silent = [
        payload
        for payload in answered
        if "permission_denials" not in (payload.get("telemetry") or {})
    ]
    denied = [
        payload
        for payload in reported
        if (payload["telemetry"].get("permission_denials") or [])
    ]
    checks.check(
        "every call reported an empty permission_denials",
        bool(answered) and not denied and not silent,
        f"{len(reported)}/{len(telemetry)} calls reported telemetry, {len(denied)} with "
        f"denials, {len(silent)} answered without reporting the field"
        + (
            f": {json.dumps([p['telemetry']['permission_denials'] for p in denied])[:600]}"
            if denied
            else ""
        ),
        real_only=True,
    )

    # A run can limp to a completed lifecycle on retries while half its calls were refused
    # — that is exactly what happened the first time this smoke ran, when the host's MCP
    # connectors reached the calls and the init assertions did their job. A completed run
    # whose calls were being refused is not a working product.
    refused = [
        payload
        for payload in telemetry
        if not payload.get("ok") and "init assertion failed" in str(payload.get("error") or "")
    ]
    violations = [event for event in events if event["type"] == "contract_violation"]
    checks.check(
        "no call was refused by its own invocation invariants",
        not refused and not violations,
        f"{len(refused)} init-assertion refusals, {len(violations)} contract violations"
        + (f": {str(refused[0].get('error'))[:300]}" if refused else "")
        + (
            f": {json.dumps((violations[0]['payload'] or {}).get('error'))[:300]}"
            if violations and not refused
            else ""
        ),
    )

    # `web_searches` is counted from the transcript's tool_use blocks. The provider's
    # `web_search_requests` counts server-side tool use and is always 0 here, because the
    # CLI runs WebSearch locally — asserting on it would fail a working grounded run.
    searches = {
        payload.get("role"): payload["telemetry"].get("web_searches") for payload in reported
    }
    generation_searches = [
        payload["telemetry"].get("web_searches") or 0
        for payload in reported
        if payload.get("role") == "generation"
    ]
    checks.check(
        "at least one generation call actually searched the web",
        any(count > 0 for count in generation_searches),
        f"generation search counts {generation_searches}; by role {json.dumps(searches)}",
        real_only=True,
    )

    repeated_roles = {
        role for role in (row["role"] for row in ledger)
        if sum(1 for row in ledger if row["role"] == role) > 1
    }
    cached = [
        row
        for row in ledger
        if row["role"] in repeated_roles and (row["cache_read"] or 0) > 0
    ]
    reads = ", ".join(f"{row['role']} cache_read={row['cache_read']}" for row in cached[:6])
    checks.check(
        "a repeated role read the cached prompt prefix rather than rebuilding it",
        bool(cached),
        f"repeated roles {sorted(repeated_roles)}; " + (reads or "no cache reads recorded"),
    )

    allow = f"{engine_run_id}/"
    outside = {
        label: changes(before[label], after[label], allow=allow if label == "runs_root" else "")
        for label in roots
    }
    dirty = {label: paths for label, paths in outside.items() if paths}
    checks.check(
        "nothing was written outside the run's own workdir",
        not dirty,
        "clean: " + ", ".join(sorted(roots))
        if not dirty
        else json.dumps({label: paths[:10] for label, paths in dirty.items()}),
    )

    checks.check(
        "the run finished inside the wall-clock ceiling",
        wall_clock <= args.deadline_seconds,
        f"{wall_clock / 60:.1f} min of {args.deadline_seconds / 60:.0f} min allowed",
    )

    checks.report()

    evidence = {
        "run_id": run_id,
        "engine_run_id": engine_run_id,
        "workdir": str(workdir),
        "wall_clock_s": round(wall_clock, 1),
        "run": {key: str(value) for key, value in run.items() if key != "engine_state"},
        "hypotheses": hypotheses,
        "reviews": reviews,
        "matches": matches,
        "ledger": ledger,
        "telemetry": telemetry,
        "overview_md": overview_md,
        "workshop": workshop,
        "filesystem": outside,
        "checks": checks.results,
    }
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(evidence, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        print(f"\nevidence written to {args.json_out}")

    print("")
    if checks.failed:
        print(f"REAL SMOKE FAILED: {len(checks.failed)} of {len(checks.results)} checks")
        for item in checks.failed:
            print(f"  FAIL {item['name']}: {item['detail']}")
        return 1
    print(f"REAL SMOKE PASSED: {len(checks.results)} checks, "
          f"{run['calls_used']} calls, ${float(run['spend_usd'] or 0):.4f}, "
          f"{wall_clock / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
