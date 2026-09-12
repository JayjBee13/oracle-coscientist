"""Put the import fixtures in the test schema and hand back a client that reads them."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.services.runs.importer import ImportReport, import_all

FIXTURE_ARCHIVE = Path(__file__).resolve().parents[1] / "fixtures" / "import"
REAL_ARCHIVE = Path(__file__).resolve().parents[3] / "archive" / "imported-runs"

# The engine_run_ids the fixtures carry, so tests name runs rather than positions.
LEGACY_RUN = "run-fixture-legacy"
MOJIBAKE_RUN = "run-fixture-mojibake"
GRAFT_RUN = "run-fixture-graft"
REJECTED_RUN = "run-fixture-rejected"
GUI_RUN = "real-v1-fixture-gui"
FIXTURE_RUNS = (LEGACY_RUN, MOJIBAKE_RUN, GRAFT_RUN, REJECTED_RUN, GUI_RUN)


def fixture_settings(archive: Path | None = None, **overrides: object) -> Settings:
    """Settings pointed at the fixture archive, with the startup import off.

    Startup import is off so a test that builds a client never quietly re-reads the real
    archive; tests that want data call `import_fixtures` and say so.
    """
    return Settings(
        **{
            "APP_ENV": "test",
            "IMPORT_ARCHIVE_ROOT": archive or FIXTURE_ARCHIVE,
            "IMPORT_ON_STARTUP": False,
            **overrides,
        }
    )


def import_fixtures(archive: Path | None = None) -> ImportReport:
    return import_all(fixture_settings(archive))


def imported_client(archive: Path | None = None) -> TestClient:
    settings = fixture_settings(archive)
    import_all(settings)
    return TestClient(create_app(settings))
