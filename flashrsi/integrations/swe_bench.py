"""Native SWE patch collection and isolated, pinned official evaluation."""

from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import shlex
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from flashrsi._records import _config_record, _redact, _secrets
from flashrsi.benchmarks.swe_bench import _checksum
from flashrsi.core import Trajectory
from flashrsi.integrations import _model_egress, _swe_grade
from flashrsi.integrations._container import _agent_class
from flashrsi.integrations._container_payload import _ROOT, _setup_script, _stage
from flashrsi.integrations._docker import _docker
from flashrsi.integrations._docker_harness import _cleanup


def bind(agent, benchmark=None):
    """Bind full evaluation with a Benchmark, or retain explicit patch collection."""
    agent._check_container()
    agent.snapshot
    _model_egress._origins(agent.config)
    cls = type(agent)
    if _agent_class(f"{cls.__module__}:{cls.__qualname__}") is not cls:
        raise ValueError("SWE execution requires an importable Agent class")
    if shutil.which("docker") is None:
        raise ValueError(
            "SWE execution requires Docker CLI and a Linux container daemon"
        )
    if benchmark is None:
        agent._env_runner = _solve
        return
    _swe_grade.check()
    options = copy.deepcopy(dict(benchmark.config))
    for field, default in (("evaluation_timeout", 900), ("cleanup_grace", 30)):
        value = options.setdefault(field, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"benchmark.{field} must be a positive finite number")

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("SWE evaluation requires a loaded benchmark Task")
        row = copy.deepcopy(benchmark._rows_by_id[task.task_id])
        if _checksum(row) != task.metadata["task_checksum"]:
            raise ValueError("SWE data changed after loading")
        if row.get("image_assets"):
            raise ValueError(
                "This SWE Verified integration does not support image_assets"
            )
        result = await _solve(target, task)
        if result.status == "finished":
            result = await _evaluate(target, task, result, row, options)
        return result

    agent._env_runner = run


async def _reset(name, base, timeout):
    # Official instance images can retain a later repair commit. Keep the task's
    # baseline and ancestors, but remove later refs/reflogs/objects and setup files.
    await _docker(
        "exec",
        name,
        "/bin/bash",
        "-c",
        "set -e\ncd /testbed\n"
        f"git reset --hard {shlex.quote(base)}\n"
        "git clean -fd\ngit checkout --detach\n"
        "git for-each-ref --format='delete %(refname)' | git update-ref --stdin\n"
        "git config --local --remove-section remote.origin || true\n"
        'rm -f -- "$(git rev-parse --git-path ORIG_HEAD)" "$(git rev-parse --git-path FETCH_HEAD)"\n'
        "git reflog expire --expire=now --all\ngit gc --prune=now\n"
        "rm -f -- /root/setup_repo.sh /root/setup_env.sh /root/install.sh "
        "/eval.sh /patch.diff /test.patch /tmp/patch.diff\n",
        timeout=timeout,
    )


