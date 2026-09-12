"""`GET /api/runs/{id}/graph` — the Ideas tab's genealogy, in one round trip.

The edge cases here are not hypothetical: every one of them is a shape the live database
already holds. Twelve imported runs predate `parent_ids` and so have no lineage at all;
four skipped clustering entirely; one rejected every idea it produced with zero matches;
the content-only runs produced a single hypothesis. The imported fixtures reproduce each
of those, so they are reused rather than reinvented — `run-fixture-legacy` is the
lineage-free run, `run-fixture-rejected` the all-rejected one, `real-v1-fixture-gui` the
single-node one.

What the fixtures cannot supply is an *operator*: the importer sets it to `None` for every
historical hypothesis, because the old engines never recorded which move produced an idea.
So the contract test builds an app run whose lineage carries operators, which is what a
run launched by this app actually looks like.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest

from app.engine.store import RunStore
from tests.support.imported import (
    GRAFT_RUN,
    GUI_RUN,
    LEGACY_RUN,
    REJECTED_RUN,
    fixture_settings,
    imported_client,
)

GRAPH_FIELDS = {"run_id", "nodes", "edges", "meta"}
GRAPH_NODE_FIELDS = {
    "hid", "id", "title", "status", "elo", "matches", "wins", "created_round", "operator",
    "cluster", "duplicate_of", "source", "is_leader",
}
GRAPH_EDGE_FIELDS = {"parent", "child", "operator"}
GRAPH_META_FIELDS = {
    "rounds", "has_lineage", "lineage_recorded", "has_clusters", "elo_min", "elo_max",
    "node_count",
}


@pytest.fixture(scope="module")
def client():
    return imported_client()


@pytest.fixture(scope="module")
def store() -> RunStore:
    return RunStore(settings=fixture_settings())


def _new_run(store: RunStore, engine_run_id: str) -> UUID:
    return store.create_run(
        question="How do ideas survive a tournament?",
        prompt="How do ideas survive a tournament?",
        harness="claude",
        source="app",
        lifecycle="completed",
        engine_run_id=engine_run_id,
        budget_calls=10,
        budget_usd=Decimal("2.00"),
    )


@pytest.fixture(scope="module")
def seeded_run(store: RunStore) -> UUID:
    """An app run with real descent: a grounding, a two-parent combination, a rejection
    and a merged duplicate — the six shapes the graph has to draw."""
    run_id = _new_run(store, "run-graph-seeded")
    store.add_hypothesis(
        run_id, title="Alpha", body_md="# Alpha", created_round=1, elo=1240.0, cluster="c-a"
    )
    store.add_hypothesis(
        run_id, title="Beta", body_md="# Beta", created_round=1, elo=1180.0, cluster="c-b"
    )
    store.add_hypothesis(
        run_id,
        title="Gamma",
        body_md="# Gamma",
        created_round=2,
        elo=1190.0,
        cluster="c-a",
        parent_ids=["h001"],
        operator="grounding",
    )
    store.add_hypothesis(
        run_id,
        title="Delta",
        body_md="# Delta",
        created_round=2,
        elo=1205.0,
        cluster="c-b",
        parent_ids=["h001", "h002"],
        operator="combination",
    )
    store.add_hypothesis(
        run_id,
        title="Epsilon",
        body_md="# Epsilon",
        created_round=2,
        elo=1150.0,
        status="rejected",
        parent_ids=["h002"],
        operator="simplification",
    )
    store.add_hypothesis(
        run_id,
        title="Zeta",
        body_md="# Zeta",
        created_round=3,
        elo=1170.0,
        parent_ids=["h004"],
        operator="out_of_box",
    )
    store.apply_clusters(run_id, {"h006": "c-b"}, duplicates={"h006": "h004"})
    return run_id


def _graph(client, reference) -> dict:
    response = client.get(f"/api/runs/{reference}/graph")
    assert response.status_code == 200, response.text
    return response.json()


# -------------------------------------------------------------------------- contract


def test_graph_returns_nodes_edges_and_meta(client, seeded_run):
    response = client.get(f"/api/runs/{seeded_run}/graph")
    assert response.status_code == 200, response.text
    body = response.json()

    assert set(body) == GRAPH_FIELDS
    assert body["run_id"] == str(seeded_run)
    hids = [node["hid"] for node in body["nodes"]]
    assert hids == sorted(hids), "nodes are ordered by hid so layout is reproducible"

    combination = next(n for n in body["nodes"] if n["operator"] == "combination")
    parents = [e["parent"] for e in body["edges"] if e["child"] == combination["hid"]]
    assert len(parents) == 2, "a combination has two parents and yields two edges"
    assert parents == ["h001", "h002"]

    assert body["meta"]["has_lineage"] is True
    assert body["meta"]["elo_max"] >= body["meta"]["elo_min"]
    assert sum(1 for n in body["nodes"] if n["is_leader"]) == 1


def test_graph_node_carries_only_what_the_graph_draws(client, seeded_run):
    """`body_md` is a third of a megabyte across a big run and the canvas renders none of
    it; the detail panel keeps using `GET /api/hypotheses/{id}`."""
    body = _graph(client, seeded_run)

    node = next(n for n in body["nodes"] if n["hid"] == "h004")
    assert set(node) == GRAPH_NODE_FIELDS
    assert node["title"] == "Delta"
    assert node["status"] == "active"
    assert node["elo"] == 1205.0
    assert node["created_round"] == 2
    assert node["cluster"] == "c-b"
    assert node["source"] == "agent"
    assert UUID(node["id"])


def test_graph_edges_point_from_parent_to_child_and_carry_the_operator(client, seeded_run):
    """The operator is copied onto the edge so a converging pair can be styled alone."""
    body = _graph(client, seeded_run)

    assert set(body["edges"][0]) == GRAPH_EDGE_FIELDS
    assert {(e["parent"], e["child"], e["operator"]) for e in body["edges"]} == {
        ("h001", "h003", "grounding"),
        ("h001", "h004", "combination"),
        ("h002", "h004", "combination"),
        ("h002", "h005", "simplification"),
        ("h004", "h006", "out_of_box"),
    }


def test_graph_meta_describes_the_run_it_came_from(client, seeded_run):
    body = _graph(client, seeded_run)

    assert set(body["meta"]) == GRAPH_META_FIELDS
    assert body["meta"] == {
        "rounds": 3,
        "has_lineage": True,
        "lineage_recorded": True,
        "has_clusters": True,
        "elo_min": 1150.0,
        "elo_max": 1240.0,
        "node_count": 6,
    }


def test_the_leader_is_the_strongest_active_idea(client, seeded_run):
    """Rejected and archived ideas cannot lead, however well they were once rated."""
    body = _graph(client, seeded_run)

    leaders = [n["hid"] for n in body["nodes"] if n["is_leader"]]
    assert leaders == ["h001"]


def test_a_merged_duplicate_keeps_the_pointer_to_its_survivor(client, seeded_run):
    body = _graph(client, seeded_run)

    merged = next(n for n in body["nodes"] if n["hid"] == "h006")
    assert merged["status"] == "archived"
    assert merged["duplicate_of"] == "h004"


def test_graph_resolves_by_uuid_and_by_engine_run_id(client, seeded_run):
    by_uuid = _graph(client, seeded_run)
    by_engine_id = _graph(client, "run-graph-seeded")

    assert by_uuid == by_engine_id


# ------------------------------------------------------------------------ real shapes


def test_imported_run_without_lineage_is_honest(client):
    """Twelve archived runs predate `parent_ids`. The ideas still render; the descent
    does not exist, and the response says so rather than implying an empty run."""
    body = _graph(client, LEGACY_RUN)

    assert body["edges"] == []
    assert body["meta"]["has_lineage"] is False
    assert body["meta"]["lineage_recorded"] is False, "the engine could not record it"
    assert body["nodes"], "the ideas still render; only the lineage is missing"
    assert body["meta"]["node_count"] == 3


def test_an_app_run_that_evolved_nothing_still_recorded_its_lineage(client, store):
    """The two absences are not the same absence.

    A run that has generated one round and evolved nothing has no edges — and neither does
    an import that predates `parent_ids`. Told apart only by `has_lineage`, every modern
    run was described to its reader as one whose lineage had been lost.
    """
    run_id = _new_run(store, "run-graph-unevolved")
    for title in ("One", "Two", "Three"):
        store.add_hypothesis(run_id, title=title, body_md=f"# {title}", created_round=1)

    body = _graph(client, run_id)

    assert body["edges"] == []
    assert body["meta"]["has_lineage"] is False
    assert body["meta"]["lineage_recorded"] is True


def test_an_import_that_did_record_descent_is_not_called_lineage_free(client):
    """One archived run names two parents in a free-text field, so its descent survived
    the import. Judging by source alone would have described it as unrecorded."""
    body = _graph(client, GRAFT_RUN)

    assert body["meta"]["lineage_recorded"] is True
    assert body["meta"]["has_lineage"] is True


def test_all_rejected_run_has_no_leader(client):
    """One archived run rejected eighteen of eighteen. That is a research outcome, and
    crowning a rejected idea to avoid an empty highlight would misreport it."""
    body = _graph(client, REJECTED_RUN)

    assert all(n["status"] == "rejected" for n in body["nodes"])
    assert not any(n["is_leader"] for n in body["nodes"])
    assert body["meta"]["elo_min"] == body["meta"]["elo_max"]


def test_a_run_that_skipped_clustering_claims_no_clusters(client):
    """Four runs never ran proximity; hue would carry no meaning, so the legend must not
    claim it does."""
    rejected = _graph(client, REJECTED_RUN)
    legacy = _graph(client, LEGACY_RUN)

    assert all(n["cluster"] is None for n in rejected["nodes"])
    assert rejected["meta"]["has_clusters"] is False
    assert legacy["meta"]["has_clusters"] is True


def test_a_single_hypothesis_run_still_has_a_graph(client):
    """A content-only historical run produced one idea; it is drawn, not an empty canvas."""
    body = _graph(client, GUI_RUN)

    assert body["meta"]["node_count"] == 1
    assert body["meta"]["elo_min"] == body["meta"]["elo_max"]
    assert body["meta"]["rounds"] >= 1, "a run has at least one round to band nodes into"
    assert [n["is_leader"] for n in body["nodes"]] == [True]


def test_an_imported_two_parent_evolution_still_yields_two_edges(client):
    """`parent: "h001,h002"` was one free-text field; the operator behind it was never
    recorded, so the edge is drawn with a null operator rather than a guessed one."""
    body = _graph(client, GRAFT_RUN)

    converging = [e for e in body["edges"] if e["child"] == "h004"]
    assert {e["parent"] for e in converging} == {"h001", "h002"}
    assert all(e["operator"] is None for e in converging)
    assert next(n for n in body["nodes"] if n["hid"] == "h001")["wins"] == 2


def test_a_parent_outside_the_run_is_not_drawn(client, store):
    """A dangling hid would otherwise become an edge to a node that does not exist, and
    every layout would place a phantom."""
    run_id = _new_run(store, "run-graph-orphan-parent")
    store.add_hypothesis(run_id, title="Root", body_md="# Root", created_round=1)
    store.add_hypothesis(
        run_id,
        title="Child of a ghost",
        body_md="# Child",
        created_round=2,
        parent_ids=["h001", "h404"],
        operator="grounding",
    )

    body = _graph(client, run_id)

    assert [(e["parent"], e["child"]) for e in body["edges"]] == [("h001", "h002")]


def test_a_run_with_no_hypotheses_is_an_empty_graph_not_a_crash(client, store):
    """A run that failed in its first generation call has rows for nothing."""
    run_id = _new_run(store, "run-graph-empty")

    body = _graph(client, run_id)

    assert body["nodes"] == []
    assert body["edges"] == []
    assert body["meta"]["node_count"] == 0
    assert body["meta"]["rounds"] == 1
    assert body["meta"]["elo_min"] == body["meta"]["elo_max"]


def test_the_leader_tie_is_broken_by_hid(client, store):
    """Two ideas on the same rating is the normal state of a run before its first match;
    without a tie-break the highlight would jump between page loads."""
    run_id = _new_run(store, "run-graph-tied-elo")
    for title in ("First", "Second", "Third"):
        store.add_hypothesis(run_id, title=title, body_md=f"# {title}", created_round=1)

    body = _graph(client, run_id)

    assert len({n["elo"] for n in body["nodes"]}) == 1
    assert [n["hid"] for n in body["nodes"] if n["is_leader"]] == ["h001"]


# ------------------------------------------------------------------------------- 404


def test_missing_run_is_404(client):
    response = client.get("/api/runs/00000000-0000-0000-0000-000000000000/graph")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"


def test_a_reference_that_is_not_even_a_uuid_is_404(client):
    response = client.get("/api/runs/nope-not-a-run/graph")

    assert response.status_code == 404
    assert response.json()["code"] == "run_not_found"
    assert response.json()["details"]["run_id"] == "nope-not-a-run"
