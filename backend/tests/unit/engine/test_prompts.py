"""The prompt port, diffed against the archive, and the composition rules on top of it.

The port is only trustworthy if the passages that carry each role's judgement are provably
identical to the archived originals, so these tests read both files and compare. The
archive is committed (`archive/engine-source/`) and is the only copy of the
pre-overhaul engines, so a missing archive is a failure here rather than a skip.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.prompts import (
    ARCHIVE_ORIGINALS,
    CONTEXT_CHAR_CAP,
    EXPLORATION_DIRECTIVES,
    OVERVIEW_CHAR_BUDGET,
    PRESERVED_PASSAGES,
    TRAJECTORY_FLOOR,
    ContextBlock,
    RoundContext,
    Seed,
    build_context_block,
    cartographer_prompt,
    evolution_prompt,
    generation_prompt,
    load_asset,
    meta_review_prompt,
    overview_prompt,
    proximity_prompt,
    ranking_prompt,
    reflection_prompt,
    render_system_prompt,
    repair_suffix,
)

BACKEND_ROOT = Path(__file__).resolve().parents[3]
ARCHIVE = BACKEND_ROOT.parent / "archive" / "engine-source"

ASSET_NAMES = tuple(ARCHIVE_ORIGINALS)


def original(name: str) -> str:
    path = ARCHIVE / ARCHIVE_ORIGINALS[name]
    assert path.is_file(), f"archived original missing: {path}"
    return path.read_text(encoding="utf-8")


def a_hypothesis(hid="h001", title="Sediment turnover sets the ceiling", elo=1200.0):
    return {
        "hid": hid,
        "title": title,
        "elo": elo,
        "matches": 2,
        "wins": 1,
        "body_md": f"# {title}\n\n**Claim:** Turnover bounds the bloom.\n\n"
        f"**Mechanism:** Resuspension outpaces uptake.\n",
    }


def a_review(hid="h001", note="A distinctive note about h001."):
    return {
        "hid": hid,
        "verdict": "pass",
        "novelty_level": "high",
        "novelty_note": f"novelty of {hid}",
        "correctness": f"correctness of {hid}",
        "testability": f"testability of {hid}",
        "key_risk": f"risk of {hid}",
        "note": note,
    }


# --- the port ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_preserved_passages_are_byte_identical_to_the_archive(name):
    """Both directions: the passage survived the port, and it is the archive's wording."""
    ported = load_asset(name)
    archived = original(name)
    for passage in PRESERVED_PASSAGES.get(name, ()):
        assert passage in archived, f"{name}: passage is not in the archived original"
        assert passage in ported, f"{name}: passage did not survive the port"


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_no_asset_carries_the_perplexity_conditional_or_webfetch(name):
    ported = load_asset(name).lower()

    assert "perplexity" not in ported
    assert "webfetch" not in ported


@pytest.mark.parametrize("name", ("generation", "reflection", "evolution", "cartographer"))
def test_the_grounding_paragraph_was_replaced_not_appended(name):
    """The archived originals branch on Perplexity; the ports carry one grounding line."""
    assert "perplexity" in original(name).lower()
    assert load_asset(name).count("{{GROUNDING}}") == 1


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_every_asset_defers_to_the_enforced_schema(name):
    assert "enforced JSON schema" in load_asset(name)


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_no_asset_still_asks_for_the_old_plaintext_formats(name):
    ported = load_asset(name)

    for marker in ("===HYPOTHESIS===", "===END===", "VERDICT:", "WINNER: <", "SOURCE_DOMAIN:"):
        assert marker not in ported


@pytest.mark.parametrize("name", ASSET_NAMES)
def test_no_asset_keeps_the_subagent_front_matter(name):
    assert not load_asset(name).startswith("---")


def test_grounded_roles_get_the_depth_line_and_toolless_roles_get_the_truth():
    deep = render_system_prompt("generation", grounding_depth="deep")
    shallow = render_system_prompt("reflection", grounding_depth="shallow")
    toolless = render_system_prompt("cartographer", grounding_depth="deep")

    assert "search thoroughly; verify each major claim independently" in deep
    assert "at most 1 search" in shallow
    assert "you have no tools on this call" in toolless
    assert "{{GROUNDING}}" not in deep + shallow + toolless


def test_the_overview_role_uses_the_meta_review_instructions():
    assert render_system_prompt("overview") == render_system_prompt("meta_review")
    assert "== MODE: overview ==" in render_system_prompt("overview")


