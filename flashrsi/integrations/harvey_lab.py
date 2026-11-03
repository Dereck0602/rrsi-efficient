"""Native file workspaces followed by Harvey LAB's host-only official evaluator."""

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from tempfile import TemporaryDirectory
from types import MappingProxyType
from uuid import uuid4

from flashrsi._records import _redact, _secrets
from flashrsi._startup import _url
from flashrsi.agents.miniagent import _stop
from flashrsi.benchmarks._archive import _is_safe
from flashrsi.benchmarks.harvey_lab import JUDGES, REVISION, _files
from flashrsi.integrations import _harvey_lab_grade, supplied_context


def bind(agent, benchmark):
    supplied_context._check(agent)
    _harvey_lab_grade.check()
    # Import once at startup so missing document/provider dependencies fail
    # before any target calls, not after an expensive legal assignment.
    from lab_core.evaluation import run_eval  # noqa: F401

    if (
        agent.name == "claude_code"
        and agent.config.get("permission_mode") != "bypassPermissions"
        and not any(
            t == "Bash" or t.startswith("Bash(")
            for t in agent.config.get("allowed_tools", [])
        )
    ):
        raise ValueError(
            "Harvey LAB requires Claude Code allowed_tools to include Bash"
        )
    if shutil.which("pandoc") is None:
        raise ValueError("Harvey LAB requires pandoc >=3.5 on PATH; see README.md")
    version = subprocess.run(
        ["pandoc", "--version"], capture_output=True, text=True, timeout=10
    )
    match = re.match(r"pandoc (\d+)\.(\d+)", version.stdout)
    if version.returncode or not match or tuple(map(int, match.groups())) < (3, 5):
        raise ValueError("Harvey LAB requires pandoc >=3.5")
    judge = copy.deepcopy(dict(benchmark.config.get("judge", {})))
    if set(judge) - {"anthropic", "openai", "parallel", "timeout"}:
        raise ValueError(
            "Harvey LAB judge accepts anthropic, openai, parallel and timeout"
        )
    for provider, model in zip(("anthropic", "openai"), JUDGES, strict=True):
        llm = judge.setdefault(provider, {})
        if not isinstance(llm, Mapping):
            raise ValueError(f"Harvey LAB judge.{provider} must be a mapping")
        llm.setdefault("model_name", model)
        if not isinstance(llm.get("api_key"), str) or not llm["api_key"].strip():
            raise ValueError(
                f"Harvey LAB requires independent benchmark.judge.{provider}.api_key"
            )
        if set(llm) - {"api_key", "model_name", "base_url"}:
            raise ValueError(
                f"Harvey LAB judge.{provider} accepts model_name, api_key and base_url"
            )
        name = llm["model_name"]
        if (
            not isinstance(name, str)
            or "/" in name
            or "\\" in name
            or not name.startswith(
                "claude" if provider == "anthropic" else ("gpt", "o1", "o3", "o4", "o5")
            )
        ):
            raise ValueError(
                f"Harvey LAB judge.{provider}.model_name must identify its native provider"
            )
        if llm.get("base_url"):
            _url(llm["base_url"], f"benchmark.judge.{provider}.base_url")
    judge.setdefault("timeout", 1800)
    if (
        type(judge["timeout"]) not in (float, int)
        or not math.isfinite(judge["timeout"])
        or judge["timeout"] <= 0
    ):
        raise ValueError("Harvey LAB judge.timeout must be positive and finite")
    judge.setdefault("parallel", 6)
    if type(judge["parallel"]) is not int or judge["parallel"] < 1:
        raise ValueError("Harvey LAB judge.parallel must be a positive integer")
    max_bytes = benchmark.config.get("max_artifact_bytes", 64 * 1024 * 1024)
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("Harvey LAB max_artifact_bytes must be a positive integer")
    if benchmark.config.get("artifacts_path"):
        benchmark._artifacts_dir = (
            Path(benchmark.config["artifacts_path"]).expanduser().resolve()
        )
    secrets = sorted(set(_secrets(judge) + _secrets(os.environ)), key=len, reverse=True)
    benchmark.metadata = MappingProxyType(
        {
            **benchmark.metadata,
            "judges": [judge[k]["model_name"] for k in ("anthropic", "openai")],
            "judge_profile": "lab-standard-dual-v1"
            if tuple(judge[k]["model_name"] for k in ("anthropic", "openai")) == JUDGES
            else "custom-dual",
        }
    )

    async def run(target, task):
        _, documents = benchmark._resources(task)
        with TemporaryDirectory(prefix="flashrsi-harvey-workspace-") as temporary:
            workspace = Path(temporary)
            shutil.copytree(documents, workspace / "documents")
            (workspace / "output").mkdir()
            collected = workspace / "collected"
            collection_error = None

            async def collect(docker, name, grace):
                nonlocal collection_error
                try:
                    await docker("stop", "--time", "1", name, timeout=grace)
                    await _copy_output(name, collected, max_bytes, grace)
                except Exception as exc:
                    collection_error = type(exc).__name__

            result = await supplied_context._run_prompt(
                target,
                task.question
                + "\n\nWorkspace: ./documents contains the supplied matter documents. Write all deliverable files to ./output. Use your native file and shell tools; your final text is not a deliverable file.",
                workspace=workspace,
                collect=collect,
            )
            result = replace(
                result,
                messages=tuple(
                    m
                    for m in result.messages
                    if m.get("type") not in {"HarveyLABVerifier", "HarveyLABArtifacts"}
                ),
            )
            if collection_error:
                result = replace(
                    result,
                    status="error",
                    error_message=f"Harvey LAB output collection failed ({collection_error})",
                )
            if result.status == "finished":
                try:
                    with TemporaryDirectory(prefix="flashrsi-harvey-grade-") as trusted:
                        root = Path(trusted)
                        # Recheck originals after execution, and copy only native
                        # criteria and collected regular outputs into grading.
                        original, _ = benchmark._resources(task)
                        task_dir = root / "tasks" / task.task_id
                        task_dir.mkdir(parents=True)
                        (task_dir / "task.json").write_text(
                            json.dumps(original), encoding="utf-8"
                        )
                        run_dir = root / "results" / "run"
                        output = run_dir / "output"
                        artifacts = _collect(collected, output, max_bytes)
                        destination = None
                        if benchmark._artifacts_dir is not None:
                            destination = benchmark._artifacts_dir / uuid4().hex
                            destination.mkdir(parents=True, mode=0o700)
                            shutil.copytree(output, destination / "output")
                        result = replace(
                            result,
                            messages=(
                                *result.messages,
                                {
                                    "type": "HarveyLABArtifacts",
                                    "directory": str(destination / "output")
                                    if destination
                                    else None,
                                    "files": artifacts,
                                },
                            ),
                        )
                        try:
                            metrics = await _evaluate(
                                root, {**judge, "task_id": task.task_id}
                            )
                        finally:
                            if destination:
                                for report in run_dir.glob("scores*.json"):
                                    (destination / report.name).write_text(
                                        _redact(
                                            report.read_text(encoding="utf-8"), secrets
                                        ),
                                        encoding="utf-8",
                                    )
                        result = replace(
                            result,
                            messages=(
                                *result.messages,
                                {
                                    "type": "HarveyLABVerifier",
                                    "task_id": task.task_id,
                                    "task_checksum": task.metadata["task_checksum"],
                                    "snapshot_id": target.snapshot.id,
                                    "scorer_revision": REVISION,
                                    "judges": [
                                        judge[k]["model_name"]
                                        for k in ("anthropic", "openai")
                                    ],
                                    "metrics": metrics,
                                },
                            ),
                        )
                except Exception as exc:
                    result = replace(
                        result,
                        status="error",
                        error_message=f"Harvey LAB evaluation failed ({type(exc).__name__})",
                    )
        return replace(
            result,
            output=_redact(result.output, secrets),
            messages=_redact(result.messages, secrets),
            usage=_redact(result.usage, secrets),
            error_message=_redact(result.error_message, secrets),
        )

    agent._env_runner = run


