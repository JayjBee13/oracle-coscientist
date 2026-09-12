"""`GET /api/compare` — two runs side by side, with no merged Elo anywhere in it."""

from __future__ import annotations

import pytest

from tests.support.imported import GRAFT_RUN, LEGACY_RUN, REJECTED_RUN, imported_client

COMPARE_FIELDS = {
    "baseline", "challenger", "shared_prompt", "deltas", "movement", "graft_summary"
}


@pytest.fixture(scope="module")
def client():
    return imported_client()


def _compare(client, baseline: str, challenger: str):
    return client.get(
        "/api/compare", params={"baseline": baseline, "challenger": challenger}
    )


def test_compares_two_runs(client):
    payload = _compare(client, LEGACY_RUN, GRAFT_RUN).json()

    assert set(payload) == COMPARE_FIELDS
    assert payload["baseline"]["engine_run_id"] == LEGACY_RUN
    assert payload["challenger"]["engine_run_id"] == GRAFT_RUN


def test_imported_runs_never_claim_a_shared_prompt(client):
    """Neither side has a recorded prompt hash, so the page warns instead of implying."""
    payload = _compare(client, LEGACY_RUN, GRAFT_RUN).json()

    assert payload["shared_prompt"] is False


def test_deltas_are_directional_and_never_compare_raw_elo(client):
    payload = _compare(client, LEGACY_RUN, GRAFT_RUN).json()

    metrics = {delta["metric"]: delta for delta in payload["deltas"]}
    assert "Top Elo" not in metrics, "Elo is only meaningful inside one tournament"
    assert metrics["Active hypotheses"]["challenger"] == 4.0
    assert metrics["Matches judged"]["base"] == 2.0
    assert metrics["Matches judged"]["challenger"] == 3.0
    assert metrics["Model calls"]["direction_hint"] == "down"
    assert {delta["direction_hint"] for delta in payload["deltas"]} <= {"up", "down", "flat"}


def test_graft_summary_is_null_for_a_run_that_never_had_the_concept(client):
    payload = _compare(client, LEGACY_RUN, GRAFT_RUN).json()

    assert payload["graft_summary"]["baseline"] is None
    assert payload["graft_summary"]["challenger"] == {
        "enabled": True,
        "fired_count": 1,
        "collapse_events": 3,
    }


def test_movement_matches_by_title_across_runs(client):
    payload = _compare(client, REJECTED_RUN, GRAFT_RUN).json()
    movement = payload["movement"]
    assert [entry["rank"] for entry in movement] == [1, 2, 3, 4]
    # Nothing in the rejected run shares a title with the graft run.
    assert all(entry["previous_rank"] is None for entry in movement)
    assert all(entry["delta"] is None for entry in movement)


def test_a_run_cannot_be_compared_with_itself(client):
    response = _compare(client, GRAFT_RUN, GRAFT_RUN)

    assert response.status_code == 422
    assert response.json()["code"] == "same_run_comparison"


def test_unknown_run_names_the_side_it_could_not_find(client):
    response = _compare(client, GRAFT_RUN, "not-a-run")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"
    assert "challenger" in response.json()["message"]