def test_an_unknown_grounding_depth_is_refused():
    with pytest.raises(ValueError, match="unknown grounding depth"):
        render_system_prompt("generation", grounding_depth="exhaustive")


def test_source_guidance_distinguishes_reasoning_from_published_evidence_and_access_limits():
    grounded = render_system_prompt("verification", grounding_depth="shallow")
    assert "at most 1 search" in grounded
    assert "peer-reviewed journal articles" in grounded
    assert "official documentation" in grounded
    assert "Never claim to have read an inaccessible full text" in grounded
    assert "SOURCE SELECTION" not in render_system_prompt("framing")


# --- task prompts -------------------------------------------------------------------------


def test_there_are_three_distinct_exploration_directives():
    assert len(EXPLORATION_DIRECTIVES) == 3
    assert len(set(EXPLORATION_DIRECTIVES)) == 3


def test_generation_prompt_carries_everything_a_shard_needs():
    ctx = RoundContext(
        goal="Why do tardigrades survive vacuum?",
        guidance="Stop proposing desiccation-only accounts.",
        scientist_notes=("Focus on radiation, not desiccation.",),
        context=build_context_block([{"name": "brief.md", "content": "Prior work: none."}]),
        seed=Seed(seed_id="seed-r2", source_domain="glacial hydrology", skeleton="- a reservoir"),
    )

    prompt = generation_prompt(
        ctx,
        count=3,
        directive=EXPLORATION_DIRECTIVES[0],
        existing=[{"hid": "h001", "title": "An existing title", "status": "active"}],
    )

    assert prompt.startswith("ROLE: generation.")
    assert "N: 3" in prompt
    assert "Mechanism level" in prompt
    assert "- h001 | An existing title" in prompt
    assert "Stop proposing desiccation-only accounts." in prompt
    assert "SCIENTIST GUIDANCE (highest priority" in prompt
    assert "Focus on radiation, not desiccation." in prompt
    assert "DIVERGENCE SEED" in prompt and "glacial hydrology" in prompt
    assert "CONTEXT DOCUMENTS" in prompt and "Prior work: none." in prompt


def test_generation_prompt_says_the_pool_is_empty_rather_than_showing_nothing():
    prompt = generation_prompt(RoundContext(goal="g"), count=2, directive="d", existing=[])

    assert "(the pool is empty.)" in prompt
    assert "(none yet — this is the first round.)" in prompt
    assert "SCIENTIST GUIDANCE" not in prompt


def test_ranking_prompt_presents_the_pair_in_the_order_it_is_given():
    first, second = a_hypothesis("h004", "Fourth"), a_hypothesis("h002", "Second")

    prompt = ranking_prompt(
        "goal", first, second, reviews={"h004": a_review("h004"), "h002": a_review("h002")}
    )

    assert prompt.index("HYPOTHESIS 1 (h004)") < prompt.index("HYPOTHESIS 2 (h002)")
    assert "risk of h004" in prompt and "risk of h002" in prompt


def test_ranking_prompt_says_plainly_when_a_side_was_never_reviewed():
    prompt = ranking_prompt("goal", a_hypothesis("h001"), a_hypothesis("h002"), reviews={})

    assert prompt.count("REVIEW: (not reviewed)") == 2


def test_proximity_prompt_lists_id_title_label_and_claim():
    prompt = proximity_prompt("goal", [a_hypothesis("h001", "First")])

    assert "h001 | First | (unlabelled) | Turnover bounds the bloom." in prompt


def test_evolution_prompt_shows_the_top_hypotheses_with_their_reviews():
    prompt = evolution_prompt(
        RoundContext(goal="goal", guidance="Address the measurement risk."),
        count=2,
        top=[a_hypothesis("h003", "Third", elo=1240)],
        reviews={"h003": a_review("h003")},
    )

    assert "N: 2" in prompt
    assert "h003 | Third | Elo 1240" in prompt
    assert "risk of h003" in prompt
    assert "Address the measurement risk." in prompt


def test_cartographer_prompt_shows_the_champions_it_must_diverge_from():
    prompt = cartographer_prompt("goal", [a_hypothesis("h005", "Champion")])

    assert prompt.startswith("ROLE: cartographer.")
    assert "CURRENT CLUSTERS" in prompt
    assert "h005 | Champion |" in prompt


