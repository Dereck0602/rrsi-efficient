"""One execution entry point, with automatic file-version activation."""

from __future__ import annotations

import copy
import inspect
import sys
from collections.abc import Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from flashrsi._records import _config_record
from flashrsi._worker_prompt import instructions
from flashrsi.config import Config, _ConfigLoader, _validate_role_config
from flashrsi.core.benchmark import Task
from flashrsi.core.snapshot import (
    Snapshot,
    _copy_rules,
    _copy_scope,
    _matching_components,
)
from flashrsi.core.trajectory import Trajectory


@dataclass(frozen=True)
class _Execution:
    snapshot: Snapshot | None
    files_dir: Path | None
    config: Config


class Agent:
    """Place components.yaml beside the adapter and implement _run.

    Public run prepares one version's files and records the result. The backend
    uses files_dir for its own code/configuration, working_dir for the task being
    performed, and config for runtime options (including credentials).
    Algorithms set editing scope once, evaluate candidates through rollout, and
    select accepted versions with update. The backend need not manage versions.
    """

    name: str
    description: str
    # Built-ins reuse their package extra. External adapters may name their own
    # distributions; installation pins the host's versions in both cases.
    _container_extra: str | None = None
    _container_dependencies: tuple[str, ...] | None = None
    # The file this harness treats as its main instructions, for algorithms that
    # rewrite one whole instruction file. A component can hold several files and
    # need not ship any of them, so only the adapter knows which one this is.
    instruction_file: str | None = None

    def __init__(self, config: Mapping[str, Any] | None = None) -> None:
        if not isinstance(getattr(self, "name", None), str) or not self.name:
            raise ValueError("Agent subclasses must declare a non-empty name")
        if config is not None and not isinstance(config, Mapping):
            raise ValueError("Agent config must be a mapping")
        if config is not None and "agent" in config:
            raise ValueError(
                "Pass the Agent's own configuration: config.agent or "
                "config.algorithm.worker, not the whole experiment configuration"
            )
        self._config = Config(copy.deepcopy(config))
        _validate_role_config(self._config, "config")
        self._snapshot = _capture_agent(type(self), self._config)
        # An integration binds execution independently of the harness type.
        self._env_runner = None
        # Set by whoever runs this Agent, to record what ask() did and to
        # redact credentials it cannot see itself. See Agent.ask.
        self._observer = None
        # Local to the async call: update changes the default for future calls,
        # never the files or version visible inside a running backend.
        self._execution: ContextVar[_Execution | None] = ContextVar(
            "agent_execution", default=None
        )

    @property
    def config(self) -> Config:
        """Defaults outside execution; a detached configuration inside a call.

        Edit these defaults before the next run or rollout. Backends should only
        read the call-local copy; edits elsewhere cannot affect an active batch.
        """
        execution = self._execution.get()
        return execution.config if execution is not None else self._config

    @property
    def snapshot(self) -> Snapshot:
        execution = self._execution.get()
        snapshot = execution.snapshot if execution is not None else self._snapshot
        if snapshot is None:
            raise RuntimeError("this Agent has no versioned files; add components.yaml")
        return snapshot

    @property
    def component_rules(self) -> Mapping[str, tuple[str, ...]]:
        """The selected version's rules, loaded from components.yaml once at creation."""
        execution = self._execution.get()
        snapshot = execution.snapshot if execution is not None else self._snapshot
        return (
            snapshot.component_rules if snapshot is not None else MappingProxyType({})
        )

    @property
    def files_dir(self) -> Path:
        """This call's code/configuration directory, available inside _run only."""
        execution = self._execution.get()
        if execution is None or execution.files_dir is None:
            raise RuntimeError("files_dir is available only inside a run with files")
        return execution.files_dir

    def set_scope(self, scope: Mapping[str, str]) -> None:
        """Set editing defaults: rw, r, or none; omitted components are hidden.

        Full harness execution is unaffected. Previously obtained Snapshots and
        active batches keep their editing view; future calls use the new scope.
        """
        if self._snapshot is None:
            raise RuntimeError("this Agent has no versioned files; add components.yaml")
        scope = _copy_scope(scope, self.component_rules)
        self._snapshot = self._snapshot._with_scope(scope)

    def update(self, snapshot: Snapshot) -> None:
        """Select a version for future calls after checking compatibility and scope."""
        self._validate_snapshot(snapshot)
        self._snapshot = snapshot._with_scope(self._snapshot._scope)

    async def run(self, prompt: str, working_dir: Path | None = None) -> Trajectory:
        """Run a prompt for text, task execution, or editing; always return a trace."""
        if type(self)._run is Agent._run:
            raise NotImplementedError(f"{self.name} does not implement _run")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if working_dir is not None:
            working_dir = Path(working_dir).expanduser().resolve()
            if not working_dir.is_dir():
                raise ValueError("working_dir must be an existing directory")
        execution = self._execution.get()
        selected = execution.snapshot if execution is not None else self._snapshot
        if selected is None:
            return await self._execute(prompt, working_dir, None, None)
        with selected._export(full=True) as files:
            return await self._execute(prompt, working_dir, selected, files)

    async def ask(self, prompt: str) -> str:
        """Answer a prompt as text, raising if this Agent did not finish.

        A caller that wants the text wants the call to have worked, so a failed
        run is an exception here rather than a Trajectory to inspect.
        """
        return await self._answer(prompt, None)

    async def edit(self, working_dir: Path, prompt: str) -> str:
        """Edit that directory, and say what was done.

        The edit is the directory's new contents; the return value is this
        Agent's own account of it, which a caller may hold the edit to. Nothing
        is committed here -- versioning the result belongs to the caller.
        """
        return await self._answer(prompt, working_dir)

    async def _answer(self, prompt: str, working_dir: Path | None) -> str:
        """The half ask and edit share: run, report failure, return the text.

        Whoever created this Agent may attach an observer, which is how the
        trajectory reaches an experiment's records and how credentials are
        redacted from the error -- a single Agent holds only its own.
        """
        # Worker is an operation, not a permanent role on a reusable Agent.
        # Keep its instructions in this call's config, outside Snapshot files.
        config = copy.deepcopy(self.config)
        config["_worker_system_prompt"] = instructions(editing=working_dir is not None)
        active = self._execution.get()
        token = self._execution.set(
            _Execution(
                active.snapshot if active is not None else self._snapshot,
                active.files_dir if active is not None else None,
                config,
            )
        )
        try:
            result = await self.run(prompt, working_dir)
        finally:
            self._execution.reset(token)
        detail = result.error_message
        if self._observer is not None:
            detail = self._observer(prompt, result)
        if result.status != "finished":
            raise RuntimeError(f"{self.name} failed: {detail}")
        return result.output

    async def _run(self, prompt: str, working_dir: Path | None = None) -> Trajectory:
        """Backend hook. Load versioned files from files_dir, not the source package."""
        raise NotImplementedError(f"{self.name} does not implement _run")

    def _check_container(self) -> None:
        """Adapters may add checks for configuration that cannot leave the host."""
        if self._container_extra is None and self._container_dependencies is None:
            raise ValueError(f"{self.name} has not declared container dependencies")

    async def rollout(
        self, tasks: Iterable[Task], *, snapshot: Snapshot | None = None
    ) -> tuple[Trajectory, ...]:
        """Execute tasks using one pinned version and configuration and attach Tasks.

        Omit snapshot to use the current version. Supplying a candidate evaluates
        it without selecting it; only update changes the default version.
        The caller binds an integration for environment tasks. It receives this
        Agent with the pinned snapshot/config; ordinary questions use run.
        """
        if type(self)._run is Agent._run:
            raise NotImplementedError(f"{self.name} does not implement _run")
        tasks = tuple(tasks)
        if any(not isinstance(task, Task) for task in tasks):
            raise ValueError("rollout tasks must be Task instances")
        env_runner = self._env_runner
        if env_runner is None and any(task.env is not None for task in tasks):
            raise NotImplementedError(
                "tasks with env require an environment-aware rollout; "
                "bind an execution integration before rollout"
            )
        execution = self._execution.get()
        baseline = execution.snapshot if execution is not None else self._snapshot
        selected = baseline if snapshot is None else snapshot
        if selected is not None:
            self._validate_snapshot(selected, baseline=baseline)
            selected = selected._with_scope(baseline._scope)
        token = self._execution.set(
            _Execution(selected, None, copy.deepcopy(self.config))
        )
        try:
            trajectories = []
            for task in tasks:
                if task.env is None:
                    result = await self.run(task.question)
                else:
                    result = await env_runner(self, task)
                    if not isinstance(result, Trajectory):
                        raise TypeError(
                            "environment execution must return a Trajectory"
                        )
                trajectories.append(replace(result, task=task))
            return tuple(trajectories)
        finally:
            self._execution.reset(token)

    async def _execute(
        self,
        prompt: str,
        working_dir: Path | None,
        snapshot: Snapshot | None,
        files_dir: Path | None,
    ) -> Trajectory:
        config = copy.deepcopy(self.config)
        _validate_role_config(config, "config")
        record = {
            "type": "RunConfig",
            "config": _config_record(config),
            "snapshot_id": snapshot.id if snapshot is not None else None,
        }
        token = self._execution.set(_Execution(snapshot, files_dir, config))
        try:
            result = await self._run(prompt, working_dir)
            if not isinstance(result, Trajectory):
                raise TypeError("_run must return a Trajectory")
            return replace(result, messages=(record, *result.messages))
        finally:
            self._execution.reset(token)

    def _validate_snapshot(
        self, snapshot: Snapshot, *, baseline: Snapshot | None = None
    ) -> None:
        if not isinstance(snapshot, Snapshot):
            raise ValueError("expected a Snapshot")
        if self._snapshot is None:
            raise RuntimeError("this Agent has no versioned files; add components.yaml")
        baseline = self._snapshot if baseline is None else baseline
        baseline._validate_candidate(snapshot)


