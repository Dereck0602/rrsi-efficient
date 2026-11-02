"""OpenCode prompt resources and workspace skill discovery.

The upstream runtime treats skills as a catalog: the model receives a short
description and asks for the full ``SKILL.md`` only when it needs one.  This
module keeps that boundary while making discovery deterministic for a
Snapshot. Snapshot/workspace skills have the same precedence as OpenCode's
workspace roots (workspace first, then ``.agents``, then bundled files).
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True, slots=True)
class Skill:
    name: str
    path: Path
    description: str
    digest: str
    source: str = "workspace"
    metadata: dict[str, Any] | None = None

    @property
    def key(self) -> str:
        return str((self.metadata or {}).get("skillKey") or self.name)


@dataclass(frozen=True, slots=True)
class Resources:
    system_prompt: str
    skills: tuple[Skill, ...]
    files: tuple[str, ...]

    def skill_manifest(self) -> list[dict[str, str]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "path": str(s.path),
                "sha256": s.digest,
                "source": s.source,
            }
            for s in self.skills
        ]

    def skill(self, name: str) -> Skill | None:
        """Resolve a model-visible name or relative skill path."""
        matches = [
            s
            for s in self.skills
            if name in {s.name, s.path.parent.name, s.path.parent.as_posix()}
        ]
        return matches[0] if len(matches) == 1 else None


def snapshot_config(root: Path) -> dict[str, Any]:
    """Read editable Snapshot defaults without importing host OpenCode state."""
    path = Path(root) / "config.yaml"
    if not path.is_file():
        return {}
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}
    return value if isinstance(value, dict) else {}


def _frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines(keepends=True)
    end = next(
        (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None
    )
    if end is None:
        return {}, text
    try:
        value = yaml.safe_load("".join(lines[1:end])) or {}
    except yaml.YAMLError:
        value = {}
    return (value if isinstance(value, dict) else {}), "".join(lines[end + 1 :])


def _metadata(frontmatter: dict[str, Any]) -> dict[str, Any]:
    """Extract OpenCode's nested metadata while accepting JSON frontmatter."""
    value = frontmatter.get("metadata", {})
    if isinstance(value, str):
        try:
            value = yaml.safe_load(value) or {}
        except yaml.YAMLError:
            value = {}
    if not isinstance(value, dict):
        value = {}
    nested = value.get("opencode", {})
    if isinstance(nested, str):
        try:
            nested = yaml.safe_load(nested) or {}
        except yaml.YAMLError:
            nested = {}
    result = dict(nested) if isinstance(nested, dict) else {}
    for key in (
        "always",
        "skillKey",
        "primaryEnv",
        "os",
        "requires",
        "disableModelInvocation",
    ):
        if key in frontmatter and key not in result:
            result[key] = frontmatter[key]
    return result


def _description(frontmatter: dict[str, Any], body: str) -> str:
    value = frontmatter.get("description", "")
    if not isinstance(value, str) or not value.strip():
        value = next(
            (
                line.strip()
                for line in body.splitlines()
                if line.strip() and not line.startswith("#")
            ),
            "",
        )
    value = str(value).strip()
    return value if len(value) <= 1024 else value[:1021] + "..."


def _skill(path: Path, source: str = "workspace") -> Skill:
    frontmatter, body = _frontmatter(path)
    metadata = _metadata(frontmatter)
    return Skill(
        str(frontmatter.get("name") or path.parent.name)[:64],
        path,
        _description(frontmatter, body),
        hashlib.sha256(path.read_bytes()).hexdigest(),
        source,
        metadata,
    )


def _as_names(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value if item is not None}
    return set()


def _has_bin(name: str) -> bool:
    return shutil.which(name) is not None


def _eligible(skill: Skill, config: dict[str, Any]) -> bool:
    """Apply runtime gating OpenCode performs before exposing a skill."""
    settings = config.get("skills") if isinstance(config.get("skills"), dict) else {}
    metadata = skill.metadata or {}
    key = skill.key
    disabled = _as_names(settings.get("disabled")) | _as_names(
        config.get("disabled_skills")
    )
    if key in disabled or skill.name in disabled:
        return False
    entries = settings.get("entries", {})
    entry = (
        entries.get(key, entries.get(skill.name, {}))
        if isinstance(entries, dict)
        else {}
    )
    if isinstance(entry, dict) and entry.get("enabled") is False:
        return False
    if metadata.get("disableModelInvocation") is True:
        return False
    selected = settings.get("filter", settings.get("allow"))
    selected = selected if selected is not None else config.get("skill_filter")
    if selected is not None:
        allowed = _as_names(selected)
        if allowed and key not in allowed and skill.name not in allowed:
            return False
    # ``always`` controls prompt inclusion in the upstream runtime; it does
    # not make a skill usable when its platform or dependency requirements
    # are unavailable. Continue through the gates below before exposing it.
    if metadata.get("os"):
        systems = _as_names(metadata["os"])
        current = {os.name, os.sys.platform, os.sys.platform.split("-", 1)[0]}
        if hasattr(os, "uname"):
            current.add(os.uname().sysname.lower())
        if not systems.intersection(current):
            return False
    requires = metadata.get("requires")
    if isinstance(requires, str):
        try:
            requires = yaml.safe_load(requires) or {}
        except yaml.YAMLError:
            requires = {}
    if not isinstance(requires, dict):
        requires = {}
    if any(not _has_bin(str(name)) for name in _as_names(requires.get("bins"))):
        return False
    any_bins = _as_names(requires.get("anyBins"))
    if any_bins and not any(_has_bin(name) for name in any_bins):
        return False
    envs = _as_names(requires.get("env"))
    configured_env = entry.get("env", {}) if isinstance(entry, dict) else {}
    envs = {
        name
        for name in envs
        if not (
            os.environ.get(name)
            or (isinstance(configured_env, dict) and configured_env.get(name))
        )
    }
    return not envs


