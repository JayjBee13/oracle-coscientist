"""Tests for the pure engine core.

These are the only tests in the suite that need no database, no clock and no network:
every function under test is a pure function of its arguments. Where the plan calls for
"property tests" we loop over seeded random populations rather than pull in a new
dependency — the seeds keep failures reproducible.
"""

from __future__ import annotations

import math
import random
from collections import Counter

import pytest

from app.engine import core
from app.engine.core import (
    ABSTAIN_COOLDOWN,
    ABSTAIN_DISABLED,
    ABSTAIN_NO_CLUSTERS,
    ABSTAIN_QUORUM,
    BUDGET_HEADROOM,
    GENERATION_SHARD_SIZE,
    HYPOTHESIS_FIELD_ORDER,
    INITIAL_ELO,
    K_INITIAL,
    K_SETTLED,
    OVERVIEW_RESERVED_CALLS,
    CollapseSnapshot,
    GraftConfig,
    HypRow,
    RunConfig,
    Signals,
    calls_by_role,
    collapse_signals,
    compose_hypothesis_md,
    elo_update,
    estimate_calls,
    estimate_minutes,
    estimate_usd,
    expected_score,
    herfindahl,
    k_for,
    make_pairs,
    meeting_key,
    presentation_order,
    should_fire_graft,
    suggested_budget_calls,
    winner_side,
)
from app.engine.runners import resolve_model_table

# --------------------------------------------------------------------------- elo


def test_expected_score_is_complementary_and_bounded() -> None:
    assert expected_score(1200, 1200) == pytest.approx(0.5)
    assert expected_score(1400, 1200) + expected_score(1200, 1400) == pytest.approx(1.0)
    assert 0.0 < expected_score(1000, 2000) < 0.5
    assert 0.5 < expected_score(2000, 1000) < 1.0


def test_equal_ratings_move_by_half_k() -> None:
    assert elo_update(1200.0, 1200.0, "a", 32) == (1216.0, 1184.0)
    assert elo_update(1200.0, 1200.0, "b", 32) == (1184.0, 1216.0)


def test_winner_gains_and_loser_loses() -> None:
    new_a, new_b = elo_update(1300.0, 1100.0, "b", 32)
    assert new_a < 1300.0
    assert new_b > 1100.0


def test_elo_update_conserves_total_rating() -> None:
    """Property: a match moves rating between the two sides, it never creates any."""
    rng = random.Random(1234)
    for _ in range(500):
        elo_a = round(rng.uniform(800, 1800), 1)
        elo_b = round(rng.uniform(800, 1800), 1)
        winner = rng.choice(("a", "b"))
        k = rng.choice((K_INITIAL, K_SETTLED))
        new_a, new_b = elo_update(elo_a, elo_b, winner, k)
        assert new_a + new_b == pytest.approx(elo_a + elo_b, abs=1e-9)


def test_the_winner_gains_exactly_what_the_loser_loses() -> None:
    """Conservation stated per match: one rating change, applied in both directions."""
    rng = random.Random(5150)
    for _ in range(500):
        elo_a = round(rng.uniform(800, 1800), 1)
        elo_b = round(rng.uniform(800, 1800), 1)
        k = rng.choice((K_INITIAL, K_SETTLED))
        new_a, new_b = elo_update(elo_a, elo_b, rng.choice(("a", "b")), k)
        assert (new_a - elo_a) == pytest.approx(-(new_b - elo_b), abs=1e-9)


def test_elo_update_is_symmetric_under_swapping_sides() -> None:
    """Property: who is called "a" cannot change the outcome."""
    rng = random.Random(99)
    for _ in range(500):
        elo_a = round(rng.uniform(800, 1800), 1)
        elo_b = round(rng.uniform(800, 1800), 1)
        winner = rng.choice(("a", "b"))
        k = rng.choice((K_INITIAL, K_SETTLED))
        mirrored = "b" if winner == "a" else "a"
        new_a, new_b = elo_update(elo_a, elo_b, winner, k)
        swapped_b, swapped_a = elo_update(elo_b, elo_a, mirrored, k)
        assert (new_a, new_b) == (swapped_a, swapped_b)


def test_elo_update_rejects_unknown_winner() -> None:
    with pytest.raises(ValueError):
        elo_update(1200.0, 1200.0, "draw", 32)  # type: ignore[arg-type]


def test_k_decays_only_when_both_sides_are_settled() -> None:
    assert k_for(0, 0) == K_INITIAL
    assert k_for(10, 9) == K_INITIAL
    assert k_for(9, 10) == K_INITIAL
    assert k_for(10, 10) == K_SETTLED
    assert k_for(31, 12) == K_SETTLED


# ------------------------------------------------------------------------ pairing


def _population(
    count: int,
    *,
    matches: int = 5,
    clusters: list[str | None] | None = None,
    elo_step: float = 25.0,
) -> list[HypRow]:
    rows = []
    for i in range(count):
        cluster = clusters[i] if clusters else None
        rows.append(
            HypRow(
                hid=f"h{i + 1:03d}",
                elo=INITIAL_ELO + (count - i) * elo_step,
                matches=matches,
                cluster=cluster,
            )
        )
    return rows