def _capture_agent(
    agent_type: type[Agent], config: Mapping[str, Any]
) -> Snapshot | None:
    """Resolve the concrete adapter's directory; an explicit root never falls back."""
    module = sys.modules.get(agent_type.__module__)
    module_file = getattr(module, "__file__", None)
    adapter = (
        Path(module_file).resolve()
        if module_file is not None and Path(module_file).is_file()
        else None
    )
    explicit = "source_dir" in config
    source = config["source_dir"] if explicit else adapter.parent if adapter else None
    if source is None and not explicit:
        return None  # Interactive or dynamically defined stateless Worker.
    if not isinstance(source, (str, Path)) or not str(source).strip():
        raise ValueError("source_dir must be a non-empty path")
    source = Path(source).expanduser()
    if source.is_symlink() or not source.is_dir():
        raise ValueError(
            "source_dir must be an existing directory, not a symbolic link"
        )
    source = source.resolve()
    manifest = source / "components.yaml"
    if not manifest.exists() and not manifest.is_symlink():
        if explicit:
            raise ValueError("source_dir must contain components.yaml")
        return None
    rules = _read_component_rules(manifest)
    # An adapter can import a harness, but it must never become editable harness
    # code itself. Check the concrete module and inherited execution hooks.
    adapter_files = {adapter} if adapter is not None else set()
    for cls in agent_type.__mro__:
        hook = cls.__dict__.get("_run")
        if hook is not None and inspect.isfunction(hook):
            adapter_files.add(Path(hook.__code__.co_filename).resolve())
    for path in adapter_files:
        if path.is_relative_to(source) and _matching_components(
            path.relative_to(source).as_posix(), rules
        ):
            raise ValueError(
                "components.yaml must exclude Agent adapter code; "
                "select only harness runtime files"
            )
    return Snapshot._capture(source, agent_type=agent_type.name, component_rules=rules)


def _read_component_rules(path: Path) -> Mapping[str, tuple[str, ...]]:
    """Read the small manifest using the same strict YAML loader as config."""
    if path.is_symlink() or not path.is_file():
        raise ValueError("components.yaml must be a regular file, not a symbolic link")
    try:
        with path.open(encoding="utf-8") as stream:
            raw = yaml.load(stream, Loader=_ConfigLoader)
        if not isinstance(raw, Mapping) or any(
            not isinstance(patterns, list) for patterns in raw.values()
        ):
            raise ValueError("expected a component-to-pattern-list mapping")
        return _copy_rules({name: tuple(patterns) for name, patterns in raw.items()})
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as error:
        raise ValueError(
            "invalid components.yaml; expected unique component names mapped "
            "to non-empty lists of relative file patterns"
        ) from error