async def _copy_output(name, destination, max_bytes, timeout):
    # Docker sends a tar stream from the stopped container. Bound the transfer
    # before parsing it and never let Docker extract candidate files on the host.
    archive_path = destination.with_suffix(".tar")
    limit = max_bytes + 16 * 1024 * 1024  # headers, padding and directories
    process = await asyncio.create_subprocess_exec(
        "docker",
        "cp",
        f"{name}:/task/output",
        "-",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(timeout):
            with archive_path.open("xb") as stream:
                while chunk := await process.stdout.read(64 * 1024):
                    limit -= len(chunk)
                    if limit < 0:
                        raise ValueError(
                            "Harvey LAB output archive exceeds transfer limit"
                        )
                    stream.write(chunk)
            if await process.wait():
                raise RuntimeError("Cannot copy Harvey LAB output from container")
    finally:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
    _unpack_output(archive_path, destination, max_bytes)


def _unpack_output(archive_path, destination, max_bytes):
    destination.mkdir()
    seen = set()
    total = 0
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive:
            name = member.name.rstrip("/") if member.isdir() else member.name
            parts = name.split("/")
            if (
                not _is_safe(name)
                or parts[0] != "output"
                or any(
                    part.endswith((".", " "))
                    or PureWindowsPath(part).is_reserved()
                    or any(c in '<>"|?*' or ord(c) < 32 for c in part)
                    for part in parts
                )
                or name.casefold() in seen
                or len(seen) >= 10_000
                or member.size < 0
                or not (member.isdir() or member.isfile() and not member.issparse())
                or (len(parts) == 1 and not member.isdir())
            ):
                raise ValueError("Harvey LAB output contains an unsafe archive entry")
            seen.add(name.casefold())
            path = destination.joinpath(*parts[1:])
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            total += member.size
            if total > max_bytes:
                raise ValueError("Harvey LAB output exceeds max_artifact_bytes")
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open("xb") as stream:
                shutil.copyfileobj(source, stream)
    if "output" not in seen:
        raise ValueError("Harvey LAB output archive is missing its root directory")


def _collect(source, destination, max_bytes):
    files = _files(source)
    if sum(p.stat().st_size for p in files) > max_bytes:
        raise ValueError("Harvey LAB output exceeds max_artifact_bytes")
    destination.mkdir(parents=True)
    records = []
    for file in files:
        relative = file.relative_to(source)
        copied = destination / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, copied)
        records.append(
            {
                "path": relative.as_posix(),
                "size": copied.stat().st_size,
                "sha256": hashlib.sha256(copied.read_bytes()).hexdigest(),
            }
        )
    return records


async def _evaluate(root, request):
    # Do not inherit the target/Worker's keys, login state, LAB_ROOT or .env.
    names = {
        "PATH",
        "SYSTEMROOT",
        "WINDIR",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "TMP",
        "TEMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in names}
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-m",
        "flashrsi.integrations._harvey_lab_grade",
        str(root),
        cwd=root,
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(request, default=dict).encode()),
            timeout=request["timeout"],
        )
        if process.returncode:
            raise RuntimeError("Official Harvey LAB evaluation failed")
        return json.loads(stdout)
    finally:
        await _stop(process)
