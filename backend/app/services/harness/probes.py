import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class CliProbeResult:
    installed: bool
    executable: str | None
    version: str | None
    error: str | None = None


def probe_cli(command: str, timeout_seconds: float = 3.0) -> CliProbeResult:
    executable = shutil.which(command)
    if executable is None:
        return CliProbeResult(
            installed=False,
            executable=None,
            version=None,
            error="not_found",
        )

    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout_seconds,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired:
        return CliProbeResult(
            installed=True,
            executable=executable,
            version=None,
            error="version_timeout",
        )
    except OSError as exc:
        return CliProbeResult(
            installed=True,
            executable=executable,
            version=None,
            error=str(exc),
        )

    output = (result.stdout or result.stderr).strip()
    if result.returncode != 0:
        return CliProbeResult(
            installed=True,
            executable=executable,
            version=output or None,
            error=f"version_failed:{result.returncode}",
        )

    return CliProbeResult(
        installed=True,
        executable=executable,
        version=output or None,
    )


PROBE_CACHE_SECONDS = 60.0
"""How long a probe answer is trusted before spawning `--version` again.

`/health` and `/capabilities` are polled by every open tab, and a probe is a subprocess
spawn. Nobody reinstalls a CLI mid-session, so a minute-old install/version answer costs
nothing to serve and saves a spawn on every poll.
"""

_cache: dict[str, tuple[float, CliProbeResult]] = {}
_cache_lock = Lock()


def cached_probe_cli(
    command: str, *, ttl_seconds: float = PROBE_CACHE_SECONDS, timeout_seconds: float = 3.0
) -> CliProbeResult:
    """`probe_cli`, memoized per command for `ttl_seconds` (default 60s)."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(command)
        if cached is not None and now - cached[0] < ttl_seconds:
            return cached[1]
    result = probe_cli(command, timeout_seconds=timeout_seconds)
    with _cache_lock:
        _cache[command] = (now, result)
    return result


def reset_probe_cache() -> None:
    """Forget cached probes. Tests use this so a monkeypatched probe is seen immediately."""
    with _cache_lock:
        _cache.clear()