def test_overview_prompt_carries_the_guidance_trajectory_and_the_counts():
    prompt = overview_prompt(
        "goal",
        top=[a_hypothesis("h001", "Leader", elo=1260)],
        standings=[a_hypothesis("h001", "Leader", elo=1260)],
        reviews={"h001": a_review("h001")},
        feedback_history=[{"round": 1, "guidance": "Name the threshold."}],
        counts={"active": 4, "rejected": 2, "archived": 1, "matches": 6},
        rounds_completed=2,
    )

    assert "MODE: overview" in prompt
    assert "2 round(s) completed" in prompt
    assert "4 active, 2 rejected, 1 archived" in prompt
    assert "Round 1: Name the threshold." in prompt


# --- the meta-review truncation rule --------------------------------------------------------


def make_debate(hid_a, hid_b, elo, size=1500):
    return {
        "hid_a": hid_a,
        "hid_b": hid_b,
        "winner": 1,
        "elo_a_after": elo,
        "elo_b_after": elo - 20,
        "debate_md": f"debate-{hid_a}-{hid_b} " + "x" * size,
    }


def test_meta_review_keeps_every_review_and_drops_the_lowest_elo_debates_first():
    reviews = [a_review(f"h{index:03d}", note=f"UNIQUE-NOTE-{index}") for index in range(1, 13)]
    debates = [
        make_debate("h001", "h002", 1400),
        make_debate("h003", "h004", 1300),
        make_debate("h005", "h006", 1100),
    ]

    prompt = meta_review_prompt(
        "goal",
        round=2,
        reviews=reviews,
        debates=debates,
        standings=[a_hypothesis()],
        previous_guidance="Earlier guidance.",
        char_budget=4000,
    )

    for index in range(1, 13):
        assert f"UNIQUE-NOTE-{index}" in prompt, "a review was dropped; reviews are never cut"
        assert f"risk of h{index:03d}" in prompt, "reviews go in with all five fields"
    assert "debate-h001-h002" in prompt, "the strongest debate is kept"
    assert "debate-h005-h006" not in prompt, "the weakest debate is dropped first"
    assert "omitted for length" in prompt
    assert "Earlier guidance." in prompt


def test_meta_review_keeps_every_debate_when_there_is_room():
    debates = [make_debate("h001", "h002", 1400, size=10), make_debate("h003", "h004", 1100, 10)]

    prompt = meta_review_prompt(
        "goal", round=1, reviews=[a_review()], debates=debates, standings=[a_hypothesis()]
    )

    assert "debate-h001-h002" in prompt
    assert "debate-h003-h004" in prompt
    assert "omitted for length" not in prompt


def test_meta_review_says_so_when_no_match_was_judged():
    prompt = meta_review_prompt(
        "goal", round=1, reviews=[a_review()], debates=[], standings=[]
    )

    assert "(no matches were judged this round.)" in prompt
    assert "(no active hypotheses.)" in prompt


# --- context documents ----------------------------------------------------------------------


def test_context_block_is_empty_for_no_documents():
    block = build_context_block([])

    assert not block
    assert block.text == ""


def test_context_block_truncates_at_the_cap_and_reports_what_it_cut():
    docs = [
        {"name": "big.md", "content": "a" * (CONTEXT_CHAR_CAP - 5_000)},
        {"name": "medium.md", "content": "b" * 6_000},
        {"name": "late.md", "content": "c" * 5_000},
    ]

    block = build_context_block(docs)

    assert len(block.text) <= CONTEXT_CHAR_CAP
    # Every document is represented. Under the old greedy first-fit the first document ate
    # the cap and the last was dropped whole; the two that fit their share now arrive
    # intact and only the oversized one is cut.
    assert block.included == ("big.md", "medium.md", "late.md")
    assert block.truncated == ("big.md",)
    assert block.omitted == ()
    assert "[document truncated to fit the context cap]" in block.text
    assert "b" * 6_000 in block.text and "c" * 5_000 in block.text


def test_context_block_reports_the_original_size_of_each_document():
    block = build_context_block([{"name": "brief.md", "text": "hello"}])

    assert "--- brief.md (5 chars) ---" in block.text


def test_reflection_prompt_includes_the_context_block_when_there_is_one():
    block = ContextBlock(text="CONTEXT DOCUMENTS (supplied by the scientist for this run):\nx")

    prompt = reflection_prompt("goal", a_hypothesis(), block)

    assert "HYPOTHESIS h001:" in prompt
    assert "CONTEXT DOCUMENTS" in prompt


