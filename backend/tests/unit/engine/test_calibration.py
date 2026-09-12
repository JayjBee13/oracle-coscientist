"""Tests for the graft calibration replay.

The unit under test is the *reconstruction*: turning an archived `state.json` — which only
ever stored each hypothesis's final cluster label and final status — back into the
per-round active sets that `collapse_signals` needs. Everything downstream (the signals,
the fire decision) belongs to `engine.core` and is tested there; here we only check that
calibration feeds it the right input and reports the right things about the output.

Fixtures are checked-in files, never the owner's archive: `healthy_run.json` (a pool that
keeps opening regions, carrying v2's own recorded telemetry so reconstruction can be scored
against ground truth), `null_cluster_run.json` (proximity never ran — the abstain path) and
`collapsed_run.json` (a synthetic positive control, because the real corpus has none).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.engine.calibration import (
    COLLAPSED,
    DEGENERATE,
    HEALTHY,
    RECOMMENDED,
    RUN_LABELS,
    grid_search,
    legacy_v2_fires,
    load_archive,
    load_state,
    reconstruct_active,
    reconstruct_run,
    reconstruction_fidelity,
    replay_run,
)
from app.engine.core import (
    ABSTAIN_COOLDOWN,
    ABSTAIN_DISABLED,
    ABSTAIN_NO_CLUSTERS,
    ABSTAIN_QUORUM,
    GraftConfig,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "calibration"


@pytest.fixture
def healthy() -> dict:
    return load_state(FIXTURES / "healthy_run.json")


@pytest.fixture
def null_clusters() -> dict:
    return load_state(FIXTURES / "null_cluster_run.json")


@pytest.fixture
def collapsed() -> dict:
    return load_state(FIXTURES / "collapsed_run.json")


# ------------------------------------------------------------------ reconstruction


def test_load_state_reads_utf8_and_returns_the_engine_shape(healthy: dict) -> None:
    assert healthy["run_id"] == "fixture-healthy"
    assert isinstance(healthy["hypotheses"], dict)


def test_active_set_grows_with_created_iter(healthy: dict) -> None:
    hyps = healthy["hypotheses"]
    assert [row.hid for row in reconstruct_active(hyps, 1)] == ["h001", "h002", "h003"]
    assert [row.hid for row in reconstruct_active(hyps, 2)] == [
        "h001",
        "h002",
        "h003",
        "h004",
        "h005",
    ]


def test_rejected_hypotheses_are_never_active(healthy: dict) -> None:
    """h006 was rejected by reflection in the round it was born, so it never competed."""
    hyps = healthy["hypotheses"]
    every_round = {row.hid for r in (1, 2, 3) for row in reconstruct_active(hyps, r)}
    assert "h006" not in every_round


def test_evolved_hypotheses_land_the_round_after_they_are_created(healthy: dict) -> None:
    """Evolution runs after the cluster step, so its offspring are absent from that round.

    h008 carries `created_iter: 2` and a parent, so it is present from round 3 — the
    correction that makes reconstruction agree with v2's recorded telemetry.
    """
    hyps = healthy["hypotheses"]
    assert "h008" not in {row.hid for row in reconstruct_active(hyps, 2)}
    assert "h008" in {row.hid for row in reconstruct_active(hyps, 3)}


def test_reconstruction_carries_the_cluster_label(healthy: dict) -> None:
    by_hid = {row.hid: row for row in reconstruct_active(healthy["hypotheses"], 3)}
    assert by_hid["h001"].cluster == "c-alpha"
    assert by_hid["h008"].cluster == "c-alpha"


def test_reconstruct_run_produces_one_round_per_iteration(healthy: dict) -> None:
    run = reconstruct_run(healthy, version="v2")
    assert [r.round for r in run.rounds] == [1, 2, 3]
    assert run.run_id == "fixture-healthy"
    assert run.clustered is True


def test_reconstruction_reproduces_the_recorded_telemetry(healthy: dict) -> None:
    """The claim the whole replay rests on, checked against v2's own logged numbers."""
    run = reconstruct_run(healthy, version="v2")
    for round_data in run.rounds:
        assert round_data.recorded is not None
        assert round_data.snapshot.n_active == round_data.recorded.n_active
        assert round_data.snapshot.n_clusters == round_data.recorded.n_clusters
        assert round_data.snapshot.hhi == round_data.recorded.hhi


def test_fidelity_scores_reconstruction_against_recorded_rounds(healthy: dict) -> None:
    [row] = reconstruction_fidelity([reconstruct_run(healthy, version="v2")])
    assert row.rounds_compared == 3
    assert row.n_active_exact == 3
    assert row.signals_exact == 3


