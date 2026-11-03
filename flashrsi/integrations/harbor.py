"""Run an existing FlashRSI adapter inside one native Harbor trial."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import shlex
import shutil
import sys
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory, mkdtemp

if sys.version_info < (3, 12):
    raise ImportError("Harbor execution requires Python 3.12 or newer")

try:
    from harbor.agents.base import BaseAgent
    from harbor.agents.installed.base import NonZeroAgentExitCodeError
    from harbor.models.task.task import Task as HarborTask
    from harbor.models.task.verifier_mode import (
        VerifierEnvironmentMode,
        resolve_task_verifier_mode,
    )
    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        TrialConfig,
        VerifierConfig,
    )
    from harbor.models.trial.paths import EnvironmentPaths
    from harbor.trial.hooks import TrialEvent
    from harbor.trial.trial import Trial
except ImportError as exc:
    raise ImportError("Install flashrsi-lite[harbor] for environment tasks") from exc

from flashrsi._records import _config_record, _redact, _secrets
from flashrsi.core import Trajectory
from flashrsi.integrations._container import _agent_class
from flashrsi.integrations._container_payload import _plain, _setup_script
from flashrsi.integrations._container_payload import _stage as _stage_payload

_ROOT = "/opt/flashrsi-lite"


def bind(agent):
    """Connect an Agent to Harbor; CLI runs do this in the runner.

    The adapter must be importable in the container. Built-ins travel with the
    current FlashRSI source; external adapters must include their distribution
    in _container_dependencies. No task or model is executed here.
    """
    agent._check_container()
    from flashrsi.integrations._terminal_integrity import validate

    validate(agent.config.get("harbor", {}).get("integrity_review"))
    agent.snapshot  # Container execution requires a versioned harness.
    cls = type(agent)
    if _agent_class(f"{cls.__module__}:{cls.__qualname__}") is not cls:
        raise ValueError("Container execution requires an importable Agent class")
    agent._env_runner = _run


async def _run(agent, task, *, verifier_env=None, agent_allowed_hosts=None):
    """The caller's rollout has already pinned its Snapshot and configuration."""
    if shutil.which("docker") is None:
        raise ValueError("Harbor execution requires Docker with the Compose plugin")
    agent._check_container()
    native = HarborTask(Path(task.env))
    if native.config.steps or native.config.environment.os.value != "linux":
        raise ValueError("This integration supports single-step Linux Harbor tasks")
    if native.instruction != task.question:
        raise ValueError("Harbor task instruction changed after benchmark.load()")
    checksum = native.checksum
    if task.metadata.get("task_checksum", checksum) != checksum:
        raise ValueError("Harbor task files changed after benchmark.load()")

    snapshot = agent.snapshot
    config = copy.deepcopy(agent.config)
    options = config.get("harbor", {})
    from flashrsi.integrations._terminal_integrity import validate

    validate(options.get("integrity_review"))
    remote_docker = options.get("remote_docker", False)
    if not isinstance(remote_docker, bool):
        raise ValueError("harbor.remote_docker must be a boolean")
    shared_verifier = (
        resolve_task_verifier_mode(native.config) == VerifierEnvironmentMode.SHARED
    )
    environment_import = _docker_environment_import(native, remote_docker)
    install_source = options.get("uv_install_source", "astral")
    installers = {
        "astral": "https://astral.sh/uv/0.11.4/install.sh",
        "github": "https://github.com/astral-sh/uv/releases/download/0.11.4/uv-installer.sh",
    }
    if not isinstance(install_source, str) or install_source not in installers:
        raise ValueError("harbor.uv_install_source must be astral or github")
    timeout = _positive(
        config.get("timeout", native.config.agent.timeout_sec or 300), "timeout"
    )
    setup_timeout = _positive(options.get("setup_timeout", 600), "harbor.setup_timeout")
    grace = _positive(options.get("cleanup_grace", 30), "harbor.cleanup_grace")
    config["timeout"] = timeout
    secrets = sorted(
        set(_secrets(config) + _secrets(os.environ) + _secrets(verifier_env)),
        key=len,
        reverse=True,
    )
    record = {
        "type": "RunConfig",
        "config": _config_record(config),
        "snapshot_id": snapshot.id,
    }
    trajectory = Trajectory(messages=(record,))
    event = {
        "type": "HarborVerifier",
        "verifier_environment_mode": "shared" if shared_verifier else "separate",
        "host_log_transport": "copy" if environment_import else "mount",
        "task_id": task.task_id,
        "task_checksum": checksum,
        "snapshot_id": snapshot.id,
    }

    with TemporaryDirectory(prefix="flashrsi-harbor-") as temporary:
        root = Path(temporary)
        payload = root / "payload"
        payload.mkdir(mode=0o700)
        _stage(payload, agent, config, task.question)
        trial = None
        try:
            trial = await Trial.create(
                TrialConfig(
                    task=TaskConfig(path=Path(task.env)),
                    trials_dir=(
                        Path(options["trials_dir"]).expanduser().resolve()
                        if options.get("trials_dir")
                        else root / "trials"
                    ),
                    agent=AgentConfig(
                        import_path="flashrsi.integrations.harbor:_SnapshotAgent",
                        model_name=config["llm"]["model_name"],
                        override_timeout_sec=timeout + grace,
                        override_setup_timeout_sec=setup_timeout,
                        extra_allowed_hosts=agent_allowed_hosts or [],
                        kwargs={
                            "payload_dir": str(payload),
                            "uv_installer_url": installers[install_source],
                        },
                    ),
                    environment=EnvironmentConfig(
                        type="docker",
                        delete=True,
                        env=_plain(options.get("environment_env", {})),
                        import_path=environment_import,
                    ),
                    verifier=VerifierConfig(env=verifier_env or {}),
                )
            )
            if shared_verifier:

                async def prepare_verifier(_event):
                    await _prepare_shared_verifier(trial.agent_environment)

                trial.add_hook(TrialEvent.VERIFICATION_START, prepare_verifier)
            # Harbor owns environment setup, phase deadlines, verifier and cleanup.
            result = await trial.run()
            output = trial.paths.agent_dir / "flashrsi.json"
            if output.is_file():
                values = json.loads(output.read_text())
                values.pop("task", None)
                trajectory = Trajectory(**values)
                if not any(
                    message.get("type") == "RunConfig"
                    and message.get("snapshot_id") == snapshot.id
                    for message in trajectory.messages
                ):
                    raise ValueError(
                        "Container result does not identify the requested Snapshot"
                    )
            elif result.exception_info is None:
                raise ValueError("Harbor agent ended without a FlashRSI trajectory")
            event["trial_id"] = str(result.id)
            event["rewards"] = (
                result.verifier_result.rewards
                if result.verifier_result is not None
                else None
            )
            event["stdout"] = _read_log(trial.paths.test_stdout_path)
            event["stderr"] = _read_log(trial.paths.test_stderr_path)
            if result.exception_info is not None:
                event["exception_type"] = result.exception_info.exception_type
                error = (
                    result.exception_info.exception_message
                    or result.exception_info.exception_type
                )
                event["error"] = error
                trajectory = replace(
                    trajectory,
                    status="error",
                    error_message=trajectory.error_message or error,
                )
            elif trajectory.status == "finished" and event["rewards"] is None:
                raise ValueError("Harbor verifier ended without rewards")
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            event["error"] = error
            trajectory = replace(trajectory, status="error", error_message=error)
        finally:
            if trial is not None:
                try:
                    if options.get("trials_dir"):
                        # Use Harbor's own collected outputs, outside the temporary payload
                        # and Snapshot. Bind retained bytes to the evaluated version/task.
                        directory = trial.paths.trial_dir.resolve()
                        event["trial_directory"] = str(directory)
                        event["files"] = {
                            str(file.relative_to(directory)): hashlib.sha256(
                                file.read_bytes()
                            ).hexdigest()
                            for folder in (
                                trial.paths.artifacts_dir,
                                trial.paths.verifier_dir,
                            )
                            for file in sorted(folder.rglob("*"))
                            if file.is_file()
                            and not file.is_symlink()
                            and file.resolve().is_relative_to(directory)
                        }
                    event["artifacts"] = _archive_trial(
                        trial,
                        options.get("artifacts_dir", "runs/artifacts/harbor"),
                        {**event, "snapshot_id": snapshot.id},
                        secrets,
                    )
                except Exception as exc:
                    event["artifact_error"] = str(exc)
                    trajectory = replace(
                        trajectory,
                        status="error",
                        error_message="Harbor artifact archive failed: " + str(exc),
                    )
        review_options = options.get("integrity_review")
        if review_options is not None:
            from flashrsi.integrations._terminal_integrity import review

            # Outside the solver exception path: retry the judge, not the task.
            event = _redact(event, secrets)
            await review(
                task,
                replace(
                    trajectory,
                    output=_redact(trajectory.output, secrets),
                    messages=_redact(trajectory.messages, secrets),
                    usage=_redact(trajectory.usage, secrets),
                    error_message=_redact(trajectory.error_message, secrets),
                ),
                event,
                review_options,
            )
        return replace(
            trajectory,
            output=_redact(trajectory.output, secrets),
            messages=_redact((*trajectory.messages, event), secrets),
            usage=_redact(trajectory.usage, secrets),
            error_message=_redact(trajectory.error_message, secrets),
        )


