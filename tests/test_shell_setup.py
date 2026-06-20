"""Tests for setup-shell / shell_setup."""

from pathlib import Path

from catalpa_tooling.shell_assets import catalpa_direnv_zsh_path
from catalpa_tooling.shell_setup import (
    MARKER_END,
    MARKER_START,
    apply_remove,
    apply_setup,
    build_next_steps,
    build_zshrc_block,
    extract_catalpa_block,
    next_steps_context,
    patch_zshrc_content,
    plan_remove,
    plan_setup,
    remove_catalpa_block,
)


def test_catalpa_direnv_zsh_path_exists() -> None:
    path = catalpa_direnv_zsh_path()
    assert path.is_file()
    assert "_catalpa_direnv_completion" in path.read_text(encoding="utf-8")


def test_build_zshrc_block_includes_markers() -> None:
    block = build_zshrc_block(include_direnv_hook=True, include_completion=True)
    assert MARKER_START in block
    assert MARKER_END in block
    assert 'direnv hook zsh' in block
    assert "catalpa/direnv.zsh" in block


def test_patch_zshrc_content_appends_when_missing() -> None:
    block = build_zshrc_block(include_direnv_hook=True, include_completion=False)
    result = patch_zshrc_content("# existing\n", block)
    assert result.startswith("# existing\n")
    assert MARKER_START in result
    assert result.endswith("\n")


def test_patch_zshrc_content_replaces_existing_block() -> None:
    old = build_zshrc_block(include_direnv_hook=True, include_completion=True)
    zshrc = f"# header\n{old}\n# footer\n"
    new = build_zshrc_block(include_direnv_hook=True, include_completion=False)
    result = patch_zshrc_content(zshrc, new)
    assert result.count(MARKER_START) == 1
    assert "catalpa/direnv.zsh" not in result
    assert "# footer" in result


def test_plan_setup_idempotent(tmp_path: Path) -> None:
    zshrc = tmp_path / ".zshrc"
    dest = tmp_path / "catalpa" / "direnv.zsh"
    zshrc.write_text("", encoding="utf-8")

    first = plan_setup(zshrc_path=zshrc, catalpa_direnv_dest=dest)
    assert first.write_catalpa_direnv is True
    assert first.patch_zshrc is True

    apply_setup(first)

    second = plan_setup(zshrc_path=zshrc, catalpa_direnv_dest=dest)
    assert second.write_catalpa_direnv is False
    assert second.patch_zshrc is False


def test_extract_catalpa_block() -> None:
    block = build_zshrc_block(include_direnv_hook=True, include_completion=True)
    zshrc = f"before\n{block}after\n"
    extracted = extract_catalpa_block(zshrc)
    assert extracted is not None
    assert extracted.startswith(MARKER_START)
    assert extracted.rstrip().endswith(MARKER_END)


def test_build_next_steps_in_repo_needs_reload(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("catalpa_tooling.shell_setup.shutil.which", lambda _cmd: None)
    (tmp_path / "tooling.yaml").write_text("project:\n  name: test\n", encoding="utf-8")
    (tmp_path / ".envrc").write_text("PATH_add .venv/bin\n", encoding="utf-8")
    ctx = next_steps_context(
        cwd=tmp_path,
        zshrc_path=tmp_path / ".zshrc",
        zshrc_changed=False,
        environ={},
    )
    steps = build_next_steps(ctx)
    assert steps == (f"source {tmp_path / '.zshrc'} && direnv reload", "whence dk   # → …/.venv/bin/dk")


def test_build_next_steps_in_repo_already_loaded(tmp_path: Path) -> None:
    (tmp_path / "tooling.yaml").write_text("project:\n  name: test\n", encoding="utf-8")
    (tmp_path / ".envrc").write_text("PATH_add .venv/bin\n", encoding="utf-8")
    ctx = next_steps_context(
        cwd=tmp_path,
        zshrc_path=tmp_path / ".zshrc",
        zshrc_changed=False,
        environ={"DIRENV_DIR": str(tmp_path)},
    )
    assert build_next_steps(ctx) == ()


def test_build_next_steps_after_zshrc_patch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("catalpa_tooling.shell_setup.shutil.which", lambda _cmd: None)
    (tmp_path / "tooling.yaml").write_text("project:\n  name: test\n", encoding="utf-8")
    (tmp_path / ".envrc").write_text("PATH_add .venv/bin\n", encoding="utf-8")
    zshrc = tmp_path / ".zshrc"
    ctx = next_steps_context(
        cwd=tmp_path,
        zshrc_path=zshrc,
        zshrc_changed=True,
        environ={},
    )
    steps = build_next_steps(ctx)
    assert steps[0] == f"source {zshrc} && direnv reload"


def test_build_next_steps_outside_repo(monkeypatch) -> None:
    monkeypatch.setattr("catalpa_tooling.shell_setup.shutil.which", lambda _cmd: None)
    ctx = next_steps_context(
        cwd=Path("/tmp"),
        zshrc_changed=True,
        environ={},
    )
    steps = build_next_steps(ctx)
    assert steps[0].startswith("source ")
    assert "cd into your tooling repo" in steps[1]
    assert "direnv allow" in steps[2]


def test_remove_catalpa_block(tmp_path: Path) -> None:
    block = build_zshrc_block(include_direnv_hook=True, include_completion=True)
    zshrc = tmp_path / ".zshrc"
    zshrc.write_text(f"# before\n{block}\n# after\n", encoding="utf-8")
    updated, removed = remove_catalpa_block(zshrc.read_text(encoding="utf-8"))
    assert removed is True
    assert MARKER_START not in updated
    assert "# before" in updated
    assert "# after" in updated


def test_plan_remove_idempotent(tmp_path: Path) -> None:
    zshrc = tmp_path / ".zshrc"
    dest = tmp_path / "catalpa" / "direnv.zsh"
    zshrc.write_text("# no block\n", encoding="utf-8")
    plan = plan_remove(zshrc_path=zshrc, catalpa_direnv_dest=dest)
    assert plan.remove_zshrc_block is False
    assert plan.remove_catalpa_direnv is False


def test_apply_remove_round_trip(tmp_path: Path) -> None:
    zshrc = tmp_path / ".zshrc"
    dest = tmp_path / "catalpa" / "direnv.zsh"
    zshrc.write_text("", encoding="utf-8")

    install = plan_setup(zshrc_path=zshrc, catalpa_direnv_dest=dest)
    apply_setup(install)

    remove = plan_remove(zshrc_path=zshrc, catalpa_direnv_dest=dest)
    assert remove.remove_zshrc_block is True
    assert remove.remove_catalpa_direnv is True
    apply_remove(remove)

    assert MARKER_START not in zshrc.read_text(encoding="utf-8")
    assert not dest.exists()
    assert not dest.parent.exists()
