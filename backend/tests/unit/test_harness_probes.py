import subprocess

from app.services.harness.probes import probe_cli


def test_probe_cli_reports_missing_command(monkeypatch):
    monkeypatch.setattr("app.services.harness.probes.shutil.which", lambda _: None)

    result = probe_cli("missing")

    assert result.installed is False
    assert result.error == "not_found"


def test_probe_cli_reports_version(monkeypatch):
    monkeypatch.setattr(
        "app.services.harness.probes.shutil.which",
        lambda _: "C:/Tools/claude.cmd",
    )
    monkeypatch.setattr(
        "app.services.harness.probes.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["claude", "--version"],
            returncode=0,
            stdout="2.1.161 (Claude Code)\n",
            stderr="",
        ),
    )

    result = probe_cli("claude")

    assert result.installed is True
    assert result.executable == "C:/Tools/claude.cmd"
    assert result.version == "2.1.161 (Claude Code)"
    assert result.error is None