def _docker_environment_import(native, remote_docker=False):
    shared = resolve_task_verifier_mode(native.config) == VerifierEnvironmentMode.SHARED
    if remote_docker or (
        not (native.paths.environment_dir / "docker-compose.yaml").exists()
        and (shared or not (native.paths.tests_dir / "docker-compose.yaml").exists())
    ):
        return "flashrsi.integrations._harbor_remote:_RemoteDockerEnvironment"
    return None


async def _prepare_shared_verifier(environment):
    """Reset reserved scoring paths before Harbor uploads the native tests."""
    paths = EnvironmentPaths.for_os(environment.os)
    result = await environment.exec("test ! -L /logs", user="root")
    if result.return_code:
        raise RuntimeError(
            "Refusing a linked Harbor logs directory before verification"
        )
    for path, chmod in ((paths.verifier_dir, True), (paths.tests_dir, False)):
        result = await environment.empty_dirs([path], chmod=chmod)
        if result is None or result.return_code:
            raise RuntimeError("Cannot clear Harbor verifier inputs and rewards")


def _positive(value, field):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"agent.{field} must be a positive finite number")
    return value


def _read_log(path):
    return path.read_text(errors="replace") if path.is_file() else ""


def _archive_trial(trial, directory, metadata, secrets):
    """Keep native outputs before temporary cleanup, without staging credentials."""
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    saved = Path(mkdtemp(prefix="trial-", dir=directory))
    record = {"directory": str(saved), "files": []}
    try:
        # Never copy the trial's config/lock or our payload: those can contain
        # solver and verifier credentials. Harbor already collected these outputs.
        for source in (
            trial.paths.artifacts_dir,
            trial.paths.verifier_dir,
            trial.paths.agent_dir,
            trial.paths.result_path,
        ):
            if source.is_symlink() or source.is_junction():
                raise ValueError(f"Refusing linked artifact: {source}")
            if not source.exists():
                continue
            paths = sorted(source.rglob("*")) if source.is_dir() else [source]
            for path in paths:
                if path.is_symlink() or path.is_junction():
                    raise ValueError(f"Refusing linked artifact: {path}")
                if not path.is_file():
                    continue
                relative = path.relative_to(trial.paths.trial_dir)
                destination = saved / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                data = path.read_bytes()
                if path.name == "model.patch" and any(
                    secret.encode() in data for secret in secrets
                ):
                    raise ValueError(
                        "Refusing to archive a patch containing configured credentials"
                    )
                # Deliverables (including patches) must remain reproducible.
                # Redact native logs, not the files that the verifier evaluated.
                if source != trial.paths.artifacts_dir and path.name != "model.patch":
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        pass
                    else:
                        if "\0" not in text:
                            data = _redact(text, secrets).encode("utf-8")
                destination.write_bytes(data)
                record["files"].append(relative.as_posix())
    except Exception as exc:
        record["error"] = str(exc)
        raise RuntimeError(f"Could not archive outputs to {saved}: {exc}") from exc
    finally:
        # Also identify partial archives when a trial fails or is cancelled.
        (saved / "manifest.json").write_text(
            json.dumps(_redact({**metadata, **record}, secrets), ensure_ascii=False),
            encoding="utf-8",
        )
    return record


