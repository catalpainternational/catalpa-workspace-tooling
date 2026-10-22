"""Install agent guardrails (Cursor rules, ``.cursorignore``) and skills into tooling repos.

Sources are bundled under ``share/agents/``. Every installed file carries a managed marker;
a file without it is the project's own and is left alone unless ``force`` is set.

Claude Code gets the same guardrails in its own formats: each Cursor rule is converted to
``.claude/rules/<rule>.md``, and the ``.cursorignore`` patterns become ``Read(...)`` deny rules
merged into ``.claude/settings.json``. JSON has no comments, so that file carries no marker:
only the bundled entries are added or removed, and everything else in it is the project's.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from catalpa_tooling.config import ProjectConfig, ProjectConfigError, load_project_config
from catalpa_tooling.repo_paths import repo_root_from_cwd

REQUIRED_RULES = ("secrets-and-agents", "remote-environments")
OPTIONAL_RULES = ("worktrees", "smoke-tests", "oss-compliance")
SKILLS = ("dk-stack",)
DEFAULT_SKILLS = ("dk-stack",)

CURSORIGNORE = ".cursorignore"
RULES_DIR = Path(".cursor/rules")
CLAUDE_RULES_DIR = Path(".claude/rules")
CLAUDE_SETTINGS = Path(".claude/settings.json")
CLAUDE_SKILLS_DIR = Path(".claude/skills")
CURSOR_SKILLS_DIR = Path(".cursor/skills")

MANAGED_MARKER = "Managed by catalpa-workspace-tooling setup-agents"
_MARKER_NOTE = (
    f"{MANAGED_MARKER}: `uv run setup-agents` overwrites edits. "
    "Delete this line to keep a local version."
)


def _bundle() -> Traversable:
    return files("catalpa_tooling").joinpath("share/agents")


def with_marker(text: str, *, name: str) -> str:
    """Return ``text`` with the managed marker, placed so the file still parses.

    ``.md``/``.mdc`` files get an HTML comment after their YAML frontmatter (frontmatter must
    stay first); anything else gets a ``#`` comment on the first line.
    """
    if not name.endswith((".md", ".mdc")):
        return f"# {_MARKER_NOTE}\n{text}"
    comment = f"<!-- {_MARKER_NOTE} -->\n"
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            split = end + len("\n---\n")
            return text[:split] + "\n" + comment + text[split:]
    return comment + "\n" + text


def cursor_rule_to_claude(text: str) -> str:
    """Convert a Cursor ``.mdc`` rule to a Claude Code ``.claude/rules`` file.

    Claude Code reads only ``paths:`` from rule frontmatter and loads a rule without it at
    every launch. A Cursor rule scoped by ``globs`` keeps that scope as ``paths``; any other
    rule (``alwaysApply: true``, or picked by the agent from its description) always loads.
    """
    body = text
    globs: list[str] = []
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                key, _, value = line.partition(":")
                if key.strip() == "globs" and value.strip():
                    globs = [g.strip() for g in value.split(",") if g.strip()]
            body = text[end + len("\n---\n") :].lstrip("\n")
    if not globs:
        return body
    paths = "".join(f'  - "{g}"\n' for g in globs)
    return f"---\npaths:\n{paths}---\n\n{body}"


def claude_deny_rules(cursorignore: str) -> list[str]:
    """``Read(...)`` deny rules for each ``.cursorignore`` pattern.

    Both use gitignore syntax. A pattern with a ``/`` is anchored at the repo root, which a
    leading ``/`` gives in project settings (a bare ``./`` would follow Claude Code's working
    directory instead). Negations have no equivalent here and are dropped, so a file they
    exempt (``.env.example``) stays hidden from Claude Code.
    """
    rules: list[str] = []
    for line in cursorignore.splitlines():
        pattern = line.strip()
        if not pattern or pattern.startswith(("#", "!")):
            continue
        if "/" in pattern.rstrip("/") and not pattern.startswith("/"):
            pattern = "/" + pattern
        rules.append(f"Read({pattern})")
    return rules


@dataclass(frozen=True)
class SettingsChange:
    """New ``.claude/settings.json`` text, or ``None`` to delete a file left empty."""

    path: Path
    content: str | None


@dataclass(frozen=True)
class ManagedFile:
    path: Path
    content: str
    bundled: str  # the bundled text without the marker, for adopting identical copies


@dataclass(frozen=True)
class ManagedLink:
    path: Path
    target: str


@dataclass(frozen=True)
class EntryStatus:
    path: Path
    present: bool
    managed: bool
    current: bool


@dataclass(frozen=True)
class AgentsSetupStatus:
    repo_root: Path
    entries: tuple[EntryStatus, ...]

    @property
    def ready(self) -> bool:
        return all(e.present and e.managed and e.current for e in self.entries)


@dataclass(frozen=True)
class AgentsSetupPlan:
    repo_root: Path
    writes: tuple[ManagedFile, ...]
    links: tuple[ManagedLink, ...]
    removals: tuple[Path, ...]
    skipped: tuple[Path, ...]  # unmanaged and different: left alone without --force
    settings: SettingsChange | None = None

    @property
    def empty(self) -> bool:
        return not (self.writes or self.links or self.removals or self.settings)


def selected_rules(config: ProjectConfig) -> tuple[str, ...]:
    unknown = [r for r in config.agents.rules if r not in OPTIONAL_RULES]
    if unknown:
        raise ProjectConfigError(
            f"agents.rules: unknown {', '.join(unknown)} "
            f"(available: {', '.join(OPTIONAL_RULES)}; always installed: "
            f"{', '.join(REQUIRED_RULES)})"
        )
    return REQUIRED_RULES + tuple(r for r in OPTIONAL_RULES if r in config.agents.rules)


def selected_skills(config: ProjectConfig) -> tuple[str, ...]:
    chosen = DEFAULT_SKILLS if config.agents.skills is None else config.agents.skills
    unknown = [s for s in chosen if s not in SKILLS]
    if unknown:
        raise ProjectConfigError(
            f"agents.skills: unknown {', '.join(unknown)} (available: {', '.join(SKILLS)})"
        )
    return tuple(s for s in SKILLS if s in chosen)


def _managed_file(root: Path, rel: Path, source: Traversable) -> ManagedFile:
    bundled = source.read_text(encoding="utf-8")
    return ManagedFile(
        path=root / rel, content=with_marker(bundled, name=rel.name), bundled=bundled
    )


def _skill_files(root: Path, skill: str) -> list[ManagedFile]:
    out: list[ManagedFile] = []

    def walk(node: Traversable, rel: Path) -> None:
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            if child.is_dir():
                walk(child, rel / child.name)
            elif child.is_file():
                out.append(_managed_file(root, rel / child.name, child))

    walk(_bundle().joinpath("skills", skill), CLAUDE_SKILLS_DIR / skill)
    return out


def _skill_link(root: Path, skill: str) -> ManagedLink:
    return ManagedLink(
        path=root / CURSOR_SKILLS_DIR / skill,
        target=os.path.join("..", "..", str(CLAUDE_SKILLS_DIR), skill),
    )


def _rule_files(root: Path, rule: str) -> list[ManagedFile]:
    rel = RULES_DIR / f"{rule}.mdc"
    cursor = _managed_file(root, rel, _bundle().joinpath("rules", rel.name))
    claude_rel = CLAUDE_RULES_DIR / f"{rule}.md"
    claude_text = cursor_rule_to_claude(cursor.bundled)
    claude = ManagedFile(
        path=root / claude_rel,
        content=with_marker(claude_text, name=claude_rel.name),
        bundled=claude_text,
    )
    return [cursor, claude]


def _bundled_deny_rules() -> list[str]:
    return claude_deny_rules(_bundle().joinpath("cursorignore").read_text(encoding="utf-8"))


def _read_settings(path: Path) -> dict | None:
    """Parsed settings, ``{}`` when the file is absent, ``None`` when it is not a JSON object."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    permissions = data.get("permissions", {})
    if not isinstance(permissions, dict) or not isinstance(permissions.get("deny", []), list):
        return None
    return data