def test_runs_without_recorded_telemetry_are_not_scored(collapsed: dict) -> None:
    """v1 predates the collapse block entirely; there is nothing to score against."""
    run = reconstruct_run(collapsed, version="v1", label=COLLAPSED)
    assert all(round_data.recorded is None for round_data in run.rounds)
    assert reconstruction_fidelity([run]) == []


# ------------------------------------------------------------------------ labelling


def test_a_run_with_no_clusters_anywhere_is_labelled_degenerate(null_clusters: dict) -> None:
    assert reconstruct_run(null_clusters, version="v2").label == DEGENERATE


def test_a_clustered_run_defaults_to_healthy(healthy: dict) -> None:
    assert reconstruct_run(healthy, version="v2").label == HEALTHY


def test_collapse_is_never_inferred_only_asserted(collapsed: dict) -> None:
    """Auto-labelling may call a run degenerate or healthy; only a human calls it collapsed."""
    assert reconstruct_run(collapsed, version="v1").label == HEALTHY
    assert reconstruct_run(collapsed, version="v1", label=COLLAPSED).label == COLLAPSED


def test_every_archived_run_carries_a_written_rationale() -> None:
    assert len(RUN_LABELS) == 12
    for run_id, entry in RUN_LABELS.items():
        assert entry.label in (HEALTHY, COLLAPSED, DEGENERATE), run_id
        assert len(entry.rationale) > 40, run_id


# --------------------------------------------------------------------------- replay


def test_disabled_graft_abstains_on_every_round(collapsed: dict) -> None:
    run = reconstruct_run(collapsed, version="v1", label=COLLAPSED)
    result = replay_run(run, GraftConfig(enabled=False))
    assert result.fired_rounds == ()
    assert set(result.abstained_reasons) == {ABSTAIN_DISABLED}


def test_a_run_with_no_clusters_always_abstains_on_the_cluster_guard(null_clusters: dict) -> None:
    """The abstain the archived engine lacked: four of twelve real runs are in this state."""
    run = reconstruct_run(null_clusters, version="v2")
    for quorum in (1, 2, 3):
        for window in (2, 3, 4):
            for cooldown in (1, 2, 3):
                config = GraftConfig(
                    enabled=True, quorum_k=quorum, window=window, cooldown=cooldown
                )
                result = replay_run(run, config)
                assert result.fired_rounds == ()
                assert set(result.abstained_reasons) == {ABSTAIN_NO_CLUSTERS}


def test_the_synthetic_positive_control_fires_once_the_window_fills(collapsed: dict) -> None:
    run = reconstruct_run(collapsed, version="v1", label=COLLAPSED)
    result = replay_run(run, GraftConfig(enabled=True, quorum_k=3, window=3, cooldown=2))
    assert result.fired_rounds == (4,)
    assert result.first_fire_round == 4


def test_a_narrower_window_fires_sooner(collapsed: dict) -> None:
    run = reconstruct_run(collapsed, version="v1", label=COLLAPSED)
    result = replay_run(run, GraftConfig(enabled=True, quorum_k=3, window=2, cooldown=2))
    assert result.first_fire_round == 3


def test_cooldown_suppresses_the_round_after_a_fire(collapsed: dict) -> None:
    run = reconstruct_run(collapsed, version="v1", label=COLLAPSED)
    result = replay_run(run, GraftConfig(enabled=True, quorum_k=3, window=3, cooldown=2))
    assert result.decisions[-1].abstained_reason == ABSTAIN_COOLDOWN


def test_quorum_abstains_when_the_pool_stays_diverse(healthy: dict) -> None:
    run = reconstruct_run(healthy, version="v2")
    result = replay_run(run, GraftConfig(enabled=True, quorum_k=2, window=2, cooldown=2))
    assert result.fired_rounds == ()
    assert ABSTAIN_QUORUM in result.abstained_reasons


# ------------------------------------------------------------------- legacy replay


def test_legacy_replay_reproduces_the_archived_false_fire(null_clusters: dict) -> None:
    """v2 shipped without the cluster guard and logged `last_fired_iter: 4` on this shape.

    Reproducing that exact round from the reconstruction alone is the end-to-end check
    that the replay models the archived engine faithfully.
    """
    run = reconstruct_run(null_clusters, version="v2")
    config = GraftConfig(enabled=True, quorum_k=1, window=3, cooldown=2)
    assert legacy_v2_fires(run, config) == [4]
    assert run.recorded_fired_round == 4