def _stage(directory, agent, config, prompt):
    config = copy.deepcopy(config)
    config.get("harbor", {}).pop("integrity_review", None)
    config.get("harbor", {}).pop("artifacts_dir", None)
    config.get("harbor", {}).pop("trials_dir", None)
    # Keep evaluation rules outside the candidate's mutable snapshot. Public
    # benchmark repositories expose hidden tests and oracle answers online.
    prompt += (
        "\n\nEvaluation integrity rules: Solve the task independently. Do not "
        "search for, download, read, or use benchmark-specific reference solutions, "
        "hidden verifier tests, expected answers, or other agents' task solutions "
        "from benchmark repositories, mirrors, datasets, or discussion threads. "
        "Do not modify the verifier or forge reward files. Internet access for "
        "ordinary documentation, dependencies, and resources requested by the task "
        "is permitted. Tests and source files explicitly supplied as task inputs "
        "may be inspected; this does not permit fetching hidden benchmark files."
    )
    _stage_payload(directory, agent, config, prompt)


class _SnapshotAgent(BaseAgent):
    """Harbor adapter for our existing, independently versioned harness adapter."""

    def __init__(
        self,
        *args,
        payload_dir,
        uv_installer_url="https://astral.sh/uv/0.11.4/install.sh",
        working_dir=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.payload_dir = Path(payload_dir)
        self.uv_installer_url = uv_installer_url
        self.working_dir = working_dir

    @staticmethod
    def name():
        return "flashrsi-lite"

    def version(self):
        return version("flashrsi-lite")

    async def setup(self, environment):
        await environment.upload_dir(self.payload_dir, _ROOT)
        # uv installs the requested Python independently of a task's Python.
        # Minimal Debian/Ubuntu task images may not include a downloader yet.
        command = _setup_script(self.uv_installer_url)
        result = await environment.exec(command)
        if result.return_code:
            raise RuntimeError(
                f"Container dependency setup failed: {result.stderr or result.stdout}"
            )

    async def run(self, instruction, environment, context):
        command = shlex.join(
            [
                f"{_ROOT}/venv/bin/python",
                "-I",
                "-c",
                f"import sys; sys.path.insert(0, {_ROOT + '/package'!r}); "
                "import runpy; runpy.run_module('flashrsi.integrations._container', "
                "run_name='__main__', alter_sys=True)",
                f"{_ROOT}/request.json",
                str(self.environment_logs_dir / "flashrsi.json"),
            ]
        )
        command = getattr(self, "_command_prefix", "") + command
        # Isolate bridge imports without changing the task's cwd or PYTHONPATH;
        # native tools inherit the image's environment. The entry point removes
        # request.json before starting the harness.
        result = await environment.exec(
            command, **({"cwd": self.working_dir} if self.working_dir else {})
        )
        if not environment.capabilities.mounted and await environment.is_file(
            str(self.environment_logs_dir / "flashrsi.json")
        ):
            await environment.download_file(
                str(self.environment_logs_dir / "flashrsi.json"),
                self.logs_dir / "flashrsi.json",
            )
        context.metadata = {"flashrsi_trajectory": "flashrsi.json"}
        if result.return_code:
            # Harbor continues the verifier for this exception, preserving task
            # feedback even when the harness has failed or reached its timeout.
            raise NonZeroAgentExitCodeError(
                f"Container agent exited with {result.return_code}: "
                f"{result.stderr or result.stdout or ''}"
            )