def test_repair_suffix_quotes_the_validation_error():
    suffix = repair_suffix("winner must be one of [1, 2], got '1'")

    assert "DID NOT MATCH THE REQUIRED SCHEMA" in suffix
    assert "winner must be one of [1, 2], got '1'" in suffix


# --- the report has to be able to caveat itself -------------------------------------------


def test_the_overview_is_told_what_the_run_lost():
    """Reconstructed from live data, the real overview prompt for run c4566ed2 was 61,208
    characters and contained no occurrence of 'timeout', 'failed', 'incomplete' or 'planned
    rounds'. The run was configured for 3 rounds, completed 1, lost 3 whole steps to
    timeouts, and its report author had no field in which any of that could be expressed."""
    health = (
        "1 of 3 planned round(s) were completed. Steps that failed: round 1 evolution — "
        "timeout after 420s. Rounds that added no new hypotheses: 2. The run ended because "
        "the cost ceiling was reached."
    )

    prompt = overview_prompt("goal", top=[], standings=[], health=health)

    assert "RUN HEALTH" in prompt
    assert "round 1 evolution" in prompt
    assert "Caveats" in prompt, "the instruction has to name where the caveat goes"


def test_an_overview_with_nothing_to_report_says_no_health_section_at_all():
    prompt = overview_prompt("goal", top=[], standings=[], health="")

    assert "RUN HEALTH" not in prompt


def test_never_ranked_variants_are_listed_apart_from_the_ranked_ones():
    """Every run ends with `evolve_top_k` hypotheses created after the final tournament.
    They are never reviewed, never matched, and sit at the default 1200 — and they used to
    be handed to the report as `top`, above ideas that competed and lost."""
    ranked = [{"hid": "h001", "title": "Fought and won", "body_md": "# a", "elo": 1231.9}]
    fresh = [{"hid": "h009", "title": "Never played", "body_md": "# b", "elo": 1200.0}]

    prompt = overview_prompt("goal", top=ranked, standings=ranked + fresh, unranked=fresh)

    assert "UNVERIFIED NEW VARIANTS" in prompt
    top_block = prompt.split("TOP HYPOTHESES")[1].split("UNVERIFIED NEW VARIANTS")[0]
    assert "Fought and won" in top_block
    assert "Never played" not in top_block, "an unplayed idea was presented as a top result"
    # The standings still show the whole pool — what changes is what is called a result.
    assert "Never played" in prompt.split("TOP HYPOTHESES")[0]


def test_a_run_where_nothing_survived_review_says_so_rather_than_ranking_the_rejects():
    rejected = [{"hid": "h001", "title": "Rejected idea", "body_md": "# a", "elo": 1200.0}]

    prompt = overview_prompt("goal", top=rejected, standings=rejected, nothing_survived=True)

    assert "none survived review" in prompt


def test_the_guidance_trajectory_gives_way_before_the_hypotheses_do():
    """A five-round trail is ~46K characters — 47% of the prompt, more than the top five
    hypotheses, their reviews and the standings combined. The overview prompt had no budget
    at all; META_REVIEW_CHAR_BUDGET applied only inside the meta-review."""
    history = [
        {
            "round": number,
            "guidance": (
                "RECURRING ISSUES:\n" + "x" * 6000 + "\n\nWHAT WINS:\n" + "y" * 3000 +
                "\n\nGUIDANCE FOR THE NEXT ROUND:\nAttack the measurement, not the effect."
            ),
        }
        for number in range(1, 6)
    ]
    top = [{"hid": "h001", "title": "The finding", "body_md": "# The finding", "elo": 1250.0}]

    untrimmed = overview_prompt(
        "goal", top=top, standings=top, feedback_history=history, char_budget=10**6
    )
    prompt = overview_prompt(
        "goal", top=top, standings=top, feedback_history=history, char_budget=20_000
    )

    assert OVERVIEW_CHAR_BUDGET == 60_000, "the default the orchestrator passes"
    assert len(untrimmed) > 45_000, "the shape this exists for: trajectory dominates"
    assert len(prompt) < len(untrimmed) / 2
    assert "The finding" in prompt, "the hypotheses are never what gets cut"
    assert "earlier rounds summarised" in prompt
    assert "Attack the measurement, not the effect." in prompt, "the steer itself survives"
    assert prompt.count("RECURRING ISSUES") <= 2, "only the last two rounds stay whole"


