"""The four diversity mechanisms, each pinned against the defect that produced it.

Every test here exists because a real run narrowed and nothing in the engine noticed. The
owner asked a deliberately wide question and got six hypotheses that were all one kind of
answer; these are the parts of the loop that were supposed to prevent that and could not:

* the exploration directive that attacks the framing, which was unreachable in round 1 at
  every batch size below 7 — the setting 25 of 29 real runs used;
* the diversity telemetry, which was computed only by runs that had already enabled the
  intervention it feeds, i.e. by none of them;
* evolution's parent selection, which bred exclusively from whatever the tournament had
  already promoted;
* the coarse family reading of a round's new ideas, which is the one measurement that
  separated the collapsed run from the healthy one in the replay.
"""

from __future__ import annotations

import math

import pytest

from app.engine.core import (
    CONCENTRATED_POOL_SHARE,
    GENERATION_SHARD_SIZE,
    top_share,
)
from app.engine.orchestrator import Orchestrator
from app.engine.prompts import (
    DIRECTIVE_ORDER,
    EXPLORATION_DIRECTIVES,
    ContextBlock,
    RoundContext,
    evolution_prompt,
    exploration_directive,
)

BATCHES = range(3, 13)
REFRAME = 2
"""The index of the adversarial reframe, the only directive that attacks the framing."""


def shards_for(batch: int) -> int:
    return math.ceil(batch / GENERATION_SHARD_SIZE)


def directives_in(batch: int, round_number: int) -> list[int]:
    """Which directive each shard of that round claims, as indices into the tuple."""
    return [
        EXPLORATION_DIRECTIVES.index(exploration_directive(index, round_number))
        for index in range(shards_for(batch))
    ]


# --- task 3: the reframe has to be reachable in the round that sets the ceiling ------------


@pytest.mark.parametrize("batch", BATCHES)
def test_round_one_reaches_the_adversarial_reframe_whenever_it_has_two_shards(batch):
    """The defect exactly: `(index + number - 1) % 3` gave a two-shard round 1 directives
    0 and 1 — two scale settings inside one frame — and never the reframe."""
    claimed = directives_in(batch, 1)

    if shards_for(batch) >= 2:
        assert REFRAME in claimed, (
            f"batch {batch} has {shards_for(batch)} shards in round 1 and still never "
            "attacks the framing"
        )
    else:
        assert claimed == [0], "a one-shard round starts on the mechanism level"


@pytest.mark.parametrize("batch", BATCHES)
def test_three_rounds_cover_every_angle_at_every_batch_size(batch):
    covered = {index for round_number in (1, 2, 3) for index in directives_in(batch, round_number)}

    assert covered == {0, 1, 2}, f"batch {batch} never reaches directives {{0,1,2}} - {covered}"


@pytest.mark.parametrize("batch", BATCHES)
@pytest.mark.parametrize("round_number", (1, 2, 3))
def test_a_shard_never_duplicates_another_shards_angle_until_all_three_are_taken(
    batch, round_number
):
    claimed = directives_in(batch, round_number)
    expected = min(len(EXPLORATION_DIRECTIVES), len(claimed))

    assert len(set(claimed)) == expected, "shards repeat an angle before the third is used"


def test_the_launch_default_batch_affords_all_three_angles_in_every_round():
    from app.engine.core import RunConfig

    assert shards_for(RunConfig().generation_batch) >= len(EXPLORATION_DIRECTIVES)


def test_the_claim_order_is_a_permutation_that_puts_the_reframe_second():
    assert sorted(DIRECTIVE_ORDER) == [0, 1, 2]
    assert DIRECTIVE_ORDER[1] == REFRAME


def test_each_directive_tells_a_strategy_goal_what_to_do_differently():
    """The three angles read as three angles on a scientific question and as two on a
    strategy one: 'mechanism level' and 'system level' both ask for the machinery of the
    same kind of answer at different sizes. Each directive now carries a clause for goals
    that ask what to do, and the system-level one is explicit that a larger version of the
    mechanism-level answer is not a second angle."""
    assert all("what to do" in directive for directive in EXPLORATION_DIRECTIVES)
    assert "not the binding one" in EXPLORATION_DIRECTIVES[REFRAME]
    assert "larger size" in EXPLORATION_DIRECTIVES[1]


# --- task 5: evolution breeds from more than the leading cluster ---------------------------


def row(hid: str, elo: float, cluster: str | None) -> dict:
    return {"hid": hid, "elo": elo, "cluster": cluster, "title": hid, "body_md": f"# {hid}"}


def test_a_pool_concentrated_in_one_cluster_still_yields_parents_from_two():
    ranked = [
        row("h001", 1400, "take-rate"),
        row("h002", 1380, "take-rate"),
        row("h003", 1360, "take-rate"),
        row("h004", 1340, "take-rate"),
        row("h005", 1100, "services"),
    ]

    parents = Orchestrator._parents(ranked, 3)

    assert [p["hid"] for p in parents] == ["h001", "h002", "h005"]
    assert len({p["cluster"] for p in parents}) == 2, "raw top-k would have been all take-rate"


