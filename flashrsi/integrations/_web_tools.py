"""Task-local search/read bridge shared by web research benchmarks."""

import asyncio
import copy
import json
import math
import shlex
import shutil
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi._records import _redact, _secrets
from flashrsi.integrations import _browsecomp_web, supplied_context
from flashrsi.integrations._container_payload import _ROOT
from flashrsi.integrations._docker_harness import _run

_PYTHON = f"{_ROOT}/venv/bin/python"


def _respond(config, raw, records, event_type, secrets):
    value = {}
    try:
        value = json.loads(raw)
        action = value["action"]
        if action == "search":
            query = value["query"]
            top_k = value.get("top_k")
            top_k = config["top_k"] if top_k is None else top_k
            if (
                not isinstance(query, str)
                or not query.strip()
                or type(top_k) is not int
                or not 1 <= top_k <= 20
            ):
                raise ValueError("Search requires a query and top_k in 1..20")
            result = _browsecomp_web.search(config, query, top_k)
        elif action == "read":
            result = _browsecomp_web.read(config, value["url"])
        else:
            raise ValueError("Unknown web tool")
    except Exception as exc:
        result = {"error": f"Web tool failed ({type(exc).__name__})"}
    result = _redact(result, secrets)
    records.append(
        {"type": event_type, "request": _redact(value, secrets), "response": result}
    )
    return result


async def _relay(process, respond):
    while raw := await process.stdout.readline():
        # State changes must finish before the caller grades or closes a session.
        response = asyncio.create_task(asyncio.to_thread(respond, raw))
        try:
            result = await asyncio.shield(response)
        except asyncio.CancelledError:
            await response
            raise
        process.stdin.write(json.dumps(result).encode() + b"\n")
        await process.stdin.drain()


def _check_target(agent, label):
    supplied_context._check(agent)
    if (
        agent.name == "claude_code"
        and agent.config.get("permission_mode") != "bypassPermissions"
        and not any(
            tool == "Bash" or tool.startswith("Bash(")
            for tool in agent.config.get("allowed_tools", ["Read", "Glob", "Grep"])
        )
    ):
        raise ValueError(f"{label} requires Claude Code allowed_tools to include Bash")


def settings(agent, config, label):
    _check_target(agent, label)
    retrieval = copy.deepcopy(dict(config.get("retrieval", {})))
    provider = retrieval.setdefault("provider", "tavily")
    if provider not in ("tavily", "serper", "json"):
        raise ValueError("retrieval.provider must be tavily, serper or json")
    if provider in ("tavily", "serper"):
        retrieval.setdefault(
            "url",
            "https://api.tavily.com/search"
            if provider == "tavily"
            else "https://google.serper.dev/search",
        )
        if not retrieval.get("api_key"):
            raise ValueError(f"{provider.title()} requires benchmark.retrieval.api_key")
    reader = retrieval.setdefault("reader", {})
    if reader.setdefault("provider", "direct") not in ("direct", "jina"):
        raise ValueError("retrieval.reader.provider must be direct or jina")
    if reader["provider"] == "jina":
        reader.setdefault("url", "https://r.jina.ai/")
        _browsecomp_web._http_url(reader["url"])
        if not reader.get("api_key"):
            raise ValueError("Jina requires benchmark.retrieval.reader.api_key")
    _browsecomp_web._http_url(retrieval.get("url", ""))
    retrieval.setdefault("timeout", 30)
    retrieval.setdefault("top_k", 5)
    retrieval.setdefault("max_page_chars", 20000)
    retrieval.setdefault("search_depth", "basic")
    if retrieval["search_depth"] not in ("basic", "advanced"):
        raise ValueError("retrieval.search_depth must be basic or advanced")
    if type(retrieval["top_k"]) is not int or not 1 <= retrieval["top_k"] <= 20:
        raise ValueError("retrieval.top_k must be in 1..20")
    if type(retrieval["max_page_chars"]) is not int or retrieval["max_page_chars"] < 1:
        raise ValueError("retrieval.max_page_chars must be a positive integer")
    timeout = retrieval["timeout"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Web retrieval timeout must be positive and finite")
    return retrieval


def _command(timeout):
    return shlex.join([_PYTHON, "/task/web.py", "--timeout", str(timeout)])


async def _run_with_relay(target, prompt, respond):
    async def execute(docker, name, timeout, grace):
        # Only the task container has the client socket. The host receives
        # requests over an attached Docker exec, without a listening host port.
        process = await asyncio.create_subprocess_exec(
            "docker",
            "exec",
            "-i",
            name,
            _PYTHON,
            "-u",
            "/task/web.py",
            "--relay",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=65537,
        )
        relay = solver = None
        try:
            if await asyncio.wait_for(process.stdout.readline(), grace) != b"ready\n":
                raise RuntimeError("Web relay did not start")
            relay = asyncio.create_task(_relay(process, respond))
            solver = asyncio.create_task(_run(docker, name, timeout, grace))
            done, _ = await asyncio.wait(
                (relay, solver), return_when=asyncio.FIRST_COMPLETED
            )
            if solver in done:
                return solver.result()
            await relay
            raise RuntimeError("Web relay stopped before the Target finished")
        finally:
            pending = [task for task in (relay, solver) if task is not None]
            for running in pending:
                running.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.communicate()

    with TemporaryDirectory(prefix="flashrsi-web-") as temporary:
        root = Path(temporary)
        shutil.copyfile(
            Path(__file__).with_name("_browsecomp_client.py"), root / "web.py"
        )
        return await supplied_context._run_prompt(
            target, prompt, workspace=root, execute=execute
        )


async def run(target, task, retrieval, label):
    records = []
    secrets = _secrets(retrieval)
    command = _command(retrieval["timeout"] + 5)
    prompt = (
        task.question
        + f'\n\nWeb tools (invoke using your shell tool):\nSearch: {command} search "your query" [--top-k N]\nRead page: {command} read "https://example.org/page"\nSearch returns titles, URLs and snippets. '
        + (
            "Reading uses Jina Reader to return public page content."
            if retrieval["reader"]["provider"] == "jina"
            else "Reading supports public HTML/text pages; JavaScript and PDF rendering are not provided."
        )
    )

    def respond(raw):
        return _respond(retrieval, raw, records, label + "Web", secrets)

    result = await _run_with_relay(target, prompt, respond)
    return replace(
        result,
        output=_redact(result.output, secrets),
        messages=_redact(
            tuple(
                m
                for m in result.messages
                if m.get("type") not in (label + "Verifier", label + "Web")
            )
            + tuple(records),
            secrets,
        ),
        usage=_redact(result.usage, secrets),
        error_message=_redact(result.error_message, secrets),
    )