def test_the_meta_review_is_told_when_a_step_of_its_round_failed():
    """The one component that could steer the next round away from a failure mode was never
    told there had been one: in c4566ed2's round 2 both generation shards timed out and the
    prompt said '(no reviews this round.)' with no explanation available anywhere in it."""
    health = "0 new hypotheses, 0 new reviews, 4 debates. generation (shard 1/2) failed: timeout."

    prompt = meta_review_prompt(
        "goal", round=2, reviews=[], debates=[], standings=[], round_health=health
    )

    assert "THIS ROUND'S HEALTH" in prompt
    assert "shard 1/2" in prompt


def test_a_healthy_round_carries_no_health_section():
    prompt = meta_review_prompt("goal", round=2, reviews=[], debates=[], standings=[])

    assert "THIS ROUND'S HEALTH" not in prompt


def test_the_debates_that_are_kept_are_the_highest_elo_ones_the_note_claims():
    """The loop used `continue`, so once one debate had been dropped a later, shorter,
    LOWER-Elo one could still be admitted — while the note went on telling the agent that
    the omissions were the lowest-Elo matches."""
    debates = [
        {"hid_a": "h001", "hid_b": "h002", "winner": 1,
         "debate_md": "A" * 3000, "elo_a_after": 1400.0},
        {"hid_a": "h003", "hid_b": "h004", "winner": 1,
         "debate_md": "B" * 3000, "elo_a_after": 1300.0},
        {"hid_a": "h005", "hid_b": "h006", "winner": 1,
         "debate_md": "C" * 100, "elo_a_after": 1200.0},
    ]

    prompt = meta_review_prompt(
        "goal", round=1, reviews=[], debates=debates, standings=[], char_budget=4000
    )

    assert "h001 vs h002" in prompt
    assert "h005 vs h006" not in prompt, "a low-Elo debate slipped in past a dropped one"
    assert "2 further debate(s) omitted" in prompt


# --- every angle gets asked eventually ----------------------------------------------------


def test_the_three_exploration_directives_are_all_distinct_angles():
    assert len(set(EXPLORATION_DIRECTIVES)) == 3
    assert "Adversarial reframe" in EXPLORATION_DIRECTIVES[2]


# --- what the audit found: sections that lied about their own rows ------------------------


def test_a_reviewed_but_unpaired_hypothesis_keeps_its_review_and_is_not_called_unreviewed():
    """`_unranked` asserted that every zero-match row was "created after the last
    tournament — never reviewed, never ranked", but the caller partitions on `matches == 0`
    alone. In run c4566ed2 five of the eight listed rows (h017-h021) carried a completed
    `pass` review; ~12,000 characters of paid critique were dropped from the only document
    the scientist reads, and the report was told they did not exist."""
    reviewed = {"hid": "h017", "title": "Examined, never paired", "body_md": "# a", "elo": 1200.0}
    fresh = {"hid": "h022", "title": "Bred after the tournament", "body_md": "# b", "elo": 1200.0}

    prompt = overview_prompt(
        "goal",
        top=[],
        standings=[reviewed, fresh],
        unranked=[reviewed, fresh],
        reviews={"h017": a_review("h017")},
    )

    assert "REVIEWED BUT NEVER PAIRED" in prompt
    section = prompt.split("REVIEWED BUT NEVER PAIRED")[1].split("UNVERIFIED NEW VARIANTS")[0]
    assert "Examined, never paired" in section
    assert "risk of h017" in section, "the critique the run paid for reaches the report"
    assert "Bred after the tournament" not in section
    assert "never reviewed" not in section
    unverified = prompt.split("UNVERIFIED NEW VARIANTS")[1]
    assert "Bred after the tournament" in unverified
    assert "Examined, never paired" not in unverified


def test_the_overview_states_its_own_top_k_cut_and_summarises_the_rest():
    """`ranked[:5]` was hard-coded, unstated, and sat under a header reading "these played
    matches" - which reads as though the five listed are the ones that played. Nine of run
    c4566ed2's fourteen competed hypotheses reached the report as a standings line only."""
    top = [a_hypothesis(f"h00{n}", f"Top {n}") for n in range(1, 6)]
    rest = [a_hypothesis("h006", "Also competed", elo=1180.0)]

    prompt = overview_prompt(
        "goal",
        top=top,
        also_ranked=rest,
        standings=top + rest,
        reviews={"h006": a_review("h006")},
    )

    assert "the 5 highest-rated of the 6 that played matches" in prompt
    assert "OTHER HYPOTHESES THAT PLAYED MATCHES" in prompt
    tail = prompt.split("OTHER HYPOTHESES THAT PLAYED MATCHES")[1]
    assert "h006 | Also competed" in tail
    assert "review: pass, novelty high" in tail