def test_pairs_are_adjacent_in_the_standings() -> None:
    rows = _population(8)
    order = [row.hid for row in sorted(rows, key=lambda r: -r.elo)]
    pairs = make_pairs(rows, per_round=4, rng=random.Random(7))
    assert pairs
    for pair in pairs:
        assert abs(order.index(pair.hid_a) - order.index(pair.hid_b)) == 1


def test_pairs_are_returned_in_standings_order_within_the_pair() -> None:
    rows = _population(6)
    pairs = make_pairs(rows, per_round=3, rng=random.Random(3))
    by_hid = {row.hid: row for row in rows}
    for pair in pairs:
        assert by_hid[pair.hid_a].elo > by_hid[pair.hid_b].elo


def test_same_cluster_hypotheses_are_never_paired() -> None:
    rows = _population(6, clusters=["x", "x", "y", "y", "z", "z"])
    pairs = make_pairs(rows, per_round=6, rng=random.Random(11))
    by_hid = {row.hid: row for row in rows}
    assert pairs
    for pair in pairs:
        assert by_hid[pair.hid_a].cluster != by_hid[pair.hid_b].cluster


def test_unclustered_hypotheses_may_be_paired_with_each_other() -> None:
    rows = _population(4, clusters=[None, None, None, None])
    pairs = make_pairs(rows, per_round=3, rng=random.Random(5))
    assert len(pairs) == 3


def test_newcomers_are_guaranteed_a_slot_even_when_the_plan_is_small() -> None:
    rows = _population(8, matches=9)
    # the lowest-ranked hypothesis is brand new and must still get a match
    rows[-1] = HypRow(hid=rows[-1].hid, elo=rows[-1].elo, matches=0)
    pairs = make_pairs(rows, per_round=1, rng=random.Random(2))
    assert len(pairs) == 1
    assert rows[-1].hid in (pairs[0].hid_a, pairs[0].hid_b)


def test_newcomer_reaches_past_its_neighbour_when_the_neighbour_shares_its_cluster() -> None:
    rows = [
        HypRow(hid="h001", elo=1400.0, matches=9, cluster="alpha"),
        HypRow(hid="h002", elo=1300.0, matches=9, cluster="beta"),
        HypRow(hid="h003", elo=1200.0, matches=0, cluster="beta"),
    ]
    pairs = make_pairs(rows, per_round=1, rng=random.Random(1))
    assert len(pairs) == 1
    assert {pairs[0].hid_a, pairs[0].hid_b} == {"h001", "h003"}


def test_every_newcomer_gets_a_slot_before_any_hypothesis_gets_a_second() -> None:
    """Ranking by least-played alone would spend both slots around one newcomer.

    h002's two adjacent pairs are the cheapest in the pool, so a plan built purely by
    priority takes both and leaves h005 — equally new, but stuck next to a veteran —
    unranked for the round at its starting rating.
    """
    rows = [
        HypRow(hid="h001", elo=1400.0, matches=2),
        HypRow(hid="h002", elo=1350.0, matches=0),
        HypRow(hid="h003", elo=1300.0, matches=2),
        HypRow(hid="h004", elo=1250.0, matches=9),
        HypRow(hid="h005", elo=1200.0, matches=0),
    ]
    for seed in range(10):
        pairs = make_pairs(rows, per_round=2, rng=random.Random(seed))
        ranked = {hid for pair in pairs for hid in (pair.hid_a, pair.hid_b)}
        assert {"h002", "h005"} <= ranked


def test_least_played_pairs_are_planned_first() -> None:
    rows = [
        HypRow(hid="h001", elo=1400.0, matches=8),
        HypRow(hid="h002", elo=1350.0, matches=8),
        HypRow(hid="h003", elo=1300.0, matches=1),
        HypRow(hid="h004", elo=1250.0, matches=1),
    ]
    for seed in range(20):
        pairs = make_pairs(rows, per_round=1, rng=random.Random(seed))
        assert {pairs[0].hid_a, pairs[0].hid_b} == {"h003", "h004"}


def test_least_played_priority_also_orders_a_pool_of_veterans() -> None:
    """No newcomers here, so the ordering is doing the work on its own."""
    rows = [
        HypRow(hid="h001", elo=1400.0, matches=8),
        HypRow(hid="h002", elo=1350.0, matches=8),
        HypRow(hid="h003", elo=1300.0, matches=3),
        HypRow(hid="h004", elo=1250.0, matches=3),
    ]
    for seed in range(20):
        pairs = make_pairs(rows, per_round=1, rng=random.Random(seed))
        assert {pairs[0].hid_a, pairs[0].hid_b} == {"h003", "h004"}


def test_only_active_hypotheses_are_paired() -> None:
    rows = [
        HypRow(hid="h001", elo=1400.0, matches=2),
        HypRow(hid="h002", elo=1300.0, matches=2, status="rejected"),
        HypRow(hid="h003", elo=1200.0, matches=2, status="archived"),
        HypRow(hid="h004", elo=1100.0, matches=2),
    ]
    pairs = make_pairs(rows, per_round=4, rng=random.Random(6))
    assert len(pairs) == 1
    assert {pairs[0].hid_a, pairs[0].hid_b} == {"h001", "h004"}