def _roots(root: Path, workspace: Path) -> tuple[tuple[Path, str], ...]:
    # OpenCode discovers project skills from the current project and its
    # ancestor `.agents` directories, then falls back to the Snapshot's
    # bundled catalog.  Keep the search deterministic and let the first name
    # win when the same skill is present at multiple levels.
    roots: list[tuple[Path, str]] = [
        (workspace / "skills", "opencode-workspace"),
        (workspace / ".claude" / "skills", "claude-project"),
    ]
    for parent in (workspace, *workspace.parents):
        roots.append((parent / ".agents" / "skills", "agents-skills-project"))
    roots.append((root / "skills", "opencode-bundled"))
    return tuple(roots)


def load(
    root: Path, workspace: Path, config: dict[str, Any] | None = None
) -> Resources:
    config = config or {}
    root, workspace = Path(root).resolve(), Path(workspace).resolve()
    files: list[str] = []
    chunks: list[str] = []
    prompt = config.get("prompt") if isinstance(config.get("prompt"), dict) else {}
    configured_files = prompt.get("bootstrap_files", prompt.get("instruction_files"))
    names = (
        tuple(str(name) for name in configured_files if str(name).strip())
        if isinstance(configured_files, (list, tuple))
        else (str(configured_files),)
        if isinstance(configured_files, str) and configured_files.strip()
        else (
            "AGENTS.md",
            "SOUL.md",
            "TOOLS.md",
            "IDENTITY.md",
            "USER.md",
            "MEMORY.md",
            "HEARTBEAT.md",
            "SYSTEM.md",
        )
    )
    ancestor_names = {"AGENTS.md", "CLAUDE.md", "CONTEXT.md"}
    for name in names:
        relative = Path(name)
        # Bootstrap names come from editable Snapshot config. They are
        # resource selectors, not arbitrary filesystem paths; reject absolute
        # and parent traversal forms before joining either root.
        if relative.is_absolute() or ".." in relative.parts:
            continue
        bases = (root, workspace)
        if name in ancestor_names:
            # Instruction.system() uses the first project instruction family
            # found while walking ancestors. Include every matching ancestor
            # so a nested task retains the same layered guidance.
            bases = (root, *workspace.parents, workspace)
        for base in bases:
            path = base / name
            try:
                inside = path.resolve().is_relative_to(base.resolve())
            except OSError:
                inside = False
            if path.is_file() and inside and str(path) not in files:
                files.append(str(path))
                chunks.append(
                    "Instructions from: "
                    + str(path)
                    + "\n"
                    + path.read_text(encoding="utf-8", errors="replace")
                )
    skills: list[Skill] = []
    seen: set[str] = set()
    skills_cfg_value = config.get("skills")
    skills_cfg = skills_cfg_value if isinstance(skills_cfg_value, dict) else {}
    skills_mode = str(prompt.get("skills_mode", "available-list")).lower()
    skills_enabled = (
        skills_cfg_value is not False
        and skills_cfg.get("enabled", True) is not False
        and skills_mode
        not in {
            "disabled",
            "off",
            "none",
        }
    )
    for base, source in _roots(root, workspace) if skills_enabled else ():
        # Snapshot roots are regular files, and workspace skill roots must not
        # be symlinked directories that redirect discovery outside their
        # declared workspace. Individual skill paths are checked below too.
        if base.is_symlink():
            continue
        if not base.is_dir():
            continue
        try:
            candidates = sorted(base.rglob("SKILL.md"))
        except OSError:
            candidates = []
        for path in candidates:
            try:
                if not path.is_file() or not path.resolve().is_relative_to(
                    base.resolve()
                ):
                    continue
                skill = _skill(path, source)
            except (OSError, UnicodeError):
                continue
            if skill.name in seen or not _eligible(skill, config):
                continue
            seen.add(skill.name)
            skills.append(skill)
    if skills:
        chunks.append(
            "<available_skills>\n"
            + "\n".join(f"- {s.name}: {s.description} (read_skill)" for s in skills)
            + "\n</available_skills>"
        )
    return Resources("\n\n".join(chunks), tuple(skills), tuple(files))