def test_the_overview_is_shown_the_debates_it_is_asked_to_explain():
    """The report is instructed to say "why it ranked highly" per top hypothesis and was
    given an Elo integer to say it with - `overview_prompt` took no debates at all, so the
    only ranking rationale in the payload was the meta-review's summary of the same
    matches."""
    prompt = overview_prompt(
        "goal",
        top=[a_hypothesis("h001", "Leader")],
        standings=[a_hypothesis("h001", "Leader")],
        debates=[make_debate("h001", "h002", 1400, size=50)],
    )

    assert "MATCH DEBATES" in prompt
    assert "debate-h001-h002" in prompt


def test_the_trajectory_keeps_a_floor_when_the_rest_of_the_prompt_ate_the_budget():
    """The budget was applied to one section while the sections around it were uncapped, so
    on a real run `char_budget - len(fixed)` went negative, clamped to zero, and the
    trajectory was truncated unconditionally however short it was. The recomposed c4566ed2
    prompt is 102,831 characters against a 60,000 budget, and its 35,168 characters of
    guidance - well inside any sane ceiling - were cut anyway."""
    huge = [{"hid": "h001", "title": "Vast", "body_md": "#" + "x" * 80_000, "elo": 1250.0}]
    history = [{"round": 1, "guidance": "RECURRING ISSUES:\nShort and worth keeping."}]

    prompt = overview_prompt("goal", top=huge, standings=huge, feedback_history=history)

    assert TRAJECTORY_FLOOR == 20_000
    assert "Short and worth keeping." in prompt
    assert "earlier rounds summarised" not in prompt


def test_the_standings_list_the_never_played_apart_from_the_competitors():
    """One flat Elo table put never-played rows at the untouched 1200 above veterans that
    competed and lost. The overview path was split for exactly this; meta_review_prompt,
    which synthesises "what wins" off this table, was not."""
    veteran = {"hid": "h003", "title": "Fought and lost", "elo": 1184.7, "matches": 3, "wins": 1}
    newcomer = {"hid": "h017", "title": "Never played", "elo": 1200.0, "matches": 0, "wins": 0}

    prompt = meta_review_prompt(
        "goal", round=3, reviews=[], debates=[], standings=[veteran, newcomer]
    )

    assert "NEVER PLAYED" in prompt
    played = prompt.split("STANDINGS")[1].split("NEVER PLAYED")[0]
    assert "Fought and lost" in played
    assert "Never played" not in played
    assert "not a result" in prompt


def test_generation_is_told_the_hids_its_guidance_names():
    """The FEEDBACK block names hypotheses by hid dozens of times - 34, 53 and 70 mentions
    across c4566ed2's three rounds - while EXISTING listed bare titles, so guidance saying
    "cross h001's option with h004's claim" arrived unresolvable."""
    prompt = generation_prompt(
        RoundContext(goal="g", guidance="Cross h001 with h004."),
        count=1,
        directive="d",
        existing=[
            {"hid": "h001", "title": "Fixed-strike option", "status": "active"},
            {"hid": "h004", "title": "Exception coverage", "status": "rejected"},
        ],
    )

    assert "- h001 | Fixed-strike option" in prompt
    assert "ALREADY TRIED AND SET ASIDE" in prompt
    live = prompt.split("EXISTING")[1].split("ALREADY TRIED AND SET ASIDE")[0]
    assert "h004" not in live, "a rejected idea is not 'already in the pool'"


def test_feedback_says_which_round_it_came_from_and_flags_a_gap():
    """`_latest_guidance` returned the newest entry whatever round it came from and the
    composer headed it "from the last round". A meta-review that fails records nothing, so
    the next round was told stale guidance - WHAT WINS included - was current."""
    fresh = generation_prompt(
        RoundContext(goal="g", guidance="Do this.", guidance_round=2, round=3),
        count=1,
        directive="d",
        existing=[],
    )
    stale = generation_prompt(
        RoundContext(goal="g", guidance="Do this.", guidance_round=1, round=3),
        count=1,
        directive="d",
        existing=[],
    )
    missing = generation_prompt(
        RoundContext(goal="g", guidance="", round=3), count=1, directive="d", existing=[]
    )

    assert "from round 2, the round before this one" in fresh
    assert "from round 1 — note that this is NOT the previous round" in stale
    assert "did not complete" in missing
    assert "Do not assume this is the first round" in missing
    assert "(none yet — this is the first round.)" not in missing


