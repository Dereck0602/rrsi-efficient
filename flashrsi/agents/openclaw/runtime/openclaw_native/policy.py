"""OpenClaw tool policy and workspace authority checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

PROFILES = {
    "minimal": {"read", "memory_search", "read_skill"},
    "coding": {
        "read",
        "write",
        "edit",
        "apply_patch",
        "exec",
        "process",
        "memory_search",
        "read_skill",
    },
    "full": {
        "read",
        "write",
        "edit",
        "apply_patch",
        "exec",
        "memory_search",
        "read_skill",
        "process",
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
        self.profile = str(tools.get("profile", self.config.get("tool_profile", "coding")))
        profile = PROFILES.get(self.profile, PROFILES["coding"])
        configured_allow = tools.get("allow", None)
        if configured_allow is None:
            configured_allow = profile
        if isinstance(configured_allow, str):
            configured_allow = [configured_allow]
        self.allow = {str(name) for name in (configured_allow or ())}
        self.deny = set()
        configured_deny = tools.get("deny", ())
        if isinstance(configured_deny, str):
            configured_deny = [configured_deny]
        self.deny = {str(name) for name in (configured_deny or ())}
        sandbox = self.config.get("sandbox")
        sandbox = sandbox if isinstance(sandbox, dict) else {}
        # OpenClaw's sandbox mode defaults to ``off``.  A workspace boundary
        # is therefore opt-in through ``sandbox.enabled`` (or the legacy
        # ``tools.workspace_only`` alias), matching the upstream authority
        # model while preserving an explicit safe mode for benchmarks.
        if "enabled" in sandbox:
            self.sandbox = bool(sandbox["enabled"])
        elif "mode" in sandbox:
            # OpenClaw exposes the authority boundary as a mode. Any active
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

    def permitted(self, name: str) -> bool:
        return ("*" in self.allow or name in self.allow) and name not in self.deny

    def path(self, value: str | Path) -> Path:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace / path
        path = path.resolve()
        if self.sandbox and path != self.workspace and self.workspace not in path.parents:
            raise PermissionError(f"path escapes OpenClaw workspace: {value}")
        return path

    def check(self, name: str) -> None:
        if not self.permitted(name):
            raise PermissionError(f"tool denied by policy: {name}")
