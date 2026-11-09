"""Versioned semantic adapters for the *editable* surface of each harness.

Classification is evidence for search bookkeeping, not a replacement for the
critic's wiring review. Profiles live outside candidate snapshots. In particular,
an identifier in a comment or an unchanged diff context cannot earn exploration.
"""

from __future__ import annotations

import ast
import copy
import difflib
import json
import re
import tomllib
from pathlib import PurePosixPath

import yaml

VERSION = "harness-v1"
UNKNOWN = "unclassified"
TAGS = frozenset(
    {
        "prompt",
        "skill",
        "config",
        "control_flow",
        "context_mgmt",
        "output_plumbing",
        "client_tool",
        "memory",
        "subagent",
    }
)
PROFILES = frozenset(
    {"miniagent", "codex", "claude_code", "pi", "hermes", "openclaw", "opencode"}
)

# These are semantic signals on changed executable code or configuration keys,
# never on the Worker's explanation. Native file roles below supply stronger
# evidence for implementations that use different names.
SIGNALS = {
    "control_flow": r"\b(?:retry|retries|turn|turns|stop|finaliz\w*|gate|loop|budget|middleware)\b",
    "context_mgmt": r"\b(?:context|compact\w*|history|messages|elid\w*|trim\w*|summari\w*)\b",
    "output_plumbing": r"\b(?:trace|serializ\w*|deserializ\w*|parse\w*|output|response|answer|usage|stream\w*)\b",
    "client_tool": r"\b(?:tool\w*|dispatch|shell|mcp|lsp|exec\w*|read_file|write_file)\b",
    "memory": r"\b(?:memory|memories|remember|recall)\b",
    "subagent": r"\b(?:subagent\w*|sub_agent\w*|subcall|spawn_agent|delegate\w*)\b",
    "skill": r"\b(?:skill\w*)\b",
    "config": r"\b(?:config\w*|settings|options|parameters)\b",
}

# Basenames refer to the vendored runtimes, not similarly named files elsewhere.
RUNTIME_ROLES = {
    "loop": "control_flow",
    "agent": "control_flow",
    "controls": "control_flow",
    "policy": "control_flow",
    "provider_retry": "control_flow",
    "iteration_budget": "control_flow",
    "compact": "context_mgmt",
    "remote_compact": "context_mgmt",
    "compaction": "context_mgmt",
    "compaction_runtime": "context_mgmt",
    "context": "context_mgmt",
    "context_budget": "context_mgmt",
    "history": "context_mgmt",
    "branch_summary": "context_mgmt",
    "trace": "output_plumbing",
    "llm": "output_plumbing",
    "client": "output_plumbing",
    "session": "output_plumbing",
    "stream_json": "output_plumbing",
    "stream_transport": "output_plumbing",
    "tools": "client_tool",
    "tool_result": "client_tool",
    "editor": "client_tool",
    "reader": "client_tool",
    "shell": "client_tool",
    "shell_snapshot": "client_tool",
    "mcp": "client_tool",
    "tool_search": "client_tool",
    "apply_patch": "client_tool",
    "code_mode": "client_tool",
    "code_mode_spec": "client_tool",
    "code_mode_worker": "client_tool",
    "subagents": "subagent",
    "subagents_v2": "subagent",
    "memory": "memory",
    "hooks": "control_flow",
    "models": "config",
    "web_config": "config",
    "model_limits": "config",
    "provider_options": "config",
    "openai_options": "config",
    "retry": "control_flow",
    "recovery": "control_flow",
    "native_continuation": "control_flow",
    "compression": "context_mgmt",
    "native_compaction": "context_mgmt",
    "runtime_context": "context_mgmt",
    "chat_transport": "output_plumbing",
    "anthropic_transport": "output_plumbing",
    "native_responses": "output_plumbing",
    "anthropic_stream": "output_plumbing",
    "openai_stream": "output_plumbing",
    "upstream_skills": "skill",
    "model_catalog": "config",
}


def profile_name(snapshot):
    return snapshot.agent_type if snapshot.agent_type in PROFILES else "generic"


