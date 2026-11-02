"""Snapshot-scoped Hermes prompt assembly and progressive skill disclosure.

The source helpers in upstream_prompt/upstream_skills come from the pinned
Hermes release. This module replaces its ambient profile/config discovery with
explicit Snapshot paths; no user Hermes installation or home is consulted.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

import yaml

from . import upstream_coding as coding
from . import upstream_prompt as prompt
from . import upstream_skills as skills
from .runtime_context import runtime_context

DEFAULT_TOOLS = [
    "terminal",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "todo_list",
    "skills_list",
    "skill_view",
]
TOOLSETS = {
    "terminal": ["terminal"],
    "file": ["read_file", "write_file", "patch", "search_files"],
    "todo": ["todo_list"],
    "skills": ["skills_list", "skill_view"],
    "session_search": ["session_search"],
}


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    category: str | None
    metadata: dict[str, Any]
    content: str


@dataclass
class ResourceBundle:
    system_prompt: str
    enabled_tools: list[str] | None
    diagnostics: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    skills: list[Skill] = field(default_factory=list)


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(result.get(key), dict) and isinstance(value, dict)
            else copy.deepcopy(value)
        )
    return result


def read_settings(snapshot_root: Path, config: dict | None = None) -> dict:
    path = Path(snapshot_root) / "config.yaml"
    settings = (
        yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    )
    if settings is None:
        settings = {}
    if not isinstance(settings, dict):
        raise ValueError("Hermes Snapshot config.yaml must contain a mapping")
    overrides = config or {}
    selectors = ("enabled_tools", "tools", "enabled_toolsets", "toolsets")
    # The role can select tools through any accepted spelling. A default
    # enabled_tools list from the Snapshot must not shadow role toolsets (or
    # vice versa) merely because its spelling has higher parsing precedence.
    if any(key in overrides for key in selectors):
        settings = {
            key: value for key, value in settings.items() if key not in selectors
        }
    return _merge(settings, overrides)


def enabled_tool_names(settings: dict) -> list[str]:
    names = settings.get("enabled_tools", settings.get("tools"))
    if names is None:
        selected = settings.get("enabled_toolsets", settings.get("toolsets"))
        if selected is None:
            names = DEFAULT_TOOLS
        else:
            if not isinstance(selected, list) or any(
                n not in TOOLSETS for n in selected
            ):
                raise ValueError(
                    "Hermes toolsets must select terminal, file, todo or skills"
                )
            names = [name for group in selected for name in TOOLSETS[group]]
    if not isinstance(names, list) or any(not isinstance(n, str) for n in names):
        raise ValueError("Hermes enabled_tools must be a list of tool names")
    unknown = set(names) - set(DEFAULT_TOOLS) - {"session_search"}
    if unknown:
        raise ValueError("Unsupported Hermes tools: " + ", ".join(sorted(unknown)))
    disabled = settings.get("disabled_toolsets", [])
    if not isinstance(disabled, list) or any(n not in TOOLSETS for n in disabled):
        raise ValueError("Unsupported Hermes disabled_toolsets")
    removed = {name for group in disabled for name in TOOLSETS[group]}
    return list(dict.fromkeys(name for name in names if name not in removed))


def discover_skills(
    snapshot_root: Path, diagnostics: list[str] | None = None
) -> list[Skill]:
    root = Path(snapshot_root) / "skills"
    diagnostics = diagnostics if diagnostics is not None else []
    settings = read_settings(snapshot_root)
    skill_config = settings.get("skills", {})
    disabled_raw = (
        skill_config.get("disabled", []) if isinstance(skill_config, dict) else []
    )
    disabled = (
        {n for n in disabled_raw if isinstance(n, str)}
        if isinstance(disabled_raw, list)
        else set()
    )
    disabled.discard("hermes-agent")  # Upstream's essential operating-manual skill.
    scanned, originals = [], {}
    for path in skills.iter_skill_index_files(root, "SKILL.md"):
        try:
            # Snapshot exports are regular files; containment also protects
            # direct Python callers from symlinks outside their declared root.
            if not path.resolve().is_relative_to(root.resolve()):
                continue
            content = path.read_text(encoding="utf-8-sig", errors="replace")
            metadata, body = skills.parse_frontmatter(content[:4000])
            if not skills.skill_matches_platform(metadata):
                continue
            description = metadata.get("description", "") or next(
                (
                    line
                    for line in map(str.strip, body.splitlines())
                    if line and not line.startswith("#")
                ),
                "",
            )
            if not isinstance(description, str):
                raise ValueError("skill description must be a string")
            description = (
                description if len(description) <= 1024 else description[:1021] + "..."
            )
            name = str(metadata.get("name", path.parent.name))[:64]
            parts = path.relative_to(root).parts
            category = parts[0] if len(parts) >= 3 else None
            scanned.append(
                {
                    "name": name,
                    "description": description,
                    "category": category,
                    "tier": 1,
                    "root": root,
                    "path": path,
                }
            )
            originals[path] = (skills.parse_frontmatter(content)[0], content)
        except (OSError, ValueError, TypeError) as exc:
            diagnostics.append(f"Could not load skill {path}: {exc}")
    result = []
    for entry in skills.resolve_skill_catalog(scanned):
        if not entry["load_name"] or skills.is_disabled_entry(entry, disabled):
            continue
        metadata, content = originals[entry["path"]]
        result.append(
            Skill(
                entry["load_name"],
                entry["description"],
                entry["path"],
                entry["category"],
                metadata,
                content,
            )
        )
    return sorted(result, key=lambda s: (s.category or "", s.name))


def skills_list(snapshot_root: Path, category: str | None = None) -> str:
    catalog = discover_skills(snapshot_root)
    if not catalog:
        return json.dumps(
            {
                "success": True,
                "skills": [],
                "categories": [],
                "message": "No skills found in skills/ directory.",
            },
            ensure_ascii=False,
        )
    if category:
        catalog = [s for s in catalog if s.category == category]
    return json.dumps(
        {
            "success": True,
            "skills": [
                {"name": s.name, "description": s.description, "category": s.category}
                for s in catalog
            ],
            "categories": sorted({s.category for s in catalog if s.category}),
            "count": len(catalog),
            "hint": "Use skill_view(name) to see full content, tags, and linked files",
        },
        ensure_ascii=False,
    )


def _tags(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if t]
    return [
        t.strip().strip("\"'")
        for t in str(value).strip().strip("[]").split(",")
        if t.strip()
    ]


def _linked_files(root: Path) -> dict:
    specs = (
        ("references", ["*.md"], False, False),
        (
            "templates",
            ["*.md", "*.py", "*.yaml", "*.yml", "*.json", "*.tex", "*.sh"],
            True,
            False,
        ),
        ("assets", ["*"], True, True),
        ("scripts", ["*.py", "*.sh", "*.bash", "*.js", "*.ts", "*.rb"], False, False),
    )
    result = {}
    for name, globs, recursive, files_only in specs:
        base = root / name
        found = [
            f.relative_to(root).as_posix()
            for g in globs
            if base.exists()
            for f in (base.rglob(g) if recursive else base.glob(g))
            if (not files_only or f.is_file())
            and f.resolve().is_relative_to(root.resolve())
        ]
        if found:
            result[name] = found
    return result


def skill_view(snapshot_root: Path, name: str, file_path: str | None = None) -> str:
    def fail(message: str, **extra) -> str:
        return json.dumps(
            {"success": False, "error": message, **extra}, ensure_ascii=False
        )

    if (
        not isinstance(name, str)
        or not name
        or Path(name).is_absolute()
        or PureWindowsPath(name).drive
        or ".." in name.replace("\\", "/").split("/")
    ):
        return fail("Skill name must be a relative skill name or path.")
    catalog = discover_skills(snapshot_root)
    root = Path(snapshot_root) / "skills"
    matching = [
        s
        for s in catalog
        if name in {s.name, s.path.parent.relative_to(root).as_posix()}
    ]
    if len(matching) != 1:
        return fail(
            f"Skill '{name}' not found."
            if not matching
            else f"Skill '{name}' is ambiguous. Use its exact path."
        )
    skill = matching[0]
    if file_path is not None:
        if not isinstance(file_path, str):
            return fail("file_path must be a string")
        if ".." in file_path.replace("\\", "/").split("/"):
            return fail(
                "Path traversal ('..') is not allowed.",
                hint="Use a relative path within the skill directory",
            )
        target = skill.path.parent / file_path
        if (
            Path(file_path).is_absolute()
            or PureWindowsPath(file_path).drive
            or not target.resolve().is_relative_to(skill.path.parent.resolve())
        ):
            return fail(
                "Path must stay within the skill directory.",
                hint="Use a relative path within the skill directory",
            )
        if not target.is_file():
            return fail(f"File '{file_path}' not found in skill '{name}'.")
        return json.dumps(
            {
                "success": True,
                "name": name,
                "file": file_path,
                "content": target.read_text(encoding="utf-8-sig", errors="replace"),
                "file_type": target.suffix,
            },
            ensure_ascii=False,
        )
    metadata = skill.metadata.get("metadata")
    hermes = metadata.get("hermes", {}) if isinstance(metadata, dict) else {}
    hermes = hermes if isinstance(hermes, dict) else {}
    linked = _linked_files(skill.path.parent)
    result = {
        "success": True,
        "name": skill.metadata.get("name", skill.path.parent.name),
        "description": skill.metadata.get("description", ""),
        "tags": _tags(hermes.get("tags") or skill.metadata.get("tags", "")),
        "related_skills": _tags(
            hermes.get("related_skills") or skill.metadata.get("related_skills", "")
        ),
        "content": skill.content,
        "path": str(skill.path.relative_to(root)),
        "skill_dir": str(skill.path.parent),
        "linked_files": linked or None,
        "usage_hint": "To view linked files, call skill_view(name, file_path) where file_path is e.g. 'references/api.md' or 'assets/config.yaml'"
        if linked
        else None,
        "required_environment_variables": [],
        "required_commands": [],
        "missing_required_environment_variables": [],
        "missing_credential_files": [],
        "missing_required_commands": [],
        "setup_needed": False,
        "setup_skipped": False,
        "readiness_status": "available",
    }
    if skill.metadata.get("compatibility"):
        result["compatibility"] = skill.metadata["compatibility"]
    if isinstance(metadata, dict):
        result["metadata"] = metadata
    # Automatic setup and executable skill preprocessing belong to the full
    # Hermes profile runtime; the Snapshot adapter preserves authored content.
    if (
        skill.metadata.get("deps")
        or skill.metadata.get("required_environment_variables")
        or skill.metadata.get("required_credential_files")
    ):
        result.update(
            setup_needed=True,
            readiness_status="setup_needed",
            setup_note="This skill declares setup requirements; prepare them in the task environment before using it.",
        )
    return json.dumps(result, ensure_ascii=False)


def _skill_prompt(
    snapshot_root: Path,
    catalog: list[Skill],
    names: set[str],
    single_query: bool,
    compact_categories: frozenset[str] | None = None,
) -> str:
    if not catalog or not names.intersection({"skills_list", "skill_view"}):
        return ""
    root = Path(snapshot_root) / "skills"
    categories: dict[str, list[tuple[str, str]]] = {}
    descriptions: dict[str, str] = {}
    for skill in catalog:
        parts = skill.path.relative_to(root).parts
        category = (
            "general"
            if len(parts) < 2
            else "/".join(parts[:-2])
            if len(parts) > 2
            else parts[0]
        )
        categories.setdefault(category, []).append(
            (skill.name, skills.extract_skill_description(skill.metadata))
        )
    for path in skills.iter_skill_index_files(root, "DESCRIPTION.md"):
        if not path.resolve().is_relative_to(root.resolve()):
            continue
        try:
            description = skills.parse_frontmatter(
                path.read_text(encoding="utf-8-sig")
            )[0].get("description")
            if description:
                parts = path.relative_to(root).parts
                descriptions["/".join(parts[:-1]) if len(parts) > 1 else "general"] = (
                    str(description).strip().strip("'\"")
                )
        except (OSError, ValueError):
            continue
    return prompt._render_skills_index(
        categories,
        descriptions,
        compact_categories,
        names,
        single_query_session=single_query,
    )


def load_resources(snapshot_root: Path, cwd: Path, config: dict) -> ResourceBundle:
    from .compression import resolve_context_length

    root, cwd = Path(snapshot_root), Path(cwd).resolve()
    settings = read_settings(root, config)
    context_length = resolve_context_length(settings, settings.get("llm", {}))
    enabled = enabled_tool_names(settings)
    names = set(enabled)
    diagnostics: list[str] = []
    token = prompt._resource_settings.set(settings)
    try:
        soul = root / "SOUL.md"
        wants_soul = settings.get("load_soul_identity", False) or not settings.get(
            "skip_context_files", False
        )
        soul_content = (
            soul.read_text(encoding="utf-8-sig").strip()
            if wants_soul and soul.is_file()
            else ""
        )
        identity = (
            prompt._truncate_content(
                soul_content,
                "SOUL.md",
                read_path=str(soul),
                context_length=context_length,
            )
            if soul_content
            else prompt.DEFAULT_AGENT_IDENTITY
        )
        stable = [identity, prompt.HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS]
        agent_config = settings.get("agent", {})
        agent_config = agent_config if isinstance(agent_config, dict) else {}
        model = settings.get("llm", {}).get("model_name", "")
        if names:
            if agent_config.get("task_completion_guidance", True):
                stable.append(prompt.TASK_COMPLETION_GUIDANCE)
            if agent_config.get("parallel_tool_call_guidance", True):
                stable.append(prompt.PARALLEL_TOOL_CALL_GUIDANCE)
            if "session_search" in names:
                stable.append(
                    "When the user references something from a past conversation or you suspect relevant cross-session context exists, use session_search to recall it before asking them to repeat themselves."
                )
            stable.append(prompt.STEER_CHANNEL_NOTE)
            if prompt._model_gate(
                agent_config.get("tool_use_enforcement", "auto"),
                model,
                prompt.TOOL_USE_ENFORCEMENT_MODELS,
            ):
                stable.append(prompt.TOOL_USE_ENFORCEMENT_GUIDANCE)
                if any(name in model.lower() for name in ("gemini", "gemma")):
                    stable.append(prompt.GOOGLE_MODEL_OPERATIONAL_GUIDANCE)
            if prompt._model_gate(
                agent_config.get("execution_guidance", "auto"),
                model,
                prompt.EXECUTION_GUIDANCE_MODELS,
            ):
                stable.append(prompt.execution_guidance_text(names))
        coding_mode = (
            coding.resolve_runtime_mode(
                platform=settings.get("platform"), cwd=cwd, config=settings, model=model
            )
            if names
            else None
        )
        coding_prefix, coding_workspace, coding_trailing = (
            coding_mode.system_prompt_parts(valid_tool_names=names)
            if coding_mode is not None
            else ([], [], [])
        )
        stable.extend(coding_prefix)
        context = []
        for path in sorted((root / "prompts").glob("*.md")):
            text = path.read_text(encoding="utf-8-sig").strip()
            if text:
                context.append(text)
        if settings.get("system_message"):
            context.append(str(settings["system_message"]))
        if not settings.get("skip_context_files", False):
            project = (
                prompt._load_hermes_md(cwd, context_length=context_length)
                or prompt._load_agents_md(cwd, context_length=context_length)
                or prompt._load_claude_md(cwd, context_length=context_length)
                or prompt._load_cursorrules(cwd, context_length=context_length)
            )
            if project:
                context.append(
                    "# Project Context\n\nThe following project context files have been loaded and should be followed:\n\n"
                    + project
                )
        if coding_workspace:
            context.extend([*coding_workspace, *coding_trailing])
        else:
            stable.extend(coding_trailing)
        catalog = discover_skills(root, diagnostics)
        index = _skill_prompt(
            root,
            catalog,
            names,
            bool(settings.get("single_query_session", False)),
            coding_mode.compact_skill_categories() if coding_mode is not None else None,
        )
        if "skill_view" in names and "- hermes-agent:" in index:
            stable[1] = prompt.HERMES_AGENT_HELP_GUIDANCE
        volatile = [index]
        # No host profile, memory DB, plugin runtime or gateway is read here.
        # These two local facts are supplied from the actual task execution.
        if settings.get("include_runtime_context", True):
            profile_line, timestamp, environment = runtime_context(root, cwd, settings)
            volatile.extend([profile_line, timestamp])
            environment = environment.replace(
                prompt.RUNTIME_ENVIRONMENT_HEADING,
                "> " + prompt.RUNTIME_ENVIRONMENT_HEADING,
            )
            volatile.append(
                f"{prompt.RUNTIME_ENVIRONMENT_HEADING}\n\n{environment}\n\n{prompt.RUNTIME_ENVIRONMENT_END}"
            )
        diagnostics.extend(prompt.drain_truncation_warnings())
        rendered = "\n\n".join(
            part
            for part in (
                prompt._join_tier(stable),
                prompt._join_tier(context),
                prompt._join_tier(volatile),
            )
            if part
        )
        # Prompt refreshes after tool/provider changes must retain this call's role.
        if config.get("_worker_system_prompt"):
            rendered += "\n\n" + config["_worker_system_prompt"]
        return ResourceBundle(rendered, enabled, diagnostics, settings, catalog)
    finally:
        prompt._resource_settings.reset(token)
