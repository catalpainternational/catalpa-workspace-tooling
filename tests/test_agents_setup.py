"""Tests for setup-agents / agents_setup."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from catalpa_tooling.agents_setup import (
    MANAGED_MARKER,
    apply_remove,
    apply_setup,
    claude_deny_rules,
    cursor_rule_to_claude,
    inspect_status,
    plan_remove,
    plan_setup,
    with_marker,
)
from catalpa_tooling.config import ProjectConfigError


def _write_repo(repo: Path, *, agents: str = "") -> None:
    (repo / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    (repo / "tooling.yaml").write_text(
        """
project:
  name: test-agents
  root_marker: pyproject.toml
paths:
  backend: .
  frontend: .
  scripts: scripts
  env_local: .env.local
  email_backend_dir: var/email
  fetch_db_dump: docker/postgres/dumps/db.custom
  deploy:
    envs_dir: docker/envs
    images_config: docker/images.yaml
    default_compose: compose.yaml
    dev_compose: compose.dev.yaml
"""
        + agents,
        encoding="utf-8",
    )


def test_default_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup())

    assert (tmp_path / ".cursorignore").is_file()
    assert (tmp_path / ".cursor/rules/secrets-and-agents.mdc").is_file()
    assert (tmp_path / ".cursor/rules/remote-environments.mdc").is_file()
    assert not (tmp_path / ".cursor/rules/worktrees.mdc").exists()
    skill = tmp_path / ".claude/skills/dk-stack/SKILL.md"
    assert skill.is_file()
    link = tmp_path / ".cursor/skills/dk-stack"
    assert link.is_symlink()
    assert os.readlink(link) == "../../.claude/skills/dk-stack"
    assert (link / "SKILL.md").read_text(encoding="utf-8") == skill.read_text(encoding="utf-8")
    assert inspect_status().ready


def test_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup())
    assert plan_setup().empty


def test_marker_follows_frontmatter() -> None:
    text = "---\nname: x\ndescription: y\n---\n\n# Body\n"
    marked = with_marker(text, name="SKILL.md")
    assert marked.startswith("---\nname: x\ndescription: y\n---\n")
    assert f"<!-- {MANAGED_MARKER}" in marked
    assert marked.endswith("# Body\n")

    assert with_marker("a\n", name="cursorignore").startswith(f"# {MANAGED_MARKER}")


def test_optional_rules_and_no_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path, agents="agents:\n  rules: [worktrees]\n  skills: []\n")
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup())

    assert (tmp_path / ".cursor/rules/worktrees.mdc").is_file()
    assert (tmp_path / ".claude/rules/worktrees.md").is_file()
    assert not (tmp_path / ".claude/skills").exists()
    assert not (tmp_path / ".cursor/skills").exists()


def test_unknown_names_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path, agents="agents:\n  rules: [nope]\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ProjectConfigError, match="agents.rules: unknown nope"):
        plan_setup()


def test_deselected_optional_files_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path, agents="agents:\n  rules: [worktrees]\n")
    monkeypatch.chdir(tmp_path)
    apply_setup(plan_setup())

    _write_repo(tmp_path, agents="agents:\n  skills: []\n")
    plan = plan_setup()
    apply_setup(plan)

    assert not (tmp_path / ".cursor/rules/worktrees.mdc").exists()
    assert not (tmp_path / ".claude/rules/worktrees.md").exists()
    assert not (tmp_path / ".cursor/skills/dk-stack").is_symlink()
    assert not (tmp_path / ".claude/skills").exists()
    assert (tmp_path / ".cursor/rules/secrets-and-agents.mdc").is_file()


def test_unmanaged_file_left_alone_without_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    own = tmp_path / ".cursorignore"
    own.write_text("my own\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    plan = plan_setup()
    assert own in plan.skipped
    apply_setup(plan)
    assert own.read_text(encoding="utf-8") == "my own\n"
    assert not inspect_status().ready

    apply_setup(plan_setup(force=True))
    assert MANAGED_MARKER in own.read_text(encoding="utf-8")


def test_unmarked_identical_copy_is_adopted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rule copied by hand from the same tooling version takes the marker without --force."""
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    apply_setup(plan_setup())

    rule = tmp_path / ".cursor/rules/secrets-and-agents.mdc"
    marked = rule.read_text(encoding="utf-8")
    unmarked = "".join(line for line in marked.splitlines(keepends=True) if MANAGED_MARKER not in line)
    rule.write_text(unmarked.replace("---\n\n\n", "---\n\n", 1), encoding="utf-8")

    plan = plan_setup()
    assert rule not in plan.skipped
    apply_setup(plan)
    assert rule.read_text(encoding="utf-8") == marked


