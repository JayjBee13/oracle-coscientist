from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.services.runs.paths import confine

APP_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """Runtime settings for the local-first GUI backend."""

    model_config = SettingsConfigDict(
        env_file=APP_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="local", alias="APP_ENV")
    app_host: str = Field(default="127.0.0.1", alias="APP_HOST")
    app_port: int = Field(default=8787, alias="APP_PORT")
    frontend_origin: str = Field(default="http://127.0.0.1:5173", alias="FRONTEND_ORIGIN")
    app_auth_token: str | None = Field(default=None, alias="APP_AUTH_TOKEN")
    gateway_secret: str | None = Field(default=None, alias="GATEWAY_SECRET")
    local_identity_username: str = Field(default="operator", alias="LOCAL_IDENTITY_USERNAME")
    identity_trust_headers: bool = Field(default=True, alias="IDENTITY_TRUST_HEADERS")
    identity_require_gateway: bool = Field(default=False, alias="IDENTITY_REQUIRE_GATEWAY")
    identity_admin_group: str = Field(default="admin", alias="IDENTITY_ADMIN_GROUP")
    # How long the SSE stream stays silent before sending a keep-alive comment. Plan C5
    # fixes it at 15s; it is settable so tests can watch a heartbeat arrive without
    # waiting a quarter of a minute for it.
    sse_heartbeat_seconds: float = Field(default=15.0, alias="SSE_HEARTBEAT_SECONDS")
    database_url: str = Field(
        default="postgresql+psycopg://ai_coscientist_gui_app:<password>@127.0.0.1:5432/ai_coscientist_gui",
        alias="DATABASE_URL",
    )
    # Where this application's own runs live: one directory per run, created by the
    # supervisor. The layout is fixed at `<runs_root>/<engine_run_id>/` because the
    # projection resolves a run's engine root as the parent of `runs/` and refuses a
    # workdir laid out any other way.
    runs_root: Path = Field(
        default=APP_ROOT / "engines" / "runs",
        alias="COSCIENTIST_RUNS_ROOT",
    )
    import_archive_root: Path = Field(
        default=APP_ROOT / "archive" / "imported-runs",
        alias="IMPORT_ARCHIVE_ROOT",
    )
    fake_run_root: Path = Field(default=APP_ROOT / ".dev" / "fake-runs", alias="FAKE_RUN_ROOT")
    # The archived historical runs are imported on boot so a fresh install opens on the
    # owner's twelve real runs rather than an empty list. Tests turn it off and import the
    # fixtures they need explicitly.
    import_on_startup: bool = Field(default=True, alias="IMPORT_ON_STARTUP")
    fake_harness_enabled: bool = Field(default=True, alias="FAKE_HARNESS_ENABLED")
    real_harness_enabled: bool = Field(default=False, alias="REAL_HARNESS_ENABLED")
    # Containers opt in so stopping the API parks resumable runs before the container
    # runtime tears down its PID namespace. Local Windows restarts deliberately leave
    # detached supervisors alone, preserving the existing workstation behavior.
    manage_supervisors_on_shutdown: bool = Field(
        default=False, alias="MANAGE_SUPERVISORS_ON_SHUTDOWN"
    )
    supervisor_shutdown_grace_seconds: float = Field(
        default=10.0, ge=0.0, le=300.0, alias="SUPERVISOR_SHUTDOWN_GRACE_SECONDS"
    )
    # The frontend build this process serves, when one has been made. Deployment runs one
    # process rather than a dev server beside it; a machine with no `dist` here serves the
    # API alone, which is what every dev box and every test does.
    web_dist_dir: Path = Field(default=APP_ROOT / "frontend" / "dist", alias="WEB_DIST_DIR")

    @field_validator(
        "runs_root",
        "import_archive_root",
        "fake_run_root",
        "web_dist_dir",
        mode="before",
    )
    @classmethod
    def normalize_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @model_validator(mode="after")
    def validate_identity_mode(self) -> "Settings":
        """Refuse a deployment that claims gateway-only auth without a usable verifier."""
        if self.gateway_secret is not None and not self.gateway_secret.strip():
            raise ValueError("GATEWAY_SECRET must not be blank")
        if not self.identity_admin_group.strip():
            raise ValueError("IDENTITY_ADMIN_GROUP must not be blank")
        if self.identity_require_gateway:
            if not self.identity_trust_headers:
                raise ValueError(
                    "IDENTITY_REQUIRE_GATEWAY=true requires IDENTITY_TRUST_HEADERS=true"
                )
            if self.gateway_secret is None:
                raise ValueError("IDENTITY_REQUIRE_GATEWAY=true requires GATEWAY_SECRET")
        return self

    @property
    def engine_root(self) -> Path:
        """The directory `state.json`'s relative `file` paths resolve against."""
        return self.runs_root.parent

    def workdir_for(self, engine_run_id: str) -> Path:
        return confine(self.runs_root, engine_run_id)

    @property
    def cors_origins(self) -> list[str]:
        origins = {
            self.frontend_origin,
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        }
        return sorted(origins)

    @property
    def database_name(self) -> str:
        parsed = urlparse(self.database_url)
        return parsed.path.lstrip("/") or "unknown"


@lru_cache
def get_settings() -> Settings:
    return Settings()
