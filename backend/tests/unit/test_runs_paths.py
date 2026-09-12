"""One containment check, used everywhere a path is confined to a run root."""

import pytest

from app.core.config import Settings
from app.services.runs.paths import (
    PathEscapesRoot,
    archive_root_ref,
    confine,
    is_within,
    live_root_ref,
    relative_posix,
    resolve_run_root,
    to_posix,
)


def test_path_inside_root_is_within(tmp_path):
    assert is_within(tmp_path / "runs" / "r1" / "state.json", tmp_path) is True


def test_root_itself_is_within_root(tmp_path):
    assert is_within(tmp_path, tmp_path) is True


def test_sibling_with_shared_prefix_is_not_within(tmp_path):
    """`/a/run2` must not count as inside `/a/run` — the classic prefix-match bug."""
    root = tmp_path / "run"
    root.mkdir()
    (tmp_path / "run2").mkdir()

    assert is_within(tmp_path / "run2" / "state.json", root) is False


def test_parent_traversal_is_not_within(tmp_path):
    root = tmp_path / "engine"
    root.mkdir()

    assert is_within(root / ".." / "outside.md", root) is False


def test_unrelated_absolute_path_is_not_within(tmp_path):
    root = tmp_path / "engine"
    root.mkdir()

    assert is_within(tmp_path / "elsewhere" / "state.json", root) is False


def test_to_posix_converts_windows_separators():
    assert to_posix("runs\\run-1\\hypotheses\\h001.md") == "runs/run-1/hypotheses/h001.md"


def test_relative_posix_is_relative_to_root(tmp_path):
    path = tmp_path / "runs" / "run-1" / "state.json"

    assert relative_posix(path, tmp_path) == "runs/run-1/state.json"


def test_relative_posix_falls_back_to_full_path_when_outside(tmp_path):
    outside = tmp_path.parent / "somewhere-else.md"

    assert relative_posix(outside, tmp_path) == to_posix(outside)


def test_confine_accepts_a_backslash_relative_path(tmp_path):
    target = tmp_path / "runs" / "run-1" / "state.json"
    target.parent.mkdir(parents=True)
    target.write_text("{}", encoding="utf-8")

    assert confine(tmp_path, "runs\\run-1\\state.json") == target.resolve()


def test_confine_rejects_parent_traversal(tmp_path):
    with pytest.raises(PathEscapesRoot):
        confine(tmp_path, "../outside.md")


@pytest.mark.parametrize(
    "absolute",
    ["/etc/hosts", "C:\\Windows\\win.ini", "C:Windows\\win.ini", "//server/share/file"],
)
def test_confine_rejects_an_absolute_path_on_every_host_platform(tmp_path, absolute):
    with pytest.raises(PathEscapesRoot):
        confine(tmp_path, absolute)


def test_confine_rejects_traversal_that_lands_back_inside(tmp_path):
    """Rejected on the way out, even though the result is inside the root."""
    (tmp_path / "runs").mkdir()

    with pytest.raises(PathEscapesRoot):
        confine(tmp_path, "runs/../../" + tmp_path.name + "/runs")


def _settings(tmp_path) -> Settings:
    return Settings(
        APP_ENV="test",
        IMPORT_ON_STARTUP=False,
        COSCIENTIST_RUNS_ROOT=tmp_path / "engines" / "runs",
        IMPORT_ARCHIVE_ROOT=tmp_path / "archive" / "imported-runs",
    )


def test_live_root_is_relocated_from_a_legacy_windows_path(tmp_path):
    settings = _settings(tmp_path)

    resolved = resolve_run_root(
        source="app",
        source_version=None,
        engine_run_id="run-20260808-155541-be3f",
        stored_root=(
            "C:/srv/oracle/engines/runs/"
            "run-20260808-155541-be3f"
        ),
        settings=settings,
    )

    assert resolved == (settings.runs_root / "run-20260808-155541-be3f").resolve()


def test_imported_root_is_relocated_from_a_legacy_windows_path(tmp_path):
    settings = _settings(tmp_path)

    resolved = resolve_run_root(
        source="imported",
        source_version="v1",
        engine_run_id="run-historical",
        stored_root=(
            "C:/srv/oracle/archive/"
            "imported-runs/gui/run-historical"
        ),
        settings=settings,
    )

    assert resolved == (settings.import_archive_root / "gui" / "run-historical").resolve()


def test_portable_root_references_resolve_only_under_configured_mounts(tmp_path):
    settings = _settings(tmp_path)
    archived = settings.import_archive_root / "v2" / "run-old"
    archived.mkdir(parents=True)

    assert live_root_ref("run-new") == "runs/run-new"
    assert archive_root_ref(archived, settings.import_archive_root) == "archive/v2/run-old"
    assert resolve_run_root(
        source="imported",
        source_version="v2",
        engine_run_id="run-old",
        stored_root="archive/v2/run-old",
        settings=settings,
    ) == archived.resolve()


@pytest.mark.parametrize("engine_run_id", ["../outside", "nested/run", "C:/outside"])
def test_run_root_resolution_rejects_an_unconfined_engine_id(tmp_path, engine_run_id):
    with pytest.raises(PathEscapesRoot):
        resolve_run_root(
            source="app",
            source_version=None,
            engine_run_id=engine_run_id,
            stored_root="",
            settings=_settings(tmp_path),
        )