def _deny_list(data: dict) -> list:
    return data.get("permissions", {}).get("deny", [])


def _settings_text(data: dict) -> str:
    return json.dumps(data, indent=2) + "\n"


def _settings_with_rules(path: Path, rules: list[str]) -> SettingsChange | None:
    data = _read_settings(path)
    if data is None:
        return None
    missing = [r for r in rules if r not in _deny_list(data)]
    if not missing:
        return None
    data.setdefault("permissions", {}).setdefault("deny", []).extend(missing)
    return SettingsChange(path=path, content=_settings_text(data))


def _settings_without_rules(path: Path, rules: list[str]) -> SettingsChange | None:
    data = _read_settings(path)
    if not data or not any(r in _deny_list(data) for r in rules):
        return None
    permissions = data["permissions"]
    permissions["deny"] = [r for r in permissions["deny"] if r not in rules]
    if not permissions["deny"]:
        del permissions["deny"]
    if not permissions:
        del data["permissions"]
    return SettingsChange(path=path, content=_settings_text(data) if data else None)


def _desired(root: Path, config: ProjectConfig) -> tuple[list[ManagedFile], list[ManagedLink]]:
    wanted = [_managed_file(root, Path(CURSORIGNORE), _bundle().joinpath("cursorignore"))]
    for r in selected_rules(config):
        wanted += _rule_files(root, r)
    links: list[ManagedLink] = []
    for skill in selected_skills(config):
        wanted += _skill_files(root, skill)
        links.append(_skill_link(root, skill))
    return wanted, links


