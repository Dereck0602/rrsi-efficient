"""Allow Target traffic only through a per-task proxy with fixed model origins."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from flashrsi.integrations._container_payload import _ROOT
from flashrsi.integrations._docker_harness import _cleanup, _run


def _origins(config):
    llm = config["llm"]
    if not llm.get("base_url"):
        raise ValueError("Model-only execution requires explicit agent.llm.base_url")
    origins = []
    for field in ("base_url", "side_base_url"):
        value = llm.get(field)
        if value:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError(f"agent.llm.{field} must be an HTTP(S) model endpoint")
            origin = (
                parsed.scheme,
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
            )
            if origin not in origins:
                origins.append(origin)
    return origins


async def run(docker, name, timeout, grace, origins):
    async with restricted(docker, name, grace, origins) as env:
        return await _run(docker, name, timeout, grace, env=env)


@asynccontextmanager
async def restricted(docker, name, grace, origins):
    network, proxy = name + "-internal", name + "-proxy"
    try:
        await docker("network", "create", "--internal", network, timeout=grace)
        await docker(
            "create",
            "--pull=missing",
            "--name",
            proxy,
            "--network",
            "bridge",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--sysctl",
            "net.ipv4.ip_forward=0",
            "--sysctl",
            "net.ipv6.conf.all.forwarding=0",
            "--pids-limit=128",
            "--memory=128m",
            "--entrypoint",
            "python3",
            "--",
            "python:3.12-slim",
            "-u",
            "-c",
            Path(__file__).with_name("_model_proxy.py").read_text(encoding="utf-8"),
            json.dumps(origins),
            timeout=grace,
        )
        await docker(
            "network",
            "connect",
            "--alias",
            "model-egress",
            network,
            proxy,
            timeout=grace,
        )
        await docker("network", "connect", network, name, timeout=grace)
        await docker("network", "disconnect", "bridge", name, timeout=grace)
        inspected = await docker(
            "inspect",
            "--format",
            "{{json .NetworkSettings.Networks}}",
            name,
            timeout=grace,
        )
        if set(json.loads(inspected.stdout)) != {network}:
            raise RuntimeError(
                "Target must be connected only to its internal model network"
            )
        await docker("start", proxy, timeout=grace)
        await docker(
            "exec",
            name,
            f"{_ROOT}/venv/bin/python",
            "-c",
            "import socket, time\n"
            "for _ in range(30):\n"
            " try:\n"
            "  with socket.create_connection(('model-egress', 8080), timeout=1): break\n"
            " except OSError: time.sleep(.2)\n"
            "else: raise SystemExit('Model proxy did not start')\n",
            timeout=grace,
        )
        env = {
            key: value
            for upper, value in (
                ("HTTP_PROXY", "http://model-egress:8080"),
                ("HTTPS_PROXY", "http://model-egress:8080"),
                ("ALL_PROXY", "http://model-egress:8080"),
                ("NO_PROXY", ""),
            )
            for key in (upper, upper.lower())
        }
        yield env
    finally:
        cleanup = asyncio.create_task(_cleanup(docker, (name, proxy), network, grace))
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await cleanup
            raise
