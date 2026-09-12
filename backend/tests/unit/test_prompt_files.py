from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[3]


def test_codex_skill_and_prompt_files_exist_and_are_ascii():
    paths = [
        APP_ROOT / ".agents" / "skills" / "coscientist" / "SKILL.md",
        APP_ROOT / "prompts" / "shared" / "artifact-contract.md",
        APP_ROOT / "prompts" / "codex" / "supervisor.md",
        APP_ROOT / "prompts" / "claude" / "supervisor.md",
    ]

    for path in paths:
        content = path.read_text(encoding="utf-8")
        assert content.isascii(), path
        assert "state.json" in content, path
        assert "engine_run_id" in content, path
        assert "grounding" in content.lower(), path


def test_codex_skill_frontmatter_and_artifact_contract_terms():
    skill = (APP_ROOT / ".agents" / "skills" / "coscientist" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    artifact_contract = (APP_ROOT / "prompts" / "shared" / "artifact-contract.md").read_text(
        encoding="utf-8"
    )

    assert skill.startswith("---\nname: coscientist\n")
    assert "description:" in skill
    assert "runs/<engine_run_id>/" in skill
    assert "status.json" in skill
    assert "status_line" in artifact_contract
    assert "rounds_completed" in artifact_contract
    assert "stop_reason" in artifact_contract