def test_reflection_is_shown_the_parents_it_is_asked_to_judge_novelty_against():
    """h013 (combination of h001 and h004) and h014 (out_of_box from h002) both passed at
    novelty=moderate; proximity gave h013 its own parent's cluster label and archived both
    as duplicates three steps later. The reviewer had never been shown either parent."""
    child = {**a_hypothesis("h013", "The variant"), "operator": "combination"}

    prompt = reflection_prompt(
        "goal",
        child,
        ContextBlock(),
        parents=[a_hypothesis("h001", "First parent"), a_hypothesis("h004", "Second parent")],
        scientist_notes=("Never propose anything requiring a new licence.",),
    )

    assert "LINEAGE" in prompt
    assert "`combination`" in prompt
    assert "h001 | First parent" in prompt
    assert "h004 | Second parent" in prompt
    assert "not novel however well written" in prompt
    assert "SCIENTIST GUIDANCE (highest priority" in prompt
    assert "Never propose anything requiring a new licence." in prompt


def test_evolution_is_shown_the_pool_and_told_which_parent_is_the_outsider():
    """`_parents` deliberately swaps the k-th slot for the best idea outside the leaders'
    clusters, and every row rendered identically under a header reading "TOP HYPOTHESES" -
    so the loop's one structural anti-convergence move arrived as an unexplained lower-Elo
    entry. Evolution was also never told what the pool already held."""
    leader = {**a_hypothesis("h001", "Leader", elo=1231), "cluster": "option-struck control"}
    outsider = {**a_hypothesis("h007", "Outsider", elo=1216), "cluster": "pre-signed exit ROFR"}

    prompt = evolution_prompt(
        RoundContext(goal="g"),
        count=2,
        top=[leader, outsider],
        outsider="h007",
        existing=[{"hid": "h012", "title": "Already here", "status": "active"}],
    )

    assert "cluster: option-struck control" in prompt
    assert "DIFFERENT CLUSTER — included deliberately" in prompt
    assert prompt.count("DIFFERENT CLUSTER") == 1, "only the reserved slot is marked"
    assert "- h012 | Already here" in prompt


def test_a_small_context_document_survives_a_large_one_beside_it():
    """Greedy first-fit gave document 1 the whole cap and dropped 2..5 whole. With five
    200,000-character documents that is 96% of a megabyte gone, and four documents absent
    from every prompt in the run."""
    docs = [
        {"name": "protocol.md", "content": "P" * 200_000},
        {"name": "priors.md", "content": "R" * 800},
        {"name": "notes.md", "content": "N" * 400},
    ]

    block = build_context_block(docs)

    assert block.included == ("protocol.md", "priors.md", "notes.md")
    assert block.omitted == ()
    assert block.truncated == ("protocol.md",)
    assert "R" * 800 in block.text
    assert "N" * 400 in block.text
    assert len(block.text) <= CONTEXT_CHAR_CAP
    assert block.delivery() == {
        "protocol.md": "truncated",
        "priors.md": "full",
        "notes.md": "full",
    }
    assert block.lossy


def test_the_context_block_charges_its_own_headers_against_the_cap():
    """The `--- name (N chars) ---` headers were free, which is why a 20,000-character cap
    produced a 20,141-character block."""
    block = build_context_block([{"name": "one.md", "content": "x" * 50_000}])

    assert len(block.text) <= CONTEXT_CHAR_CAP


def test_proximity_is_shown_the_labels_it_is_asked_to_keep():
    """Four of run c4566ed2's six unchanged round-1 hypotheses had a different cluster
    string by round 3 with no change to their text, because the call never saw the label it
    was replacing."""
    labelled = {
        **a_hypothesis("h001", "First"),
        "cluster": "escrowed acceptance-test take-rate",
    }

    prompt = proximity_prompt("goal", [labelled])

    assert "h001 | First | escrowed acceptance-test take-rate |" in prompt
    assert "current label" in prompt