def available_components(snapshot, custom):
    """Do not reserve an opaque CLI's inaccessible runtime/memory components."""
    roles = set(snapshot.component_rules)
    available = {"prompt"}
    available.update({"skill", "config", "memory"} & roles)
    for role, tag in (
        ("tool", "client_tool"),
        ("agent", "subagent"),
        ("hook", "control_flow"),
    ):
        if role in roles:
            available.add(tag)
    if roles & {"code", "runtime"}:
        available.update(TAGS)
    if profile_name(snapshot) == "claude_code" and "hook" in roles:
        # PreCompact and structured hook output are extension surfaces; the
        # adapter disables native auto-memory and does not expose the CLI core.
        available.update({"context_mgmt", "output_plumbing"})
    available.update(custom)
    return sorted(available)


def _text(entry):
    return entry.content.decode("utf-8", errors="replace") if entry else ""


class _NoDocstrings(ast.NodeTransformer):
    def generic_visit(self, node):
        node = super().generic_visit(node)
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            if node.body and isinstance(node.body[0], ast.Expr):
                value = node.body[0].value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    node.body.pop(0)
        return node


def _python_change(before, after):
    """Return code, text, unchanged, or invalid; strip comments/docstrings first."""
    try:
        trees = [_NoDocstrings().visit(ast.parse(s)) for s in (before, after)]
    except SyntaxError:
        return "invalid", ""
    if ast.dump(trees[0]) == ast.dump(trees[1]):
        return "text", ""
    shapes = copy.deepcopy(trees)
    for tree in shapes:
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                node.value = "<text>"
    if ast.dump(shapes[0]) == ast.dump(shapes[1]):
        return "text", ""
    # Unparse excludes comments; changed lines exclude unrelated existing code.
    return "code", _changed(ast.unparse(shapes[0]), ast.unparse(shapes[1]))