def test_no_plan_is_possible_below_two_active_hypotheses() -> None:
    assert make_pairs([], per_round=3, rng=random.Random(0)) == []
    assert make_pairs(_population(1), per_round=3, rng=random.Random(0)) == []
    assert make_pairs(_population(4), per_round=0, rng=random.Random(0)) == []


def test_a_single_cluster_leaves_nothing_legal_to_pair() -> None:
    rows = _population(4, clusters=["same"] * 4)
    assert make_pairs(rows, per_round=3, rng=random.Random(0)) == []


def test_no_pair_repeats_within_a_round() -> None:
    """Property: the same two hypotheses are never scheduled twice in one round."""
    for seed in range(60):
        rng = random.Random(seed)
        count = rng.randint(2, 14)
        rows = [
            HypRow(
                hid=f"h{i:03d}",
                elo=round(rng.uniform(1000, 1400), 1),
                matches=rng.randint(0, 12),
                cluster=rng.choice((None, "a", "b", "c")),
            )
            for i in range(count)
        ]
        pairs = make_pairs(rows, per_round=rng.randint(1, 12), rng=rng)
        keys = [meeting_key(pair.hid_a, pair.hid_b) for pair in pairs]
        assert len(keys) == len(set(keys))
        for pair in pairs:
            assert pair.hid_a != pair.hid_b


def test_same_seed_produces_the_same_plan() -> None:
    """Property: pairing is reproducible, so a resumed run can be audited."""
    rows = _population(10)
    for seed in range(20):
        first = make_pairs(rows, per_round=5, rng=random.Random(seed))
        second = make_pairs(rows, per_round=5, rng=random.Random(seed))
        assert first == second


def test_different_seeds_can_produce_different_plans() -> None:
    rows = _population(10)
    plans = {
        tuple((pair.hid_a, pair.hid_b) for pair in make_pairs(rows, 3, random.Random(seed)))
        for seed in range(20)
    }
    assert len(plans) > 1


def test_plan_never_exceeds_the_requested_size() -> None:
    rows = _population(12)
    for per_round in range(1, 15):
        assert len(make_pairs(rows, per_round, random.Random(per_round))) <= per_round


def test_rematches_swap_the_presentation_order() -> None:
    rows = _population(4)
    plain = make_pairs(rows, per_round=3, rng=random.Random(8))
    assert all(pair.swapped is False for pair in plain)

    history = {meeting_key(pair.hid_a, pair.hid_b): 1 for pair in plain}
    rematch = make_pairs(rows, per_round=3, rng=random.Random(8), prior_meetings=history)
    assert all(pair.swapped is True for pair in rematch)

    twice = {key: 2 for key in history}
    third = make_pairs(rows, per_round=3, rng=random.Random(8), prior_meetings=twice)
    assert all(pair.swapped is False for pair in third)


def test_presentation_order_and_winner_mapping_follow_the_swap() -> None:
    rows = _population(2)
    [pair] = make_pairs(rows, per_round=1, rng=random.Random(0))
    assert presentation_order(pair) == (pair.hid_a, pair.hid_b)
    assert winner_side(pair, 1) == "a"
    assert winner_side(pair, 2) == "b"

    history = {meeting_key(pair.hid_a, pair.hid_b): 1}
    [rematch] = make_pairs(rows, per_round=1, rng=random.Random(0), prior_meetings=history)
    assert presentation_order(rematch) == (rematch.hid_b, rematch.hid_a)
    assert winner_side(rematch, 1) == "b"
    assert winner_side(rematch, 2) == "a"
    with pytest.raises(ValueError):
        winner_side(rematch, 3)


def test_meeting_key_is_order_independent() -> None:
    assert meeting_key("h009", "h002") == meeting_key("h002", "h009")


# ----------------------------------------------------------------------- collapse


def _clustered(labels: list[str | None]) -> list[HypRow]:
    return [HypRow(hid=f"h{i:03d}", cluster=label) for i, label in enumerate(labels)]


def test_herfindahl_is_one_when_everything_shares_a_cluster() -> None:
    assert herfindahl(_clustered(["a"] * 5)) == 1.0


def test_herfindahl_approaches_one_over_k_when_evenly_spread() -> None:
    assert herfindahl(_clustered(["a", "b", "c", "d"])) == pytest.approx(0.25)


def test_herfindahl_is_zero_without_clusters_or_hypotheses() -> None:
    assert herfindahl([]) == 0.0
    assert herfindahl(_clustered([None, None])) == 0.0


def test_plateau_and_birth_rate_stay_silent_until_the_window_is_full() -> None:
    graft = GraftConfig(enabled=True, window=3)
    active = _clustered(["a", "b", "c", "d"])
    history = [
        CollapseSnapshot(round=r, n_active=4, n_clusters=4, hhi=0.25, clusters=("a", "b", "c", "d"))
        for r in range(1, 3)
    ]
    signals = collapse_signals(3, active, history, graft)
    assert signals.have_window is False
    assert signals.plateau is False
    assert signals.birth_rate is False
    assert signals.concentration is False
    assert signals.votes == 0


