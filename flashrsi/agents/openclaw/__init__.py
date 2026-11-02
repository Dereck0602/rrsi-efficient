"""OpenClaw Target/Worker adapter backed by a pure-Python Snapshot runtime."""

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
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType
from typing import Any, Iterator

from flashrsi._records import _redact, _secrets
from flashrsi.core import Agent, Trajectory


class OpenClaw(Agent):
    """Run the selected OpenClaw embedded-core Snapshot.

    The adapter intentionally owns no Gateway, channel, or host CLI process.
    Snapshot code supplies the model loop, tool policy, sessions, skills, and
    memory semantics so an evolved Snapshot is the code that actually runs.
    """

    name = "openclaw"
    description = (
        "OpenClaw embedded agent runtime with a versioned Python Snapshot."
    )
    instruction_file = "SOUL.md"
    _container_dependencies = ("httpx",)

    async def _run(self, prompt: str, working_dir: Path | None = None) -> Trajectory:
        secrets = sorted(set(_secrets(self.config)), key=len, reverse=True)
        try:
            _validate(self.config, self.files_dir)
            config = _plain(self.config)
            with TemporaryDirectory(prefix="flashrsi-openclaw-") as temporary:
                task_dir = working_dir or Path(temporary) / "work"
                task_dir.mkdir(parents=True, exist_ok=True)
                with _native_runtime(self.files_dir) as native:
                    state: dict[str, Any] = {}
                    parameters = inspect.signature(native.run).parameters
                    options: dict[str, Any] = {}
                    if "_state" in parameters:
                        options["_state"] = state
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
                    _redact(str(error or "OpenClaw runtime failed"), secrets)
                    if failed
                    else None
                ),
            )
        except asyncio.TimeoutError:
            return Trajectory(
                status="error",
                error_message=(
                    f"OpenClaw timed out after {self.config.get('timeout', 300):g}s"
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
    runtime = root / "runtime" / "openclaw_native"
    if not (runtime / "__init__.py").is_file():
        raise ValueError(
            "OpenClaw Snapshot is missing runtime/openclaw_native/__init__.py"
        )
    return runtime.resolve()


def _validate(config: Mapping[str, Any], files_dir: Path | None = None) -> None:
    if config.get("cli_path") or config.get("node_path") or config.get("gateway_path"):
        raise ValueError(
            "OpenClaw executes the Snapshot's Python runtime; cli_path/node_path/gateway_path are unsupported"
        )
    llm = config.get("llm", {})
    if not isinstance(llm, Mapping):
        raise ValueError("OpenClaw llm must be a mapping")
    model = llm.get("model_name")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("OpenClaw llm.model_name must be a non-empty string")
    key = llm.get("api_key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("OpenClaw llm.api_key is required for this role")
    mode = str(
        llm.get("model_api")
        or llm.get("api_mode")
        or config.get("api_mode", "chat_completions")
    ).lower().replace("-", "_").replace(" ", "_")
    if mode not in {
        "chat_completions",
        "openai_completions",
        "responses",
        "openai_responses",
        "anthropic_messages",
    }:
        raise ValueError(
            "OpenClaw supports Chat Completions, Responses and Anthropic Messages APIs"
        )
    timeout = config.get("timeout", 300)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("OpenClaw timeout must be a finite positive number")
    if "max_iterations" in config and "max_turns" in config:
        raise ValueError("Set only one of OpenClaw max_iterations and max_turns")
    limit = config.get("max_iterations", config.get("max_turns", 32))
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("OpenClaw max_iterations must be a positive integer")
    policy = config.get("tool_policy", {})
    if policy is not None and not isinstance(policy, Mapping):
        raise ValueError("OpenClaw tool_policy must be a mapping")
    root = files_dir
    if root is None:
        root = Path(config.get("source_dir", Path(__file__).parent)).expanduser()
    _runtime(root)


@contextmanager
def _native_runtime(files_dir: Path) -> Iterator[ModuleType]:
    root = _runtime(files_dir)
    name = "flashrsi_openclaw_native_" + uuid.uuid4().hex
    spec = importlib.util.spec_from_file_location(
        name, root / "__init__.py", submodule_search_locations=[str(root)]
    )
    if spec is None or spec.loader is None:
        raise ValueError("OpenClaw Snapshot runtime could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        for imported in list(sys.modules):
            if imported == name or imported.startswith(name + "."):
                sys.modules.pop(imported, None)


__all__ = ["OpenClaw"]
