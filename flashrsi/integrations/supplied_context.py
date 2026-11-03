"""Run supplied-context tasks without access to host evaluation files."""

import asyncio
import math
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from flashrsi.integrations._container import _agent_class
from flashrsi.integrations._container_payload import _stage
from flashrsi.integrations._docker import _docker
from flashrsi.integrations._docker_harness import _cleanup, _run, _setup


def bind(agent):
    agent._check_container()
    cls = type(agent)
    if _agent_class(f"{cls.__module__}:{cls.__qualname__}") is not cls:
        raise ValueError("Supplied-context execution requires an importable Agent")
    if shutil.which("docker") is None:
        raise ValueError("Supplied-context execution requires Docker")
    agent._env_runner = _solve


async def _solve(agent, task):
    if task.env != "supplied_context":
        raise ValueError("Expected a supplied-context task")
    config = agent.config
    timeout = config.get("timeout", 300)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("agent.timeout must be a positive finite number")
    name = f"flashrsi-context-{uuid4().hex}"
    grace = 30
    with TemporaryDirectory(prefix="flashrsi-context-") as temporary:
        # Only the selected harness and public prompt cross the boundary.
        _stage(Path(temporary), agent, config, task.question)
        try:
            await _setup(_docker, name, None, Path(temporary), config, 600)
            return await _run(_docker, name, timeout, grace)
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