def test_a_static_cluster_history_votes_plateau_and_birth_rate() -> None:
    graft = GraftConfig(enabled=True, window=3)
    active = _clustered(["a", "b", "c", "d"])
    history = [
        CollapseSnapshot(round=r, n_active=4, n_clusters=4, hhi=0.25, clusters=("a", "b", "c", "d"))
        for r in range(1, 4)
    ]
    signals = collapse_signals(4, active, history, graft)
    assert signals.have_window is True
    assert signals.plateau is True
    assert signals.birth_rate is True
    assert signals.born == 0
    assert signals.concentration is False
    assert signals.votes == 2


def test_a_newly_born_cluster_clears_the_birth_rate_signal() -> None:
    graft = GraftConfig(enabled=True, window=3)
    history = [
        CollapseSnapshot(round=r, n_active=4, n_clusters=4, hhi=0.25, clusters=("a", "b", "c", "d"))
        for r in range(1, 4)
    ]
    signals = collapse_signals(4, _clustered(["a", "b", "c", "e"]), history, graft)
    assert signals.born == 1
    assert signals.birth_rate is False


def test_concentration_votes_at_the_threshold_without_any_history() -> None:
    graft = GraftConfig(enabled=True)
    signals = collapse_signals(1, _clustered(["a", "a", "b", "b"]), [], graft)
    assert signals.snapshot.hhi == pytest.approx(0.5)
    assert signals.concentration is True
    assert signals.votes == 1


def test_snapshot_records_the_round_telemetry() -> None:
    signals = collapse_signals(6, _clustered(["a", "a", None]), [], GraftConfig(enabled=True))
    snapshot = signals.snapshot
    assert snapshot.round == 6
    assert snapshot.n_active == 3
    assert snapshot.n_clusters == 1
    assert snapshot.clusters == ("a",)
    assert snapshot.hhi == pytest.approx(0.444, abs=5e-4)
    assert CollapseSnapshot.from_dict(snapshot.to_dict()) == snapshot


def test_graft_config_rejects_impossible_knobs() -> None:
    with pytest.raises(ValueError):
        GraftConfig(window=0)
    with pytest.raises(ValueError):
        GraftConfig(quorum_k=0)
    with pytest.raises(ValueError):
        GraftConfig(cooldown=-1)


# ---------------------------------------------------------------- graft decision


def _signals_after_static_history(
    clusters: list[str | None], graft: GraftConfig, current_round: int = 4
) -> Signals:
    """Signals for a pool whose cluster structure has not moved for `current_round` rounds."""
    labels = tuple(sorted({label for label in clusters if label is not None}))
    history = [
        CollapseSnapshot(
            round=r, n_active=len(clusters), n_clusters=len(labels), hhi=0.0, clusters=labels
        )
        for r in range(1, current_round)
    ]
    return collapse_signals(current_round, _clustered(clusters), history, graft)


def test_graft_abstains_when_clustering_produced_nothing() -> None:
    """The v2 bug: with no clusters S1 and S3 are vacuously true and the graft fired.

    4 of the 12 archived runs skipped proximity entirely, so this is the historical
    default, not a corner case.
    """
    graft = GraftConfig(enabled=True, quorum_k=2)
    signals = _signals_after_static_history([None, None], graft)
    assert signals.snapshot.n_clusters == 0
    assert signals.votes >= 2  # the signals themselves stay honest...
    decision = should_fire_graft(signals, graft, current_round=4, last_fired_round=None)
    assert decision.fire is False  # ...the decision is where the guard lives
    assert decision.abstained_reason == ABSTAIN_NO_CLUSTERS


def test_disabled_graft_never_fires() -> None:
    graft = GraftConfig(enabled=False, quorum_k=1)
    signals = _signals_after_static_history(["a", "a"], graft)
    decision = should_fire_graft(signals, graft, current_round=4, last_fired_round=None)
    assert decision.fire is False
    assert decision.abstained_reason == ABSTAIN_DISABLED


def test_quorum_must_be_reached() -> None:
    graft = GraftConfig(enabled=True, quorum_k=3, window=3)
    signals = collapse_signals(1, _clustered(["a", "a"]), [], graft)
    assert signals.votes == 1
    decision = should_fire_graft(signals, graft, current_round=1, last_fired_round=None)
    assert decision.fire is False
    assert decision.abstained_reason == ABSTAIN_QUORUM


def test_graft_fires_once_quorum_is_met_and_records_its_votes() -> None:
    graft = GraftConfig(enabled=True, quorum_k=2, window=3, cooldown=2)
    signals = _signals_after_static_history(["a", "a"], graft)
    decision = should_fire_graft(signals, graft, current_round=4, last_fired_round=None)
    assert decision.fire is True
    assert decision.abstained_reason is None
    assert decision.votes == signals.votes
    assert decision.votes >= 2
    assert decision.n_clusters == 1
    assert decision.to_dict()["signals"]["concentration"] is True


def test_cooldown_blocks_a_second_fire_until_it_expires() -> None:
    graft = GraftConfig(enabled=True, quorum_k=2, window=3, cooldown=2)
    signals = _signals_after_static_history(["a", "a"], graft)
    blocked = should_fire_graft(signals, graft, current_round=4, last_fired_round=3)
    assert blocked.fire is False
    assert blocked.abstained_reason == ABSTAIN_COOLDOWN
    still_blocked = should_fire_graft(signals, graft, current_round=4, last_fired_round=2)
    assert still_blocked.fire is False
    cooled = should_fire_graft(signals, graft, current_round=4, last_fired_round=1)
    assert cooled.fire is True


