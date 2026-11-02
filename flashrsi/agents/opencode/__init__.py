"""OpenCode Target/Worker adapter backed by a pure-Python Snapshot runtime.

The adapter deliberately loads the selected Snapshot's ``opencode_native``
package.  It never starts Bun, Node, the OpenCode CLI, a server, or a TUI.
"""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import inspect
import math
import sys
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any, Iterator

from flashrsi._records import _redact, _secrets
from flashrsi.core import Agent, Trajectory

_RUN_CONTROLS: ContextVar[dict[str, Any] | None] = ContextVar(
    "opencode_run_controls", default=None
)


class OpenCode(Agent):
    """Execute the embedded OpenCode turn path from the selected Snapshot."""

    name = "opencode"
    description = "OpenCode-compatible coding agent with a versioned Python Snapshot."
    instruction_file = "AGENTS.md"
    _container_dependencies = ("httpx", "PyYAML")

    async def run_with_controls(
        self,
        prompt: str,
        working_dir: Path | None = None,
        *,
        on_event=None,
        cancel_event: asyncio.Event | None = None,
    ) -> Trajectory:
        """Run a turn with an event observer and cooperative cancellation."""
        controls = {"on_event": on_event}
        if cancel_event is not None:
            controls["cancel_event"] = cancel_event
        token = _RUN_CONTROLS.set(controls)
        try:
            return await self.run(prompt, working_dir)
        finally:
            _RUN_CONTROLS.reset(token)

    async def _run(self, prompt: str, working_dir: Path | None = None) -> Trajectory:
        secrets = sorted(set(_secrets(self.config)), key=len, reverse=True)
        try:
            _validate(self.config, self.files_dir)
            config = _plain(self.config)
            with TemporaryDirectory(prefix="flashrsi-opencode-") as temporary:
                task_dir = working_dir or Path(temporary) / "work"
                task_dir.mkdir(parents=True, exist_ok=True)
                with _native_runtime(self.files_dir) as native:
                    state: dict[str, Any] = {}
                    controls = dict(_RUN_CONTROLS.get() or {})
                    options = {"_state": state, **controls}
                    accepted = inspect.signature(native.run).parameters
                    options = {
                        key: value for key, value in options.items() if key in accepted
                    }
                    result = await asyncio.wait_for(
                        native.run(config, prompt, task_dir, self.files_dir, **options),
                        timeout=float(config.get("timeout", 300)),
                    )
            if not isinstance(result, Mapping):
                result = vars(result)
            error = result.get("error_message") or result.get("error")
            failed = result.get("status", "finished") != "finished" or bool(error)
            return Trajectory(
                status="error" if failed else "finished",
                output=_redact(str(result.get("output", "") or ""), secrets),
                messages=_redact(list(result.get("events", [])), secrets),
                usage=_redact(dict(result.get("usage", {})), secrets),
                error_message=(
                    _redact(str(error or "OpenCode runtime failed"), secrets)
                    if failed
                    else None
                ),
            )
        except asyncio.TimeoutError:
            return Trajectory(
                status="error",
                error_message=(
                    f"OpenCode timed out after {self.config.get('timeout', 300):g}s"
                ),
            )
        except Exception as exc:
            return Trajectory(status="error", error_message=_redact(str(exc), secrets))


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return copy.deepcopy(value)


def _runtime(root: Path) -> Path:
    runtime = root / "runtime" / "opencode_native"
    if not (runtime / "__init__.py").is_file():
        raise ValueError(
            "OpenCode Snapshot is missing runtime/opencode_native/__init__.py"
        )
    return runtime.resolve()


def _validate(config: Mapping[str, Any], files_dir: Path | None = None) -> None:
    """Validate only the embedded Target contract.

    OpenCode's Bun/Node executable, CLI and server settings intentionally do
    not belong to this Snapshot integration.  A role supplies a model API
    connection and the embedded runtime selects its wire adapter.
    """

    for key in ("cli_path", "node_path", "bun_path", "server_path", "tui_path"):
        if config.get(key):
            raise ValueError(
                f"OpenCode executes the Snapshot's Python runtime; {key} is unsupported"
            )
    llm = config.get("llm", {})
    if not isinstance(llm, Mapping):
        raise ValueError("OpenCode llm must be a mapping")
    model = llm.get("model_name")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("OpenCode llm.model_name must be a non-empty string")
    key = llm.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("OpenCode llm.api_key is required for this role")
    mode = (
        str(
            llm.get("model_api")
            or llm.get("api_mode")
            or config.get("api_mode", "chat_completions")
        )
        .lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    if mode not in {
        "chat_completions",
        "openai_completions",
        "responses",
        "openai_responses",
        "anthropic_messages",
        "anthropic",
    }:
        raise ValueError(
            "OpenCode supports Chat Completions, Responses and Anthropic Messages APIs"
        )
    timeout = config.get("timeout", 300)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("OpenCode timeout must be a finite positive number")
    if "max_iterations" in config and "max_turns" in config:
        raise ValueError("Set only one of OpenCode max_iterations and max_turns")
    limit = config.get("max_iterations", config.get("max_turns", 32))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("OpenCode max_iterations must be a positive integer")
    policy = config.get(
        "permission",
        config.get("permissions", config.get("tool_policy", config.get("tools", {}))),
    )
    if policy is not None and not isinstance(policy, Mapping):
        raise ValueError("OpenCode permission must be a mapping")
    root = files_dir
    if root is None:
        root = Path(config.get("source_dir", Path(__file__).parent)).expanduser()
    _runtime(root)


@contextmanager
def _native_runtime(files_dir: Path) -> Iterator[ModuleType]:
    root = _runtime(files_dir)
    name = "flashrsi_opencode_native_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise ValueError("OpenCode Snapshot runtime could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for imported in list(sys.modules):
            if imported == name or imported.startswith(name + "."):
                sys.modules.pop(imported, None)


__all__ = ["OpenCode"]
