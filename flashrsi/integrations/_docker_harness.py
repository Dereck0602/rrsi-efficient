"""Shared Docker setup, native harness execution and resource cleanup."""

import asyncio
import json
import os
import shlex

from flashrsi.core import Trajectory
from flashrsi.integrations._container_payload import _ROOT, _setup_script


async def _setup(
    docker, name, network, payload, config, timeout, *, drop_net_raw=False
):
    installers = {
        "astral": "https://astral.sh/uv/0.11.4/install.sh",
        "github": "https://github.com/astral-sh/uv/releases/download/0.11.4/uv-installer.sh",
    }
    source = config.get("container_uv_install_source", "astral")
    if not isinstance(source, str) or source not in installers:
        raise ValueError("agent.container_uv_install_source must be astral or github")
    # apt/dpkg bootstrap needs Docker's default chown/setuid capabilities.
    await docker(
        "create",
        "--pull=missing",
        "--name",
        name,
        "--init",
        "--security-opt=no-new-privileges",
        *(["--cap-drop=NET_RAW"] if drop_net_raw else []),
        "--pids-limit=512",
        "--memory=2g",
        "--workdir",
        "/task",
        "--entrypoint",
        "/bin/sh",
        "--",
        config.get("container_image", "python:3.12-slim"),
        "-c",
        "while :; do sleep 3600; done",
        timeout=timeout,
    )
    if network is not None:
        await docker("network", "connect", network, name, timeout=timeout)
    await docker("start", name, timeout=timeout)
    await docker("exec", name, "mkdir", "-p", _ROOT, timeout=timeout)
    await docker("cp", str(payload) + os.sep + ".", f"{name}:{_ROOT}", timeout=timeout)
    await docker(
        "exec",
        name,
        "/bin/sh",
        "-c",
        _setup_script(installers[source]),
        timeout=timeout,
    )


async def _run(docker, name, timeout, grace, *, env=None):
    command = shlex.join(
        [
            f"{_ROOT}/venv/bin/python",
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {_ROOT + '/package'!r}); "
            "import runpy; runpy.run_module('flashrsi.integrations._container', run_name='__main__', alter_sys=True)",
            f"{_ROOT}/request.json",
            f"{_ROOT}/trajectory.json",
        ]
    )
    result = await docker(
        "exec",
        *(
            argument
            for key, value in (env or {}).items()
            for argument in ("--env", f"{key}={value}")
        ),
        "--workdir",
        "/task",
        name,
        "/bin/sh",
        "-c",
        command,
        timeout=timeout + grace,
        check=False,
    )
    raw = (
        await docker("exec", name, "cat", f"{_ROOT}/trajectory.json", timeout=grace)
    ).stdout
    values = json.loads(raw)
    values.pop("task", None)
    trajectory = Trajectory(**values)
    if result.returncode and trajectory.status == "finished":
        raise RuntimeError(
            f"Agent process exited with {result.returncode}: {result.stderr[-2000:]}"
        )
    return trajectory


async def _cleanup(docker, containers, network, grace):
    results = await asyncio.gather(
        *(
            docker("rm", "--force", name, timeout=grace, check=False)
            for name in containers
        ),
        return_exceptions=True,
    )
    if network is not None:
        try:
            results.append(
                await docker("network", "rm", network, timeout=grace, check=False)
            )
        except Exception as exc:
            results.append(exc)
    errors = []
    for result in results:
        if isinstance(result, BaseException):
            errors.append(str(result) or type(result).__name__)
        elif result.returncode and not any(
            message in result.stderr for message in ("No such container", "not found")
        ):
            errors.append(result.stderr[-2000:])
    if errors:
        raise RuntimeError("Container cleanup failed: " + "; ".join(errors))