# -------------------------------------------------------------------- composition


def test_compose_hypothesis_md_has_the_exact_documented_shape() -> None:
    body = compose_hypothesis_md(
        {
            "title": "Sleep-dependent consolidation of motor skills",
            "claim": "Naps within two hours of practice raise retention.",
            "mechanism": "Spindle density gates synaptic tagging.",
            "novelty": "Prior work looked at overnight sleep only.",
            "test": "Randomise nap vs quiet rest, measure day-7 retention.",
            "assumptions": "Participants keep a stable sleep schedule.",
        }
    )
    assert body == (
        "# Sleep-dependent consolidation of motor skills\n"
        "\n"
        "**Claim:** Naps within two hours of practice raise retention.\n"
        "\n"
        "**Mechanism:** Spindle density gates synaptic tagging.\n"
        "\n"
        "**Novelty:** Prior work looked at overnight sleep only.\n"
        "\n"
        "**Test:** Randomise nap vs quiet rest, measure day-7 retention.\n"
        "\n"
        "**Assumptions:** Participants keep a stable sleep schedule.\n"
    )


def test_compose_hypothesis_md_covers_every_schema_field() -> None:
    assert HYPOTHESIS_FIELD_ORDER == (
        "title",
        "claim",
        "mechanism",
        "novelty",
        "test",
        "assumptions",
    )
    fields = {name: f"value for {name}" for name in HYPOTHESIS_FIELD_ORDER}
    body = compose_hypothesis_md(fields)
    for name in HYPOTHESIS_FIELD_ORDER[1:]:
        assert f"**{name.capitalize()}:** value for {name}" in body


def test_compose_hypothesis_md_trims_whitespace_and_ignores_extra_keys() -> None:
    fields = {name: f"  {name}  " for name in HYPOTHESIS_FIELD_ORDER}
    fields["derived_from"] = ["h001"]
    fields["operator"] = "combination"
    body = compose_hypothesis_md(fields)
    assert body.startswith("# title\n\n**Claim:** claim\n")
    assert "derived_from" not in body


def test_compose_hypothesis_md_falls_back_to_untitled() -> None:
    fields = {name: "x" for name in HYPOTHESIS_FIELD_ORDER}
    fields["title"] = "   "
    assert compose_hypothesis_md(fields).startswith("# untitled\n")


def test_compose_hypothesis_md_names_the_fields_it_is_missing() -> None:
    with pytest.raises(ValueError) as excinfo:
        compose_hypothesis_md({"title": "t", "claim": "c"})
    message = str(excinfo.value)
    assert "mechanism" in message and "assumptions" in message


# ----------------------------------------------------------------------- estimates


def _c3_step_list_calls(rounds: int, batch: int, matches: int, evolve_top_k: int) -> int:
    """Count calls by walking C3's step list literally — the parity oracle.

    Deliberately written as a loop rather than a formula so it can disagree with
    `estimate_calls`; the test below asserts they never do.
    """
    calls = OVERVIEW_RESERVED_CALLS  # reserved at run start
    awaiting_review = 0
    for _round in range(rounds):
        calls += math.ceil(batch / GENERATION_SHARD_SIZE)  # 1. sharded generation
        calls += batch + awaiting_review  # 2. reflection over hypotheses lacking a review
        calls += 1  # 3. proximity
        # 4. collapse check is a pure function; the cartographer is excluded by design
        calls += matches  # 5. ranking
        calls += 1  # 6. evolution
        awaiting_review = evolve_top_k  # ...reviewed at the start of the next round
        calls += 1  # 7. meta-review
    return calls


def test_estimate_calls_matches_the_c3_step_list() -> None:
    for rounds in range(1, 9):
        for batch in range(1, 13):
            for matches in (0, 2, 6, 11):
                for evolve_top_k in (0, 1, 3, 5):
                    config = RunConfig(
                        rounds=rounds,
                        generation_batch=batch,
                        matches_per_round=matches,
                        evolve_top_k=evolve_top_k,
                    )
                    assert estimate_calls(config) == _c3_step_list_calls(
                        rounds, batch, matches, evolve_top_k
                    )


def test_estimate_calls_accepts_a_plain_config_mapping() -> None:
    mapping = {
        "rounds": 2,
        "generation_batch": 6,
        "matches_per_round": 4,
        "evolve_top_k": 3,
        "grounding_depth": "deep",
        "graft": {"enabled": True, "quorum_k": 2},
    }
    assert estimate_calls(mapping) == estimate_calls(RunConfig.from_mapping(mapping))
    assert RunConfig.from_mapping(mapping).graft.enabled is True


def test_estimate_calls_covers_the_guarded_smoke_configuration() -> None:
    """Task 6.2 runs 1 round / batch 3 / 2 matches under a 12-call budget."""
    smoke = RunConfig(rounds=1, generation_batch=3, matches_per_round=2, evolve_top_k=3)
    assert estimate_calls(smoke) == 11
    assert estimate_calls(smoke) <= 12


