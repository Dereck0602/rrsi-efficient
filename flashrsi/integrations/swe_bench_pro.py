"""Locked Pro V2 execution; native replay/scoring at 66f92766bba642462d4bbe5479e83f91f9211862."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from flashrsi._records import _config_record, _redact, _secrets
from flashrsi.benchmarks.swe_bench_pro import _REVISION, _upstream
from flashrsi.core import Trajectory
from flashrsi.integrations._harbor_remote import _check_download
from flashrsi.integrations.harbor import (
    AgentConfig,
    EnvironmentConfig,
    HarborTask,
    TaskConfig,
    Trial,
    TrialConfig,
    VerifierConfig,
    _archive_trial,
    _docker_environment_import,
    _positive,
    _SnapshotAgent,
    _stage,
)
from flashrsi.integrations.harbor import (
    bind as bind_harbor,
)

_PROXY_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _native_tooling():
    root = _upstream() / "v2/tooling"
    modules = []
    for name in ("patch_replay", "locked_claude_code"):
        key = f"_flashrsi_pro_{name}_{_REVISION}"
        if key not in sys.modules:
            spec = importlib.util.spec_from_file_location(key, root / f"{name}.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            sys.modules[key] = module
        modules.append(sys.modules[key])
    return modules[0].__name__ + ":PatchReplayAgent", modules[1]._CAPTURE


def _runtime(agent):
    config = copy.deepcopy(agent.config)
    llm = config["llm"]
    hosts = []
    for field in ("base_url", "side_base_url"):
        value = llm.get(field)
        if field == "base_url" and not value:
            raise ValueError(
                "SWE-bench Pro requires agent.llm.base_url for its model-only allowlist"
            )
        if value:
            url = urlsplit(value)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError(f"agent.llm.{field} must be an HTTP(S) model endpoint")
            hosts.append(url.hostname)
    options = config.get("harbor", {})
    if type(options.get("remote_docker", False)) is not bool:
        raise ValueError("harbor.remote_docker must be a boolean")
    if options.get("integrity_review") is not None:
        raise ValueError(
            "SWE-bench Pro uses its official verifier, not Terminal integrity_review"
        )
    timeout = _positive(config.get("timeout", 3000), "timeout")
    if timeout > 3000:
        raise ValueError("SWE-bench Pro V2 limits agent.timeout to 3000 seconds")
    config["timeout"] = timeout
    config["disallowed_tools"] = list(
        dict.fromkeys([*config.get("disallowed_tools", []), "WebFetch", "WebSearch"])
    )
    # Setup may use a proxy; the solver only receives direct model connectivity.
    if "environment_env" in options:
        config.harbor.environment_env = {
            k: v
            for k, v in options["environment_env"].items()
            if k not in (*_PROXY_ENV, "NO_PROXY", "no_proxy", "PROXY_PASSWORD")
        }
    return config, list(dict.fromkeys(hosts))


def bind(agent, benchmark):
    bind_harbor(agent)
    _runtime(agent)
    verifier_env = benchmark.config.get("verifier_env", {})
    if not isinstance(verifier_env, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str)
        for k, v in verifier_env.items()
    ):
        raise ValueError("benchmark.verifier_env must map environment names to strings")
    verifier_env = dict(verifier_env)

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("SWE-bench Pro requires a loaded benchmark Task")
        return await _run(target, task, verifier_env)

    agent._env_runner = run


class _CaptureAgent(_SnapshotAgent):
    def __init__(self, *args, capture_command, budget, **kwargs):
        super().__init__(*args, **kwargs)
        self.capture_command = capture_command
        self._command_prefix = f"timeout -k 5 {budget:g} "

    async def setup(self, environment):
        await super().setup(environment)
        result = await environment.exec(
            "git -C /app rev-parse --show-toplevel 2>/dev/null || git -C /testbed rev-parse --show-toplevel",
            timeout_sec=30,
        )
        if result.return_code or result.stdout.strip() not in ("/app", "/testbed"):
            raise ValueError("SWE-bench Pro image must contain /app or /testbed")
        self.working_dir = result.stdout.strip()

    async def run(self, instruction, environment, context):
        try:
            # Egress is enforced by Harbor's phase policy. Clear generic proxies
            # so an allowed endpoint is not routed through a blocked proxy host.
            with environment.scoped_exec_env(
                {**dict.fromkeys(_PROXY_ENV, ""), "NO_PROXY": "*", "no_proxy": "*"}
            ):
                await super().run(instruction, environment, context)
        finally:
            result = await environment.exec(self.capture_command, timeout_sec=60)
            if result.return_code:
                raise RuntimeError("SWE-bench Pro patch capture failed")
            await environment.download_file(
                "/logs/agent/model.patch", self.logs_dir / "model.patch"
            )


async def _run(agent, task, verifier_env):
    native = HarborTask(Path(task.env))
    if (
        native.instruction != task.question
        or native.checksum != task.metadata["task_checksum"]
    ):
        raise ValueError("SWE-bench Pro task changed after benchmark.load()")
    config, hosts = _runtime(agent)
    options = agent.config.get("harbor", {})
    setup_timeout = _positive(options.get("setup_timeout", 600), "harbor.setup_timeout")
    grace = _positive(options.get("cleanup_grace", 30), "harbor.cleanup_grace")
    secrets = sorted(
        set(_secrets(agent.config) + _secrets(os.environ) + _secrets(verifier_env)),
        key=len,
        reverse=True,
    )
    trajectory = Trajectory(
        messages=(
            {
                "type": "RunConfig",
                "config": _config_record(config),
                "snapshot_id": agent.snapshot.id,
            },
        )
    )
    event = {
        "type": "SWEBenchProVerifier",
        "task_id": task.task_id,
        "task_checksum": task.metadata["task_checksum"],
        "evaluator_revision": _REVISION,
        "official_evaluation_performed": False,
        "model_allowed_hosts": hosts,
    }
    with TemporaryDirectory(prefix="flashrsi-swe-pro-") as temporary:
        root = Path(temporary)
        payload = root / "payload"
        payload.mkdir(mode=0o700)
        _stage(payload, agent, config, task.question)
        solve = replay = None
        try:
            replay_path, capture = _native_tooling()
            environment = {
                "type": "docker",
                "delete": True,
                "import_path": _docker_environment_import(
                    native, options.get("remote_docker", False)
                ),
            }
            installer = options.get("uv_install_source", "astral")
            installers = {
                "astral": "https://astral.sh/uv/0.11.4/install.sh",
                "github": "https://github.com/astral-sh/uv/releases/download/0.11.4/uv-installer.sh",
            }
            if installer not in installers:
                raise ValueError("harbor.uv_install_source must be astral or github")
            solve = await Trial.create(
                TrialConfig(
                    task=TaskConfig(path=Path(task.env)),
                    trials_dir=root / "solve",
                    agent=AgentConfig(
                        import_path=__name__ + ":_CaptureAgent",
                        model_name=config["llm"]["model_name"],
                        override_timeout_sec=config["timeout"] + grace,
                        override_setup_timeout_sec=setup_timeout,
                        extra_allowed_hosts=hosts,
                        kwargs={
                            "payload_dir": str(payload),
                            "capture_command": capture,
                            "budget": config["timeout"],
                            "uv_installer_url": installers[installer],
                        },
                    ),
                    environment=EnvironmentConfig(
                        **environment, env=dict(options.get("environment_env", {}))
                    ),
                    verifier=VerifierConfig(disable=True),
                )
            )
            solve_result = await solve.run()
            output = solve.paths.agent_dir / "flashrsi.json"
            if output.exists() or output.is_symlink():
                _check_download(output)
                values = json.loads(output.read_text(encoding="utf-8"))
                values.pop("task", None)
                trajectory = Trajectory(**values)
                if not any(
                    m.get("type") == "RunConfig"
                    and m.get("snapshot_id") == agent.snapshot.id
                    for m in trajectory.messages
                ):
                    raise ValueError("SWE-bench Pro result has the wrong Snapshot")
            if solve_result.exception_info is not None:
                trajectory = replace(
                    trajectory,
                    status="error",
                    error_message=trajectory.error_message
                    or solve_result.exception_info.exception_type,
                )
            patch = solve.paths.agent_dir / "model.patch"
            if not patch.is_file() and solve_result.exception_info is not None:
                error = (
                    getattr(solve_result.exception_info, "exception_message", None)
                    or solve_result.exception_info.exception_type
                )
                raise RuntimeError(
                    "SWE-bench Pro solver failed before patch capture: " + error
                )
            if patch.exists() or patch.is_symlink():
                _check_download(patch)
            if not patch.is_file() or patch.stat().st_size > 32 * 1024 * 1024:
                raise ValueError(
                    "SWE-bench Pro captured patch is missing or exceeds 32 MiB"
                )
            data = patch.read_bytes()
            model_patch = data.decode("utf-8")
            if _redact(model_patch, secrets) != model_patch:
                raise ValueError("SWE-bench Pro patch contains configured credentials")
            event.update(
                solve_trial_id=str(solve_result.id),
                patch_sha256=hashlib.sha256(data).hexdigest(),
                patch_bytes=len(data),
                model_patch=model_patch,
                solver_status=trajectory.status,
            )
            if solve_result.exception_info is not None:
                event["solver_error"] = solve_result.exception_info.exception_type
            # A new trial receives only the captured diff, never the solver's
            # filesystem, payload, package environment or API credentials.
            replay = await Trial.create(
                TrialConfig(
                    task=TaskConfig(path=Path(task.env)),
                    trials_dir=root / "regrade",
                    agent=AgentConfig(
                        import_path=replay_path,
                        model_name="replay",
                        kwargs={"source_job": str(root / "solve")},
                    ),
                    environment=EnvironmentConfig(**environment),
                    verifier=VerifierConfig(env=dict(verifier_env)),
                )
            )
            result = await replay.run()
            if result.exception_info is not None:
                raise RuntimeError(
                    "SWE-bench Pro official regrade failed: "
                    + result.exception_info.exception_type
                )
            replay_log = replay.paths.agent_dir / "replay.json"
            _check_download(replay_log)
            replay_record = json.loads(replay_log.read_text(encoding="utf-8"))
            if data and replay_record.get("patch") != str(patch):
                raise ValueError(
                    "Official SWE-bench Pro replay did not find the captured patch"
                )
            apply_rc = replay_record.get("apply_rc")
            event["patch_apply_exit"] = apply_rc
            rewards = result.verifier_result.rewards if result.verifier_result else None
            reward = rewards.get("reward") if isinstance(rewards, dict) else None
            if type(reward) not in (int, float) or reward not in (0, 1):
                raise ValueError(
                    "SWE-bench Pro official verifier must return reward 0 or 1"
                )
            if reward == 1 and (not data or type(apply_rc) is not int or apply_rc != 0):
                raise ValueError(
                    "SWE-bench Pro reward requires a successfully applied patch"
                )
            event.update(
                regrade_trial_id=str(result.id),
                resolved=bool(reward),
                official_evaluation_performed=True,
            )
        except Exception as exc:
            event["error"] = str(exc) or type(exc).__name__
            trajectory = replace(
                trajectory, status="error", error_message=event["error"]
            )
        finally:
            for phase, trial in (("solve", solve), ("regrade", replay)):
                if trial is None:
                    continue
                try:
                    event[phase + "_artifacts"] = _archive_trial(
                        trial,
                        options.get("artifacts_dir", "runs/artifacts/harbor"),
                        {
                            **event,
                            "phase": phase,
                            "snapshot_id": agent.snapshot.id,
                        },
                        secrets,
                    )
                except Exception as exc:
                    event[phase + "_artifact_error"] = str(exc)
                    trajectory = replace(
                        trajectory,
                        status="error",
                        error_message="Harbor artifact archive failed: " + str(exc),
                    )
    return replace(
        trajectory,
        output=_redact(trajectory.output, secrets),
        messages=_redact((*trajectory.messages, event), secrets),
        usage=_redact(trajectory.usage, secrets),
        error_message=_redact(trajectory.error_message, secrets),
    )