def _changed(before, after):
    return "\n".join(
        line[1:]
        for line in difflib.unified_diff(before.splitlines(), after.splitlines(), n=0)
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def _script_code(path, text):
    # Keep quoted strings intact while excluding comments from native hook and
    # JS worker scripts. This is a comment filter, not a reachability analysis.
    quoted = r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`"""
    comments = (
        r"\#[^\n]*" if path.endswith((".sh", ".bash")) else r"//[^\n]*|/\*[\s\S]*?\*/"
    )
    tokens = re.findall(f"{quoted}|{comments}|[^\\s]", text)
    return " ".join(
        t
        for t in tokens
        if not (t.startswith("#") or t.startswith("//") or t.startswith("/*"))
    )


def _structured(path, text):
    if not text.strip():
        return {}
    if path.endswith(".json"):
        return json.loads(text)
    if path.endswith(".toml"):
        return tomllib.loads(text)
    return yaml.safe_load(text)


def _leaves(value, prefix=()):
    if isinstance(value, dict) and value:
        return {
            k: v
            for key, item in value.items()
            for k, v in _leaves(item, (*prefix, str(key))).items()
        }
    if isinstance(value, list) and value:
        return {
            k: v
            for i, item in enumerate(value)
            for k, v in _leaves(item, (*prefix, str(i))).items()
        }
    return {prefix: value}


def _config_tags(path, before, after, profile):
    try:
        old, new = (_leaves(_structured(path, text)) for text in (before, after))
    except (ValueError, yaml.YAMLError):
        return {"config"}, "invalid structured data; critic/smoke must validate"
    changed = [
        key
        for key in old.keys() | new.keys()
        if (key in old, old.get(key)) != (key in new, new.get(key))
    ]
    if not changed:
        return {"prompt"}, "formatting/comments only"
    if all(key and key[-1] in {"description", "help", "title"} for key in changed):
        return {"prompt"}, "description text only"
    keys = " ".join(" ".join(key) for key in changed)
    words = re.sub(r"[_\-]", " ", keys).lower()
    tags = {"config"}
    for tag, pattern in SIGNALS.items():
        if re.search(pattern, words):
            tags.add(tag)
    if profile == "claude_code":
        if re.search(r"PreCompact|PostCompact", keys):
            tags.add("context_mgmt")
        if re.search(r"hooks|Stop|PreToolUse|PostToolUse|SessionStart", keys):
            tags.add("control_flow")
    if path in {".mcp.json", ".lsp.json"}:
        tags.add("client_tool")
    if "sub_agents" in keys or "agents" in keys.split():
        tags.add("subagent")
    return tags, "changed keys: " + ", ".join(".".join(k) for k in sorted(changed))[
        :600
    ]


def _native_role(profile, path, component):
    if profile == "miniagent":
        if path.startswith("sub_agents/"):
            return "subagent"
        if path.startswith("middleware/"):
            return "control_flow"
    if profile == "codex" and path.startswith("python_core/"):
        if path.startswith("python_core/assets/compact/"):
            return "prompt"
        for prefix, tag in (
            ("python_core/memory_assets/", "memory"),
            ("python_core/context_assets/", "context_mgmt"),
        ):
            if path.startswith(prefix):
                return tag
        if role := RUNTIME_ROLES.get(PurePosixPath(path).stem):
            return role
    if profile in {
        "miniagent",
        "pi",
        "hermes",
        "openclaw",
        "opencode",
    } and path.startswith("runtime/"):
        if role := RUNTIME_ROLES.get(PurePosixPath(path).stem):
            return role
    return {
        "prompt": "prompt",
        "skill": "skill",
        "tool": "client_tool",
        "agent": "subagent",
        "memory": "memory",
        "hook": "control_flow",
        "config": "config",
    }.get(component)


def classify_edit(before, after, edit, custom):
    """Classify one reviewed mechanism using only its actual changed files."""
    declared = edit["component"]
    profile = profile_name(before)
    evidence = {}
    custom_evidence = {}
    for path in edit["paths"]:
        first, last = before.files.get(path), after.files.get(path)
        old, new = _text(first), _text(last)
        component = (last or first).component
        native = _native_role(profile, path, component)
        if old == new:
            if first and last and first.executable != last.executable and native:
                evidence.setdefault(native, []).append(
                    f"{path}: executable mode changed"
                )
            continue
        changed = _changed(old, new)
        tags = set()
        reason = "native resource"
        if path.endswith(".py"):
            kind, changed_code = _python_change(old, new)
            if kind == "text":
                tags, reason = {"prompt"}, "comments/docstrings/string text only"
            elif kind == "code":
                changed = changed_code
                if native:
                    tags.add(native)
                words = re.sub(r"[_\-]", " ", changed_code)
                for tag, pattern in SIGNALS.items():
                    if re.search(pattern, words, re.I):
                        tags.add(tag)
                # Actual control statements are evidence even with novel names.
                if re.search(
                    r"\b(?:if|elif|for|while|try|except|raise|return|break|continue|await)\b",
                    changed_code,
                ):
                    tags.add("control_flow")
                reason = "executable Python change"
            else:
                reason = "invalid Python; syntax gate must validate"
        elif path.endswith((".json", ".yaml", ".yml", ".toml")):
            tags, reason = _config_tags(path, old, new, profile)
            if tags != {"prompt"} and native and native != "config":
                tags.add(native)
        elif path.endswith((".js", ".ts", ".mjs", ".cjs", ".sh", ".bash")):
            old_code, new_code = (_script_code(path, text) for text in (old, new))
            if old_code == new_code:
                tags, reason = {"prompt"}, "script comments/formatting only"
            else:
                changed = _changed(old_code, new_code)
                tags = {native} if native else set()
                reason = "native executable script change"
                if component == "hook":
                    if re.search(
                        r"hookSpecificOutput|additionalContext|JSON\.stringify|json\.dumps",
                        changed,
                    ):
                        tags.add("output_plumbing")
                    if re.search(r"PreCompact|compact|messages|history", changed, re.I):
                        tags.add("context_mgmt")
        elif native:
            tags = {native}
        # Custom regexes extend a profile but cannot turn a Python comment-only
        # change into a new memory/tool. File patterns remain available explicitly.
        if tags != {"prompt"}:
            material = f"FILE {path}\n{changed}"
            for tag, patterns in custom.items():
                if any(re.search(pattern, material) for pattern in patterns):
                    custom_evidence.setdefault(tag, []).append(
                        f"{path}: configured signal"
                    )
        for tag in sorted(tags):
            evidence.setdefault(tag, []).append(f"{path}: {reason}")
    available = set(available_components(before, custom))
    evidence = {tag: rows for tag, rows in evidence.items() if tag in available}
    evidence.update(custom_evidence)
    # Retain an honest declaration; otherwise choose a single unambiguous tag.
    # Multiple mechanisms belong in separate edits[] declarations.
    if declared in evidence:
        tag = declared
    elif len(custom_evidence) == 1:
        tag = next(iter(custom_evidence))
    elif len(evidence) == 1:
        tag = next(iter(evidence))
    else:
        tag = UNKNOWN
    return {
        "version": VERSION,
        "profile": profile,
        "declared": declared,
        "component": tag,
        "evidence": evidence,
        "reason": "supported by changed files"
        if tag != UNKNOWN
        else "no unique supported classification; declare a supported mechanism or add component_signals",
    }