def test_estimate_calls_never_decreases_when_work_is_added() -> None:
    base = RunConfig(rounds=3, generation_batch=6, matches_per_round=4, evolve_top_k=2)
    baseline = estimate_calls(base)
    for field_name in ("rounds", "generation_batch", "matches_per_round", "evolve_top_k"):
        raised = {**base.to_dict(), field_name: getattr(base, field_name) + 1}
        assert estimate_calls(RunConfig.from_mapping(raised)) > baseline


def test_a_zero_round_run_still_reserves_the_overview() -> None:
    assert estimate_calls(RunConfig(rounds=0)) == OVERVIEW_RESERVED_CALLS


def test_calls_by_role_sums_to_the_call_estimate() -> None:
    """The split has to be exact, not approximately right: it is what the dollar estimate
    weights by, so a role counted twice would inflate the number a scientist budgets from."""
    for rounds in (0, 1, 3, 5):
        for batch in (1, 4, 8):
            for matches in (0, 2, 6):
                for evolved in (0, 3):
                    config = RunConfig(
                        rounds=rounds,
                        generation_batch=batch,
                        matches_per_round=matches,
                        evolve_top_k=evolved,
                    )
                    assert sum(calls_by_role(config).values()) == estimate_calls(config)


def test_calls_by_role_puts_the_evolved_reviews_in_the_round_that_pays_for_them() -> None:
    config = RunConfig(rounds=3, generation_batch=4, matches_per_round=2, evolve_top_k=3)

    counts = calls_by_role(config)

    # Three rounds of four, plus the two later rounds each carrying three evolved in.
    assert counts["reflection"] == 3 * 4 + 2 * 3
    assert counts["proximity"] == counts["evolution"] == counts["meta_review"] == 3
    assert counts["overview"] == OVERVIEW_RESERVED_CALLS


def test_estimate_usd_prices_a_run_at_the_floor_when_no_table_is_given() -> None:
    config = RunConfig(rounds=2, generation_batch=6, matches_per_round=4, evolve_top_k=3)

    low, high = estimate_usd(config)

    assert 0 < low < high
    # Anchored to what an Opus 5 call actually costs, not to a flat guess.
    assert low / estimate_calls(config) == pytest.approx(0.25, abs=0.02)
    assert high / estimate_calls(config) == pytest.approx(0.667, abs=0.02)


def test_estimate_usd_is_weighted_by_which_roles_run_which_model() -> None:
    """The reason the estimate takes a table at all: fable on reflection costs more than
    fable on proximity, because reflection is most of the calls."""
    config = RunConfig(rounds=3, generation_batch=6, matches_per_round=4, evolve_top_k=3)
    light = resolve_model_table("anthropic", "high", overrides={
        role: {"model": "claude-opus-5"}
        for role in ("generation", "reflection", "ranking", "meta_review", "overview")
    })
    heavy = resolve_model_table("anthropic", "high")

    floor = estimate_usd(config, light)
    top = estimate_usd(config, heavy)
    one_cheap_role = estimate_usd(
        config,
        [
            {**row, "model": "fable" if row["role"] == "proximity" else row["model"]}
            for row in light
        ],
    )

    assert top[0] > floor[0] and top[1] > floor[1]
    assert top[1] < floor[1] * 2, "not every role moved to fable, so it is not a flat doubling"
    assert one_cheap_role[1] - floor[1] < top[1] - floor[1], (
        "upgrading three proximity calls cannot cost what upgrading the tournament does"
    )


def test_estimate_usd_of_a_run_that_does_nothing_is_the_reserved_overview() -> None:
    low, high = estimate_usd(RunConfig(rounds=0))

    assert low > 0, "the report is still written, and still costs something"
    assert high == pytest.approx(OVERVIEW_RESERVED_CALLS * 0.667, abs=0.02)


def test_estimate_minutes_without_a_table_is_unchanged_by_effort() -> None:
    """A caller that has not resolved a table yet is not punished with a number invented
    from a tier it never chose."""
    config = RunConfig(rounds=3, generation_batch=6, matches_per_round=4)

    assert estimate_minutes(config) == estimate_minutes(config, [])


def test_estimate_minutes_grows_with_effort() -> None:
    """Effort buys thinking with latency. Now that minutes are the scarce resource and
    `wall_clock_minutes` is a number somebody has to pick, an estimate that ignored the
    difference between a table at medium and one at high would set them up for exactly the
    failure the dollar ceiling used to cause."""
    config = RunConfig(rounds=3, generation_batch=6, matches_per_round=4)
    cheap = [{"role": row["role"], "effort": "medium"} for row in resolve_model_table()]
    default = resolve_model_table("anthropic", "high")

    assert estimate_minutes(config, default) > estimate_minutes(config, cheap)