def test_edited_managed_file_is_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    apply_setup(plan_setup())

    skill = tmp_path / ".claude/skills/dk-stack/SKILL.md"
    original = skill.read_text(encoding="utf-8")
    skill.write_text(original + "\nlocal edit\n", encoding="utf-8")
    assert not inspect_status().ready

    apply_setup(plan_setup())
    assert skill.read_text(encoding="utf-8") == original


def test_real_directory_in_place_of_link_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    own = tmp_path / ".cursor/skills/dk-stack"
    own.mkdir(parents=True)
    (own / "SKILL.md").write_text("project copy\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup(force=True))
    assert not own.is_symlink()
    assert (own / "SKILL.md").read_text(encoding="utf-8") == "project copy\n"


def test_remove_only_managed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    apply_setup(plan_setup())
    own_rule = tmp_path / ".cursor/rules/project.mdc"
    own_rule.write_text("ours\n", encoding="utf-8")

    apply_remove(plan_remove())

    assert not (tmp_path / ".cursorignore").exists()
    assert not (tmp_path / ".cursor/rules/secrets-and-agents.mdc").exists()
    assert not (tmp_path / ".cursor/skills").exists()
    assert not (tmp_path / ".claude").exists()
    assert own_rule.read_text(encoding="utf-8") == "ours\n"


def test_dry_run_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path)
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup(), dry_run=True)
    assert not (tmp_path / ".cursorignore").exists()
    assert not (tmp_path / ".cursor").exists()


def test_cursor_rule_to_claude() -> None:
    scoped = "---\ndescription: d\nglobs: pyproject.toml, **/smoke/**\nalwaysApply: false\n---\n\n# Body\n"
    assert cursor_rule_to_claude(scoped) == (
        '---\npaths:\n  - "pyproject.toml"\n  - "**/smoke/**"\n---\n\n# Body\n'
    )
    always = "---\ndescription: d\nalwaysApply: true\n---\n\n# Body\n"
    assert cursor_rule_to_claude(always) == "# Body\n"


def test_claude_deny_rules_from_cursorignore() -> None:
    rules = claude_deny_rules("# c\n\ndocker/envs/**/credentials.yaml\n.env\n.env.*\n!.env.example\n")
    assert rules == ["Read(/docker/envs/**/credentials.yaml)", "Read(.env)", "Read(.env.*)"]


def test_claude_rules_and_settings_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path, agents="agents:\n  rules: [smoke-tests]\n")
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup())

    always = (tmp_path / ".claude/rules/secrets-and-agents.md").read_text(encoding="utf-8")
    assert always.startswith(f"<!-- {MANAGED_MARKER}")
    assert "alwaysApply" not in always
    scoped = (tmp_path / ".claude/rules/smoke-tests.md").read_text(encoding="utf-8")
    assert scoped.startswith('---\npaths:\n  - "pyproject.toml"\n')

    settings = json.loads((tmp_path / ".claude/settings.json").read_text(encoding="utf-8"))
    deny = settings["permissions"]["deny"]
    assert "Read(/docker/envs/**/credentials.yaml)" in deny
    assert "Read(.env.local)" in deny
    assert not any("!" in r for r in deny)
    assert inspect_status().ready


def test_settings_merge_keeps_project_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_repo(tmp_path)
    settings_path = tmp_path / ".claude/settings.json"
    settings_path.parent.mkdir()
    project = {
        "permissions": {"allow": ["Bash(uv run dk dev ps)"], "deny": ["Read(./secret.txt)"]},
        "env": {"FOO": "1"},
    }
    settings_path.write_text(json.dumps(project), encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    apply_setup(plan_setup())
    merged = json.loads(settings_path.read_text(encoding="utf-8"))
    assert merged["env"] == {"FOO": "1"}
    assert merged["permissions"]["allow"] == ["Bash(uv run dk dev ps)"]
    assert merged["permissions"]["deny"][0] == "Read(./secret.txt)"
    assert "Read(.env)" in merged["permissions"]["deny"]
    assert plan_setup().settings is None

    apply_remove(plan_remove())
    assert json.loads(settings_path.read_text(encoding="utf-8")) == project


def test_unreadable_settings_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_repo(tmp_path)
    settings_path = tmp_path / ".claude/settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text("{ not json", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    plan = plan_setup(force=True)
    assert settings_path in plan.skipped
    assert plan.settings is None
    apply_setup(plan)
    assert settings_path.read_text(encoding="utf-8") == "{ not json"
    assert not inspect_status().ready
