"""Run supplied-context tasks with isolated files and model-only access."""

import asyncio
import math
import os
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from flashrsi.integrations import _model_egress
from flashrsi.integrations._container import _agent_class
from flashrsi.integrations._container_payload import _stage
from flashrsi.integrations._docker import _docker
from flashrsi.integrations._docker_harness import _cleanup, _run, _setup


def bind(agent):
    _check(agent)
    _model_egress._origins(agent.config)
    agent._env_runner = _solve


def _check(agent):
    agent._check_container()
    cls = type(agent)
    if _agent_class(f"{cls.__module__}:{cls.__qualname__}") is not cls:
        raise ValueError("Supplied-context execution requires an importable Agent")
    if shutil.which("docker") is None:
        raise ValueError("Supplied-context execution requires Docker")


async def _solve(agent, task):
    if task.env != "supplied_context":
        raise ValueError("Expected a supplied-context task")
    return await _run_prompt(agent, task.question, model_only=True)


async def _run_prompt(
    agent, prompt, *, workspace=None, collect=None, execute=None, model_only=False
):
    config = agent.config
    timeout = config.get("timeout", 300)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("agent.timeout must be a positive finite number")
    origins = _model_egress._origins(config) if model_only else None
    name = f"flashrsi-context-{uuid4().hex}"
    grace = 30
    with TemporaryDirectory(prefix="flashrsi-context-") as temporary:
        # Only the selected harness and public prompt cross the boundary.
        _stage(Path(temporary), agent, config, prompt)
        try:
            options = {"drop_net_raw": True} if model_only else {}
            await _setup(_docker, name, None, Path(temporary), config, 600, **options)
            if workspace is not None:
                await _docker(
                    "cp", str(workspace) + os.sep + ".", f"{name}:/task", timeout=600
                )
            if model_only:
                return await _model_egress.run(_docker, name, timeout, grace, origins)
            result = await (execute or _run)(_docker, name, timeout, grace)
            if collect is not None and result.status == "finished":
                await collect(_docker, name, grace)
            return result
        finally:
            failure = sys.exception()
            cleanup = asyncio.create_task(_cleanup(_docker, (name,), None, grace))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
            except Exception as exc:
                if failure is None:
                    raise
                failure.add_note(str(exc))