def test_effort_on_the_mechanical_role_barely_moves_the_estimate() -> None:
    """Proximity is one call a round; reflection is one per hypothesis. Raising the cheap
    one is not the same decision as raising the expensive one, and the estimate says so."""
    config = RunConfig(rounds=3, generation_batch=6, matches_per_round=4)
    base = [{"role": row["role"], "effort": "medium"} for row in resolve_model_table()]
    proximity = [
        {**row, "effort": "max" if row["role"] == "proximity" else row["effort"]} for row in base
    ]
    reflection = [
        {**row, "effort": "max" if row["role"] == "reflection" else row["effort"]} for row in base
    ]

    baseline = estimate_minutes(config, base)[1]
    assert estimate_minutes(config, proximity)[1] - baseline < (
        estimate_minutes(config, reflection)[1] - baseline
    )


def test_suggested_budget_adds_the_planned_headroom() -> None:
    config = RunConfig(rounds=2, generation_batch=6, matches_per_round=4, evolve_top_k=3)
    assert suggested_budget_calls(config) == math.ceil(estimate_calls(config) * BUDGET_HEADROOM)
    assert suggested_budget_calls(config) > estimate_calls(config)


def test_estimate_minutes_returns_an_ordered_positive_bracket() -> None:
    lo, hi = estimate_minutes(RunConfig(rounds=3, generation_batch=6, matches_per_round=4))
    assert isinstance(lo, int) and isinstance(hi, int)
    assert 1 <= lo < hi


def test_estimate_minutes_grows_with_the_work_and_with_grounding_depth() -> None:
    small = estimate_minutes(RunConfig(rounds=1, generation_batch=3, matches_per_round=2))
    large = estimate_minutes(RunConfig(rounds=6, generation_batch=12, matches_per_round=8))
    assert large[0] > small[0] and large[1] > small[1]

    shallow = estimate_minutes(RunConfig(rounds=3, grounding_depth="shallow"))
    deep = estimate_minutes(RunConfig(rounds=3, grounding_depth="deep"))
    assert deep[1] > shallow[1]


def test_run_config_round_trips_through_its_mapping() -> None:
    config = RunConfig(
        rounds=4,
        generation_batch=9,
        matches_per_round=7,
        evolve_top_k=2,
        budget_calls=200,
        budget_usd=7.5,
        grounding_depth="deep",
        graft=GraftConfig(enabled=True, quorum_k=3, window=4, cooldown=1),
        model_tier="max",
        runner="demo",
    )
    assert RunConfig.from_mapping(config.to_dict()) == config
    assert config.to_dict()["graft"]["quorum_k"] == 3


# --- a shard's own numbering never reaches the judge --------------------------------------


def test_a_self_declared_rank_is_stripped_out_of_the_stored_title():
    """Generation shard 2 of run c4566ed2 emitted titles beginning `Rank 1:`, `Rank 2:` and
    `Rank 3:`. Those strings went into the body, and the body goes into the ranking prompt —
    beside an instruction telling the judge to ignore scores and position. Three of that
    run's six hypotheses carried the claim and three did not."""
    for raw, expected in [
        ("Rank 1: Vertical exception-corpus federation", "Vertical exception-corpus federation"),
        ("rank 2 — Correlation-aware guarantee book", "Correlation-aware guarantee book"),
        ("Hypothesis #3. Congestion rents", "Congestion rents"),
        ("Idea 1) Sediment turnover", "Sediment turnover"),
    ]:
        body = compose_hypothesis_md({**_FIELDS, "title": raw})
        assert body.splitlines()[0] == f"# {expected}"


def test_a_title_that_merely_contains_a_number_is_left_alone():
    """Only a leading self-rank goes. `Rank correlation…` is the hypothesis, not a label."""
    for title in ("Rank correlation drives the effect", "3 mechanisms compete", "Top 5 lakes"):
        body = compose_hypothesis_md({**_FIELDS, "title": title})
        assert body.splitlines()[0] == f"# {title}"


_FIELDS = {
    "title": "t",
    "claim": "c",
    "mechanism": "m",
    "novelty": "n",
    "test": "t",
    "assumptions": "a",
}


# --- pairing spends slots on the ideas that need them -------------------------------------


def test_a_round_does_not_give_one_idea_two_matches_while_a_newcomer_sits_out():
    """`least_played` read the match counts frozen at the start of the round, so slots
    already handed out this round were invisible to it. Real case: run 146282fa round 2 gave
    both of its matches to h005 while h001 and h003 were still newcomers and played none."""
    standings = [
        HypRow(hid="h001", elo=1216.0, matches=1),
        HypRow(hid="h003", elo=1184.0, matches=1),
        HypRow(hid="h005", elo=1200.0, matches=1),
        HypRow(hid="h007", elo=1200.0, matches=1),
    ]

    pairs = make_pairs(standings, 2, random.Random("seed"))

    appearances = Counter(hid for pair in pairs for hid in (pair.hid_a, pair.hid_b))
    assert len(pairs) == 2
    assert max(appearances.values()) == 1, f"one idea took two of two slots: {appearances}"


def test_slots_still_go_to_the_least_played_when_the_pool_is_uneven():
    standings = [
        HypRow(hid="h001", elo=1240.0, matches=4),
        HypRow(hid="h002", elo=1230.0, matches=4),
        HypRow(hid="h003", elo=1220.0, matches=0),
        HypRow(hid="h004", elo=1210.0, matches=0),
    ]

    pairs = make_pairs(standings, 2, random.Random("seed"))
    played = {hid for pair in pairs for hid in (pair.hid_a, pair.hid_b)}

    assert {"h003", "h004"} <= played, "the never-played ideas must get the round's slots"