def test_the_outsider_is_the_highest_elo_one_not_merely_the_first_found():
    ranked = [
        row("h001", 1400, "a"),
        row("h002", 1380, "a"),
        row("h003", 1200, "b"),
        row("h004", 1300, "b"),
    ]
    ranked.sort(key=lambda item: (-item["elo"], item["hid"]))

    parents = Orchestrator._parents(ranked, 2)

    assert [p["hid"] for p in parents] == ["h001", "h004"]


def test_one_cluster_and_nothing_else_falls_back_to_raw_top_k():
    ranked = [row(f"h00{index}", 1400 - index, "only") for index in range(1, 5)]

    assert [p["hid"] for p in Orchestrator._parents(ranked, 3)] == ["h001", "h002", "h003"]


def test_an_unlabelled_pool_is_raw_top_k_because_every_row_is_its_own_cluster():
    ranked = [row(f"h00{index}", 1400 - index, None) for index in range(1, 6)]

    assert [p["hid"] for p in Orchestrator._parents(ranked, 3)] == ["h001", "h002", "h003"]


@pytest.mark.parametrize("top_k", (0, 1))
def test_a_tiny_slate_has_no_slot_to_reserve(top_k):
    ranked = [row("h001", 1400, "a"), row("h002", 1300, "b")]

    assert len(Orchestrator._parents(ranked, top_k)) == top_k


def test_concentration_is_measured_against_the_whole_pool_not_the_labelled_part():
    """Two labels out of twelve is an unlabelled pool, not a collapsed one, and must not
    demand a divergent variant on the strength of two rows."""
    sparse = [row("h001", 1400, "a"), row("h002", 1300, "a")] + [
        row(f"h1{index:02d}", 1200, None) for index in range(10)
    ]

    assert Orchestrator._pool_concentration(sparse) is None
    assert Orchestrator._pool_concentration([]) is None


def test_a_narrow_pool_reports_its_share():
    ranked = [row(f"h00{index}", 1300, "one") for index in range(1, 4)] + [
        row("h004", 1200, "other")
    ]

    concentration = Orchestrator._pool_concentration(ranked)

    assert concentration == pytest.approx(0.75)
    assert concentration >= CONCENTRATED_POOL_SHARE


def evolution_text(concentration: float | None) -> str:
    return evolution_prompt(
        RoundContext(goal="Reach escape velocity", context=ContextBlock()),
        count=3,
        top=[row("h001", 1400, "a")],
        concentration=concentration,
    )


def test_a_concentrated_pool_demands_a_divergent_operator():
    prompt = evolution_text(0.8)

    assert "DIVERGENCE REQUIREMENT" in prompt
    assert "80%" in prompt
    assert "out_of_box" in prompt and "simplification" in prompt


def test_a_wide_pool_says_nothing_about_operators():
    assert "DIVERGENCE REQUIREMENT" not in evolution_text(None)


# --- task 6: the family reading of one round's new ideas -----------------------------------


def test_top_family_share_over_a_round_with_known_labels():
    """The collapsed run's round 2 shape: seven of twelve ideas one kind of thing."""
    labels = ["b2b tooling"] * 7 + ["consumer app", "content", "services", "licensing", "fund"]

    reading = top_share(labels)

    assert reading.label == "b2b tooling"
    assert reading.share == pytest.approx(7 / 12, abs=0.001)
    assert reading.share >= CONCENTRATED_POOL_SHARE
    assert reading.n_labelled == 12
    assert reading.n_total == 12
    assert dict(reading.counts)["b2b tooling"] == 7


def test_a_healthy_round_reads_below_the_threshold():
    labels = ["a", "b", "c", "d", "a", "e", "f", "g", "h"]

    reading = top_share(labels)

    assert reading.share == pytest.approx(2 / 9, abs=0.001)
    assert reading.share < CONCENTRATED_POOL_SHARE


def test_a_missing_label_shrinks_the_denominator_and_stays_visible():
    """A share of 1.00 over one label out of six is an unlabelled round, not a collapsed
    one — so the population it was drawn from is recorded beside the share."""
    reading = top_share(["services", None, None, None, None, None])

    assert reading.share == pytest.approx(1.0)
    assert (reading.n_labelled, reading.n_total) == (1, 6)


def test_a_round_with_no_labels_at_all_reads_as_nothing_rather_than_as_collapse():
    reading = top_share([None, None])

    assert reading.label is None
    assert reading.share == 0.0
    assert reading.n_labelled == 0 and reading.n_total == 2
    assert reading.to_dict()["counts"] == {}


def test_a_tie_breaks_deterministically_on_the_label():
    assert top_share(["beta", "alpha", "beta", "alpha"]).label == "alpha"


def test_an_empty_round_is_not_a_division_by_zero():
    reading = top_share([])

    assert reading.share == 0.0 and reading.n_total == 0