def test_the_cluster_guard_is_what_prevents_that_fire(null_clusters: dict) -> None:
    run = reconstruct_run(null_clusters, version="v2")
    config = GraftConfig(enabled=True, quorum_k=1, window=3, cooldown=2)
    assert legacy_v2_fires(run, config) == [4]
    assert replay_run(run, config).fired_rounds == ()


# ----------------------------------------------------------------------- grid search


def test_grid_search_covers_the_whole_parameter_space(healthy: dict, collapsed: dict) -> None:
    runs = [
        reconstruct_run(healthy, version="v2"),
        reconstruct_run(collapsed, version="v1", label=COLLAPSED),
    ]
    cells = grid_search(runs, quorums=(1, 2, 3), windows=(2, 3, 4), cooldowns=(1, 2, 3))
    assert len(cells) == 27
    assert {(c.quorum_k, c.window, c.cooldown) for c in cells} == {
        (q, w, c) for q in (1, 2, 3) for w in (2, 3, 4) for c in (1, 2, 3)
    }


def test_grid_metrics_separate_recall_from_false_fires(
    healthy: dict, collapsed: dict, null_clusters: dict
) -> None:
    runs = [
        reconstruct_run(healthy, version="v2"),
        reconstruct_run(collapsed, version="v1", label=COLLAPSED),
        reconstruct_run(null_clusters, version="v2"),
    ]
    cells = {(c.quorum_k, c.window, c.cooldown): c for c in grid_search(runs)}
    cell = cells[(3, 3, 2)]
    assert cell.collapsed_total == 1
    assert cell.collapsed_fired == 1
    assert cell.recall == pytest.approx(1.0)
    assert cell.healthy_total == 1
    assert cell.healthy_fired == 0
    assert cell.false_fire_rate == pytest.approx(0.0)
    assert cell.degenerate_rounds == 5
    assert cell.degenerate_abstained == 5
    assert cell.abstain_rate == pytest.approx(1.0)
    assert cell.fires == (("fixture-collapsed", 4),)


def test_degenerate_runs_abstain_in_every_cell(null_clusters: dict) -> None:
    cells = grid_search([reconstruct_run(null_clusters, version="v2")])
    assert all(cell.abstain_rate == 1.0 for cell in cells)
    assert all(cell.fires == () for cell in cells)


# ------------------------------------------------- the archived corpus (read-only)

ARCHIVE = (
    Path(__file__).resolve().parents[4] / "archive" / "imported-runs"
)
archived = pytest.mark.skipif(
    not ARCHIVE.is_dir(), reason="historical run archive is not present in this checkout"
)


@archived
def test_the_archive_holds_the_twelve_labelled_runs() -> None:
    runs = load_archive(ARCHIVE)
    assert len(runs) == 12
    assert {run.run_id for run in runs} == set(RUN_LABELS)


@archived
def test_no_archived_run_fires_at_the_recommended_parameters() -> None:
    """The headline calibration result, locked in as a regression.

    `RECOMMENDED` ships with `enabled=False`, so it is switched on here deliberately —
    otherwise every run would abstain on the disabled guard and prove nothing.
    """
    switched_on = replace(RECOMMENDED, enabled=True)
    for run in load_archive(ARCHIVE):
        assert replay_run(run, switched_on).fired_rounds == (), run.run_id


@archived
def test_the_graft_ships_disabled() -> None:
    assert RECOMMENDED.enabled is False


@archived
def test_the_archive_contains_no_collapse_episode() -> None:
    """Nothing in the corpus is labelled collapsed — which is why the defaults are strict."""
    assert [run.run_id for run in load_archive(ARCHIVE) if run.label == COLLAPSED] == []


@archived
def test_the_cluster_guard_suppresses_the_archived_false_fire() -> None:
    """`run-20260603-152041` logged `last_fired_iter: 4`; replay must reproduce and prevent it."""
    from app.engine.core import GraftConfig

    shipped_v2 = GraftConfig(enabled=True, quorum_k=1, window=3, cooldown=2)
    run = next(r for r in load_archive(ARCHIVE) if r.run_id == "run-20260603-152041")
    assert run.recorded_fired_round == 4
    assert legacy_v2_fires(run, shipped_v2) == [4]
    assert replay_run(run, shipped_v2).fired_rounds == ()


@archived
def test_json_fixtures_and_archive_agree_on_the_engine_state_shape() -> None:
    sample = json.loads(
        (ARCHIVE / "v2" / "run-faithtech-20260601" / "state.json").read_text(encoding="utf-8")
    )
    fixture = json.loads((FIXTURES / "healthy_run.json").read_text(encoding="utf-8"))
    for key in ("run_id", "goal", "iteration", "hypotheses", "config", "collapse"):
        assert key in sample and key in fixture