# --- what the audit found in the pure math ------------------------------------------------


def test_presentation_order_is_anchored_on_the_pair_not_on_which_side_is_a():
    """`swapped` was `meetings % 2` against whichever side the plan put first, and the plan
    puts the better-placed side first *at that round's planning time*. When two hypotheses
    swap places in the standings between meetings the a/b order flips too, so the flag that
    exists to reverse the presentation order restored it instead and the judge saw the
    identical order twice - which is the position bias the mechanism is for. Two of the 30
    rematches in the live database are in that state (`b41853cd` rounds 1/2)."""
    first = core.Pair("h001", "h002", swapped=core.presentation_swap("h001", "h002", 0))
    # Round 2: h002 has overtaken h001, so the plan names it side a.
    second = core.Pair("h002", "h001", swapped=core.presentation_swap("h002", "h001", 1))

    assert core.presentation_order(first) == ("h001", "h002")
    assert core.presentation_order(second) == ("h002", "h001")
    assert core.presentation_order(first) != core.presentation_order(second)
    # And the winner still resolves against the pair's own a/b, not against the position.
    assert core.winner_side(second, 1) == "a"
    assert core.winner_side(second, 2) == "b"


def test_a_rematch_still_swaps_when_the_sides_keep_their_order():
    stable_first = core.Pair("h001", "h002", swapped=core.presentation_swap("h001", "h002", 0))
    stable_again = core.Pair("h001", "h002", swapped=core.presentation_swap("h001", "h002", 1))

    assert core.presentation_order(stable_first) == ("h001", "h002")
    assert core.presentation_order(stable_again) == ("h002", "h001")


def test_the_collapse_snapshot_records_how_much_of_the_pool_carried_a_label():
    """An unlabelled row counts in HHI's denominator and forms no cluster, so the reading
    gets *quieter* the more of the pool the clustering call missed. Run c4566ed2's round 2
    read `n_active 12, n_clusters 6, hhi 0.042` with six rows unlabelled; had those six
    shared the six existing labels it would have read 0.167."""
    rows = [core.HypRow(hid=f"h{n:03d}", cluster=f"c{n}" if n <= 6 else None) for n in range(1, 13)]

    signals = core.collapse_signals(2, rows, [], core.GraftConfig(enabled=True))

    assert signals.snapshot.n_active == 12
    assert signals.snapshot.n_labelled == 6
    assert signals.snapshot.coverage == 0.5
    assert signals.to_dict()["coverage"] == 0.5


def test_a_half_labelled_pool_abstains_rather_than_voting_on_its_own_incompleteness():
    rows = [core.HypRow(hid=f"h{n:03d}", cluster=f"c{n}" if n <= 6 else None) for n in range(1, 13)]
    graft = core.GraftConfig(enabled=True, quorum_k=1, hhi_threshold=0.0)

    signals = core.collapse_signals(2, rows, [], graft)
    decision = core.should_fire_graft(signals, graft, 2, None)

    assert not decision.fire
    assert decision.abstained_reason == core.ABSTAIN_PARTIAL_CLUSTERS


def test_a_fully_labelled_pool_is_still_allowed_to_fire():
    rows = [core.HypRow(hid=f"h{n:03d}", cluster="one idea") for n in range(1, 7)]
    graft = core.GraftConfig(enabled=True, quorum_k=1, hhi_threshold=0.5)

    signals = core.collapse_signals(2, rows, [], graft)
    decision = core.should_fire_graft(signals, graft, 2, None)

    assert signals.snapshot.coverage == 1.0
    assert signals.concentration, "six ideas under one label is a concentrated pool"
    assert decision.fire


def test_a_snapshot_written_before_coverage_existed_does_not_claim_one():
    old = core.CollapseSnapshot.from_dict(
        {"round": 2, "n_active": 12, "n_clusters": 6, "hhi": 0.042, "clusters": ["a"]}
    )

    assert old.n_labelled == 12
    assert old.coverage == 1.0, "the reading it produced already assumed a complete map"


def test_a_shards_self_declared_rank_is_stripped_from_the_claim_as_well_as_the_title():
    """`_without_self_rank` guarded the title only, so run c4566ed2's h016-h018 carried
    clean titles and claims beginning "Rank 1 of this shard." - and the claim is the line
    `claim_of` lifts into the proximity list and the one the ranking judge reads first."""
    body = core.compose_hypothesis_md(
        {
            "title": "Rank 2: A real title",
            "claim": "Rank 1 of this shard. The mechanism is what matters.",
            "mechanism": "m",
            "novelty": "n",
            "test": "t",
            "assumptions": "a",
        }
    )

    assert body.startswith("# A real title")
    assert "**Claim:** The mechanism is what matters." in body
    assert "Rank 1" not in body

    numbered = core.compose_hypothesis_md(
        {
            "title": "T",
            "claim": "Rank 1 of 3. Still the mechanism.",
            "mechanism": "m",
            "novelty": "n",
            "test": "t",
            "assumptions": "a",
        }
    )
    assert "**Claim:** Still the mechanism." in numbered