async def _solve(agent, task):
    """The enclosing rollout pins this call's Snapshot and configuration."""
    config = copy.deepcopy(agent.config)
    origins = _model_egress._origins(config)
    options = config.get("swe_bench", {})
    timeout = config.get("timeout", 300)
    setup_timeout = options.get("setup_timeout", 600)
    cleanup_grace = options.get("cleanup_grace", 30)
    for name, value in (
        ("timeout", timeout),
        ("swe_bench.setup_timeout", setup_timeout),
        ("swe_bench.cleanup_grace", cleanup_grace),
    ):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"agent.{name} must be a positive finite number")
    snapshot = agent.snapshot
    record = {
        "type": "RunConfig",
        "config": _config_record(config),
        "snapshot_id": snapshot.id,
    }
    secrets = sorted(
        set(_secrets(config) + _secrets(os.environ)), key=len, reverse=True
    )
    trajectory = Trajectory(messages=(record,))
    name = f"flashrsi-swe-{uuid4().hex}"
    with TemporaryDirectory(prefix="flashrsi-swe-") as temporary:
        payload = Path(temporary) / "payload"
        payload.mkdir(mode=0o700)
        _stage(payload, agent, config, task.question)
        phase = "setup"
        try:
            async with asyncio.timeout(setup_timeout):
                await _docker(
                    "create",
                    "--pull=missing",
                    "--name",
                    name,
                    "--init",
                    "--cap-drop=NET_RAW",
                    "--user",
                    "root",
                    "--workdir",
                    "/testbed",
                    "--entrypoint",
                    "/bin/sh",
                    "--",
                    task.env,
                    "-c",
                    "while :; do sleep 3600; done",
                    timeout=setup_timeout,
                )
                await _docker("start", name, timeout=setup_timeout)
                image = (
                    await _docker(
                        "inspect", "--format", "{{.Image}}", name, timeout=setup_timeout
                    )
                ).stdout.strip()
                await _docker("exec", name, "mkdir", "-p", _ROOT, timeout=setup_timeout)
                # Copy only the adapter, Snapshot and request. Never mount the
                # host workspace, dataset cache, Docker socket or evaluator.
                await _docker(
                    "cp",
                    str(payload) + os.sep + ".",
                    f"{name}:{_ROOT}",
                    timeout=setup_timeout,
                )
                await _docker(
                    "exec",
                    name,
                    "git",
                    "config",
                    "--global",
                    "--add",
                    "safe.directory",
                    "/testbed",
                    timeout=setup_timeout,
                )
                await _reset(name, task.metadata["base_commit"], setup_timeout)
                setup = _setup_script()
                await _docker(
                    "exec", name, "/bin/sh", "-c", setup, timeout=setup_timeout
                )
            phase = "model network setup"
            async with _model_egress.restricted(
                _docker, name, cleanup_grace, origins
            ) as env:
                phase = "execution"
                # Let the native timeout handler finish its trace before enforcing
                # the outer deadline; both harnesses also reap their own children.
                async with asyncio.timeout(timeout + cleanup_grace):
                    command = shlex.join(
                        [
                            f"{_ROOT}/venv/bin/python",
                            "-I",
                            "-c",
                            f"import sys; sys.path.insert(0, {_ROOT + '/package'!r}); "
                            "import runpy; runpy.run_module('flashrsi.integrations._container', "
                            "run_name='__main__', alter_sys=True)",
                            f"{_ROOT}/request.json",
                            f"{_ROOT}/trajectory.json",
                        ]
                    )
                    result = await _docker(
                        "exec",
                        *(arg for k, v in env.items() for arg in ("--env", f"{k}={v}")),
                        "--workdir",
                        "/testbed",
                        name,
                        "/bin/bash",
                        "-c",
                        "set -e\nsource /opt/miniconda3/bin/activate testbed\nexec "
                        + command,
                        timeout=timeout + cleanup_grace,
                        check=False,
                    )
                    # Read in-container rather than copying an Agent-controlled
                    # file/symlink to the host filesystem.
                    raw = (
                        await _docker(
                            "exec",
                            name,
                            "cat",
                            f"{_ROOT}/trajectory.json",
                            timeout=timeout,
                        )
                    ).stdout
                    values = json.loads(raw)
                    values.pop("task", None)
                    trajectory = Trajectory(**values)
                    trajectory = replace(
                        trajectory,
                        messages=(
                            record,
                            *(
                                item
                                for item in trajectory.messages
                                if item.get("type")
                                not in (
                                    "RunConfig",
                                    "SWEBenchPatch",
                                    "SWEBenchVerifier",
                                )
                            ),
                        ),
                    )
                    if result.returncode and trajectory.status == "finished":
                        raise RuntimeError(
                            f"SWE Agent process exited with {result.returncode}: {result.stderr[-4000:]}"
                        )
                    # diff against base includes committed, staged and unstaged
                    # changes. Intent-to-add also includes new nonignored files.
                    await _docker(
                        "exec",
                        name,
                        "git",
                        "-C",
                        "/testbed",
                        "add",
                        "-N",
                        "--",
                        ".",
                        timeout=timeout,
                    )
                    patch = (
                        await _docker(
                            "exec",
                            name,
                            "git",
                            "-c",
                            "core.quotePath=true",
                            "-C",
                            "/testbed",
                            "diff",
                            "--no-ext-diff",
                            "--no-textconv",
                            "--binary",
                            "--full-index",
                            task.metadata["base_commit"],
                            "--",
                            ".",
                            timeout=timeout,
                        )
                    ).stdout
                    if _redact(patch, secrets) != patch:
                        raise ValueError("SWE patch contains configured credentials")
                    event = {
                        "type": "SWEBenchPatch",
                        "task_id": task.task_id,
                        "task_checksum": task.metadata["task_checksum"],
                        "snapshot_id": snapshot.id,
                        "base_commit": task.metadata["base_commit"],
                        "image_id": image,
                        "model_patch": patch,
                    }
                    trajectory = replace(
                        trajectory, messages=(*trajectory.messages, event)
                    )
        except TimeoutError:
            trajectory = replace(
                trajectory, status="error", error_message=f"SWE {phase} timed out"
            )
        except Exception as exc:
            trajectory = replace(
                trajectory, status="error", error_message=str(exc) or type(exc).__name__
            )
        finally:
            # Shield removal so task cancellation still waits for cleanup.
            cleanup = asyncio.create_task(
                _docker("rm", "--force", name, timeout=cleanup_grace, check=False)
            )
            cleanup_error = None
            try:
                removed = await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            except Exception as exc:
                cleanup_error = str(exc) or type(exc).__name__
            else:
                if removed.returncode and "No such container" not in removed.stderr:
                    cleanup_error = removed.stderr[-4000:]
            if cleanup_error is not None:
                prior = (
                    f"{trajectory.error_message}; " if trajectory.error_message else ""
                )
                trajectory = replace(
                    trajectory,
                    status="error",
                    error_message=f"{prior}SWE cleanup failed: {cleanup_error}",
                )
    return replace(
        trajectory,
        output=_redact(trajectory.output, secrets),
        messages=_redact(trajectory.messages, secrets),
        usage=_redact(trajectory.usage, secrets),
        error_message=_redact(trajectory.error_message, secrets),
    )