def _all_known(root: Path) -> tuple[list[ManagedFile], list[ManagedLink]]:
    known = [_managed_file(root, Path(CURSORIGNORE), _bundle().joinpath("cursorignore"))]
    for r in REQUIRED_RULES + OPTIONAL_RULES:
        known += _rule_files(root, r)
    links: list[ManagedLink] = []
    for skill in SKILLS:
        known += _skill_files(root, skill)
        links.append(_skill_link(root, skill))
    return known, links


def _is_managed(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        return MANAGED_MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _text_matches(path: Path, expected: str) -> bool:
    try:
        return path.is_file() and path.read_text(encoding="utf-8") == expected
    except (OSError, UnicodeDecodeError):
        return False


def _link_matches(link: ManagedLink) -> bool:
    return link.path.is_symlink() and os.readlink(link.path) == link.target


def _load(repo_root: Path | None) -> tuple[Path, ProjectConfig]:
    root = repo_root or repo_root_from_cwd()
    return root, load_project_config(root)


def inspect_status(*, repo_root: Path | None = None) -> AgentsSetupStatus:
    root, config = _load(repo_root)
    wanted, links = _desired(root, config)
    entries = [
        EntryStatus(
            path=f.path,
            present=f.path.is_file(),
            managed=_is_managed(f.path),
            current=_text_matches(f.path, f.content),
        )
        for f in wanted
    ]
    entries += [
        EntryStatus(
            path=link.path,
            present=link.path.is_symlink() or link.path.exists(),
            managed=_link_matches(link),
            current=_link_matches(link),
        )
        for link in links
    ]
    settings_path = root / CLAUDE_SETTINGS
    settings = _read_settings(settings_path)
    entries.append(
        EntryStatus(
            path=settings_path,
            present=settings_path.is_file(),
            # Readable, so setup-agents can merge into it.
            managed=settings is not None,
            current=settings is not None
            and all(r in _deny_list(settings) for r in _bundled_deny_rules()),
        )
    )
    return AgentsSetupStatus(repo_root=root, entries=tuple(entries))


def plan_setup(*, repo_root: Path | None = None, force: bool = False) -> AgentsSetupPlan:
    root, config = _load(repo_root)
    wanted, links = _desired(root, config)

    writes: list[ManagedFile] = []
    skipped: list[Path] = []
    for f in wanted:
        if _text_matches(f.path, f.content):
            continue
        if (
            force
            or not f.path.exists()
            or _is_managed(f.path)
            or _text_matches(f.path, f.bundled)  # an unmarked copy of the same file: adopt it
        ):
            writes.append(f)
        else:
            skipped.append(f.path)

    new_links: list[ManagedLink] = []
    for link in links:
        if _link_matches(link):
            continue
        # Only ever replace a symlink; a real directory here is the project's own.
        if not link.path.exists() and not link.path.is_symlink():
            new_links.append(link)
        elif force and link.path.is_symlink():
            new_links.append(link)
        else:
            skipped.append(link.path)

    # Optional files dropped from tooling.yaml since they were installed.
    wanted_paths = {f.path for f in wanted} | {link.path for link in links}
    known, known_links = _all_known(root)
    removals = [f.path for f in known if f.path not in wanted_paths and _is_managed(f.path)]
    removals += [
        link.path
        for link in known_links
        if link.path not in wanted_paths and _link_matches(link)
    ]

    # Merged, never replaced, so --force does not apply: an unreadable file is skipped.
    settings_path = root / CLAUDE_SETTINGS
    if _read_settings(settings_path) is None:
        skipped.append(settings_path)

    return AgentsSetupPlan(
        repo_root=root,
        writes=tuple(writes),
        links=tuple(new_links),
        removals=tuple(removals),
        skipped=tuple(skipped),
        settings=_settings_with_rules(settings_path, _bundled_deny_rules()),
    )


def plan_remove(*, repo_root: Path | None = None) -> AgentsSetupPlan:
    root = repo_root or repo_root_from_cwd()
    known, known_links = _all_known(root)
    removals = [f.path for f in known if _is_managed(f.path)]
    removals += [link.path for link in known_links if _link_matches(link)]
    return AgentsSetupPlan(
        repo_root=root,
        writes=(),
        links=(),
        removals=tuple(removals),
        skipped=(),
        settings=_settings_without_rules(root / CLAUDE_SETTINGS, _bundled_deny_rules()),
    )


def _rel(plan: AgentsSetupPlan, path: Path) -> Path:
    try:
        return path.relative_to(plan.repo_root)
    except ValueError:
        return path


def _prune_empty_dirs(plan: AgentsSetupPlan) -> None:
    candidates = {p.parent for p in plan.removals}
    candidates |= {
        plan.repo_root / d
        for d in (
            RULES_DIR,
            CLAUDE_RULES_DIR,
            CLAUDE_SKILLS_DIR,
            CURSOR_SKILLS_DIR,
            Path(".claude"),
            Path(".cursor"),
        )
    }
    # Deepest first, so a parent empties out before it is checked.
    for d in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
        if d != plan.repo_root and d.is_dir() and not any(d.iterdir()):
            d.rmdir()


def apply_setup(plan: AgentsSetupPlan, *, dry_run: bool = False) -> None:
    verb = "Would write" if dry_run else "Wrote"
    for f in plan.writes:
        print(f"{verb} {_rel(plan, f.path)}")
        if not dry_run:
            f.path.parent.mkdir(parents=True, exist_ok=True)
            f.path.write_text(f.content, encoding="utf-8")

    verb = "Would link" if dry_run else "Linked"
    for link in plan.links:
        print(f"{verb} {_rel(plan, link.path)} -> {link.target}")
        if not dry_run:
            link.path.parent.mkdir(parents=True, exist_ok=True)
            if link.path.is_symlink():
                link.path.unlink()
            link.path.symlink_to(link.target, target_is_directory=True)

    apply_remove(plan, dry_run=dry_run)


def apply_remove(plan: AgentsSetupPlan, *, dry_run: bool = False) -> None:
    verb = "Would remove" if dry_run else "Removed"
    for path in plan.removals:
        print(f"{verb} {_rel(plan, path)}")
        if not dry_run:
            path.unlink(missing_ok=True)

    change = plan.settings
    if change is not None:
        if change.content is None:
            print(f"{verb} {_rel(plan, change.path)}")
        else:
            print(f"{'Would update' if dry_run else 'Updated'} {_rel(plan, change.path)}")
        if not dry_run:
            if change.content is None:
                change.path.unlink(missing_ok=True)
            else:
                change.path.parent.mkdir(parents=True, exist_ok=True)
                change.path.write_text(change.content, encoding="utf-8")

    if not dry_run and (plan.removals or (change is not None and change.content is None)):
        _prune_empty_dirs(plan)
