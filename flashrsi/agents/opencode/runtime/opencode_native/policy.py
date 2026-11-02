"""OpenCode tool policy and workspace authority checks."""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

PROFILES = {
    "minimal": {
        "read",
        "glob",
        "grep",
        "diagnostics",
        "memory_search",
        "read_skill",
        "skill",
    },
    "coding": {
        "read",
        "write",
        "edit",
        "patch",
        "apply_patch",
        "bash",
        "exec",
        "process",
        "glob",
        "grep",
        "fetch",
        "webfetch",
        "diagnostics",
        "task",
        "todowrite",
        "skill",
        "memory_search",
        "read_skill",
    },
    "full": {
        "read",
        "write",
        "edit",
        "patch",
        "apply_patch",
        "bash",
        "exec",
        "memory_search",
        "read_skill",
        "process",
        "glob",
        "grep",
        "fetch",
        "webfetch",
        "diagnostics",
        "task",
        "todowrite",
        "skill",
    },
}


class ToolPolicy:
    """Resolve tool allow/deny and path policy once per agent turn.

    ``tools.allow`` is an explicit list when present, including an empty list;
    ``tools.profile`` supplies defaults only when allow is omitted.  A sandbox
    can be enabled with ``sandbox.enabled`` or the legacy ``tools.workspace_only``
    switch.  Sandbox paths are resolved before checking containment, so a
    symlink cannot escape the workspace.
    """

    def __init__(self, config: dict[str, Any], workspace: Path):
        self.config = config if isinstance(config, dict) else {}
        raw = self.config.get("tool_policy")
        if not isinstance(raw, dict):
            raw = self.config.get("tools")
        tools = raw if isinstance(raw, dict) else {}
        self.workspace = Path(workspace).expanduser().resolve()
        configured_profile = tools.get("profile", self.config.get("tool_profile"))
        if configured_profile is None:
            permissions = self.config.get("permission", self.config.get("permissions"))
            if isinstance(permissions, dict):
                configured_profile = permissions.get("profile")
        self.profile = str(configured_profile or "coding")
        profile = PROFILES.get(self.profile, PROFILES["coding"])
        configured_allow = tools.get("allow", None)
        allow_was_configured = configured_allow is not None
        if configured_allow is None:
            configured_allow = profile
        if isinstance(configured_allow, str):
            configured_allow = [configured_allow]
        self.allow = {str(name) for name in (configured_allow or ())}
        # OpenCode configuration names permissions by capability while older
        # FlashRSI roles use tools.allow. Merge explicit permission grants so a
        # snapshot can use either spelling without widening a deny rule.
        permissions = self.config.get("permission", self.config.get("permissions"))
        if (
            isinstance(permissions, dict)
            and "allow" in permissions
            and not allow_was_configured
        ):
            values = permissions.get("allow")
            if isinstance(values, str):
                values = [values]
            self.allow = {str(v) for v in (values or ())}
        if isinstance(permissions, dict):
            for name, decision in permissions.items():
                if str(decision).lower() in {"allow", "enabled", "true", "yes"}:
                    self.allow.add(str(name))
        self.deny = set()
        configured_deny = tools.get("deny", ())
        if isinstance(configured_deny, str):
            configured_deny = [configured_deny]
        self.deny = {str(name) for name in (configured_deny or ())}
        if isinstance(permissions, dict) and isinstance(
            permissions.get("deny"), (list, tuple, set, str)
        ):
            values = permissions.get("deny")
            self.deny.update(
                [values] if isinstance(values, str) else str(v) for v in values
            )
        if isinstance(permissions, dict):
            for name, decision in permissions.items():
                if str(decision).lower() in {"deny", "ask", "disabled", "false", "no"}:
                    self.deny.add(str(name))
        self.rules: list[tuple[str, str, str]] = []
        if isinstance(permissions, dict):
            for capability, decision in permissions.items():
                if capability in {"profile", "allow", "deny"}:
                    continue
                if isinstance(decision, dict):
                    for pattern, action in decision.items():
                        self.rules.append(
                            (str(capability), str(pattern), str(action).lower())
                        )
                elif isinstance(decision, str):
                    self.rules.append((str(capability), "*", decision.lower()))
        sandbox = self.config.get("sandbox")
        sandbox = sandbox if isinstance(sandbox, dict) else {}
        # OpenCode's sandbox mode defaults to ``off``.  A workspace boundary
        # is therefore opt-in through ``sandbox.enabled`` (or the legacy
        # ``tools.workspace_only`` alias), matching the upstream authority
        # model while preserving an explicit safe mode for benchmarks.
        if "enabled" in sandbox:
            self.sandbox = bool(sandbox["enabled"])
        elif "mode" in sandbox:
            # OpenCode exposes the authority boundary as a mode. Any active
            # mode is workspace constrained in this embedded Target; ``off``
            # retains the upstream unrestricted default.
            self.sandbox = str(sandbox["mode"]).lower() not in {
                "off",
                "disabled",
                "none",
            }
        else:
            self.sandbox = bool(tools.get("workspace_only", False))
        self.allow_outside_workspace = not self.sandbox

    def evaluate(self, name: str, pattern: str = "*") -> str:
        effective = (
            "edit" if name in {"write", "edit", "apply_patch", "patch"} else name
        )
        action = (
            "allow"
            if ("*" in self.allow or name in self.allow or effective in self.allow)
            else "deny"
        )
        if name in self.deny or effective in self.deny or "*" in self.deny:
            action = "deny"
        for rule_capability, rule_pattern, rule_action in self.rules:
            if fnmatch.fnmatchcase(effective, rule_capability) and fnmatch.fnmatchcase(
                str(pattern), rule_pattern
            ):
                action = rule_action
        return action

    def permitted(self, name: str, pattern: str = "*") -> bool:
        return self.evaluate(name, pattern) == "allow"

    def path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if (
            self.sandbox
            and path != self.workspace
            and self.workspace not in path.parents
        ):
            raise PermissionError(f"path escapes OpenCode workspace: {value}")
        return path

    def check(self, name: str, pattern: str = "*") -> None:
        action = self.evaluate(name, pattern)
        if action != "allow":
            raise PermissionError(
                f"tool {action} by OpenCode permission policy: {name} ({pattern})"
            )