async def _official(action, root, timeout):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        # The native parser opens our UTF-8 logs using Python's default encoding.
        "-X",
        "utf8",
        str(Path(_swe_grade.__file__)),
        action,
        str(root),
        cwd=root,
        env={**os.environ, "PYTHON_DOTENV_DISABLED": "1"},
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
    if process.returncode:
        # The private helper emits exception types only, never label contents.
        raise RuntimeError(
            stderr.decode(errors="replace").strip() or "Official SWE evaluator failed"
        )
    return json.loads(stdout)


async def _evaluate(agent, task, trajectory, row, options):
    timeout, grace = options["evaluation_timeout"], options["cleanup_grace"]
    event = trajectory.messages[-1]
    patch, image = event["model_patch"], event["image_id"]
    name = f"flashrsi-swe-grade-{uuid4().hex}"
    with TemporaryDirectory(prefix="flashrsi-swe-grade-") as temporary:
        root = Path(temporary)
        try:
            async with asyncio.timeout(timeout):
                # Gold code is unnecessary for scoring. Test names, native script
                # and parser stay in this host-owned directory, never in Task.
                fields = (
                    "instance_id",
                    "image",
                    "repo",
                    "version",
                    "eval_script",
                    "log_parser",
                    "eval_type",
                    "FAIL_TO_PASS",
                    "PASS_TO_PASS",
                )
                (root / "request.json").write_text(
                    json.dumps({"row": {key: row[key] for key in fields}}),
                    encoding="utf-8",
                )
                (root / "model.patch").write_text(patch, encoding="utf-8", newline="\n")
                prepared = await _official("prepare", root, timeout)
                await _docker(
                    "create",
                    "--name",
                    name,
                    "--init",
                    "--user",
                    "root",
                    "--network=none",
                    "--security-opt=no-new-privileges",
                    "--memory=4g",
                    "--cpus=2",
                    "--pids-limit=512",
                    # Docker CLI can inject proxy credentials from config.json.
                    # The offline grader has no reason to inherit any of them.
                    *[
                        f"--env={name}="
                        for key in (
                            "HTTP_PROXY",
                            "HTTPS_PROXY",
                            "FTP_PROXY",
                            "ALL_PROXY",
                            "NO_PROXY",
                        )
                        for name in (key, key.lower())
                    ],
                    "--workdir",
                    "/testbed",
                    "--entrypoint",
                    "/bin/sh",
                    "--",
                    image,
                    "-c",
                    "while :; do sleep 3600; done",
                    timeout=timeout,
                )
                await _docker("start", name, timeout=timeout)
                await _docker(
                    "exec",
                    name,
                    "git",
                    "config",
                    "--global",
                    "--add",
                    "safe.directory",
                    "/testbed",
                    timeout=timeout,
                )
                await _reset(name, task.metadata["base_commit"], timeout)
                inputs = root / "inputs"
                inputs.mkdir()
                for filename in ("eval.sh", "model.patch"):
                    shutil.copyfile(root / filename, inputs / filename)
                location = "/tmp/flashrsi-evaluation"
                await _docker("exec", name, "mkdir", "-p", location, timeout=timeout)
                await _docker(
                    "cp",
                    str(inputs) + os.sep + ".",
                    f"{name}:{location}",
                    timeout=timeout,
                )
                applied = False
                # Native run_evaluation excludes empty predictions before running
                # instances; some patch CLI fallbacks otherwise accept an empty file.
                commands = prepared["apply_commands"] if patch.strip() else ()
                for index, command in enumerate(commands):
                    if index:
                        await _docker(
                            "exec",
                            name,
                            "/bin/bash",
                            "-c",
                            "cd /testbed && git checkout -- . && git clean -fd",
                            timeout=timeout,
                        )
                    result = await _docker(
                        "exec",
                        "--workdir",
                        "/testbed",
                        name,
                        "/bin/bash",
                        "-c",
                        f"{command} {location}/model.patch",
                        timeout=timeout,
                        check=False,
                    )
                    if result.returncode == 0:
                        applied = True
                        break
                if commands and not applied:
                    result = await _docker(
                        "exec",
                        "--workdir",
                        "/testbed",
                        name,
                        "git",
                        "apply",
                        "--check",
                        "--reverse",
                        f"{location}/model.patch",
                        timeout=timeout,
                        check=False,
                    )
                    applied = result.returncode == 0
                if applied:
                    await _docker(
                        "exec",
                        name,
                        "/bin/bash",
                        "-c",
                        f"ulimit -f 32768; /bin/bash {location}/eval.sh > {location}/test_output.txt 2>&1",
                        timeout=timeout,
                        check=False,
                    )
                    size = int(
                        (
                            await _docker(
                                "exec",
                                name,
                                "wc",
                                "-c",
                                f"{location}/test_output.txt",
                                timeout=timeout,
                            )
                        ).stdout.split()[0]
                    )
                    if size > 32 * 1024 * 1024:
                        raise ValueError("SWE test output exceeds 32 MiB")
                    output = (
                        await _docker(
                            "exec",
                            name,
                            "cat",
                            f"{location}/test_output.txt",
                            timeout=timeout,
                        )
                    ).stdout
                else:
                    output = prepared["apply_patch_fail"]
                (root / "test_output.txt").write_text(output, encoding="utf-8")
                verdict = await _official("grade", root, timeout)
                if (
                    type(verdict.get("resolved")) is not bool
                    or type(verdict.get("patch_successfully_applied")) is not bool
                    or (
                        verdict["resolved"]
                        and not (applied and verdict["patch_successfully_applied"])
                    )
                ):
                    raise ValueError("Invalid official SWE evaluator verdict")
                trajectory = replace(
                    trajectory,
                    messages=(
                        *trajectory.messages,
                        {
                            "type": "SWEBenchVerifier",
                            "task_id": task.task_id,
                            "task_checksum": task.metadata["task_checksum"],
                            "snapshot_id": agent.snapshot.id,
                            "image_id": image,
                            "evaluator_revision": _swe_grade.REVISION,
                            "official_evaluation_performed": True,
                            **verdict,
                        },
                    ),
                )
        except TimeoutError:
            trajectory = replace(
                trajectory, status="error", error_message="SWE evaluation timed out"
            )
        except Exception as exc:
            trajectory = replace(
                trajectory, status="error", error_message=str(exc) or type(exc).__name__
            )
        finally:
            cleanup = asyncio.create_task(_cleanup(_docker, (name,), None, grace))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await asyncio.gather(cleanup, return_exceptions=True)
                raise
            except Exception as exc:
                prior = (
                    f"{trajectory.error_message}; " if trajectory.error_message else ""
                )
                trajectory = replace(
                    trajectory,
                    status="error",
                    error_message=prior + (str(exc) or type(exc).__name__),
                )
    secrets = sorted(
        set(_secrets(agent.config) + _secrets(os.environ)), key=len, reverse=True
    )
    return replace(
        trajectory,
        output=_redact(trajectory.output, secrets),
        messages=_redact(trajectory.messages, secrets),
        usage=_redact(trajectory.usage, secrets),
        error_message=_redact(trajectory.error_message, secrets),
    )
