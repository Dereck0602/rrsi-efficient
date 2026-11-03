"""Bind sole-planning execution, independent conversion and native host scoring."""

import asyncio
import copy
import importlib.util
import json
import math
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType

from flashrsi._records import _redact, _secrets
from flashrsi._startup import _url
from flashrsi.agents.miniagent import _stop
from flashrsi.benchmarks.travelplanner import REVISION
from flashrsi.integrations import (
    _travelplanner_grade,
    _travelplanner_postprocess,
    supplied_context,
)

DATABASE_SOURCE = "osunlp/TravelPlannerEnvironment"
DATABASE_REVISION = "f2183898e2b9547ed2a6f5356ae4cbc3f3431bf4"


def bind(agent, benchmark):
    supplied_context._check(agent)
    for module in ("openai", "pandas", "numpy", "requests"):
        if importlib.util.find_spec(module) is None:
            raise ValueError("Install flashrsi-lite[all]; see README.md")
    postprocess = copy.deepcopy(dict(benchmark.config.get("postprocess", {})))
    if set(postprocess) - {"llm", "timeout", "max_retries"}:
        raise ValueError(
            "TravelPlanner postprocess accepts llm, timeout and max_retries"
        )
    llm = postprocess.setdefault("llm", {})
    if not isinstance(llm, Mapping):
        raise ValueError("TravelPlanner postprocess.llm must be a mapping")
    llm.setdefault("model_name", _travelplanner_postprocess.MODEL)
    if (
        not isinstance(llm.get("api_key"), str)
        or not llm["api_key"].strip()
        or not isinstance(llm["model_name"], str)
        or not llm["model_name"].strip()
        or set(llm) - {"model_name", "api_key", "base_url"}
    ):
        raise ValueError(
            "TravelPlanner requires independent benchmark.postprocess.llm model_name, api_key and optional base_url"
        )
    if llm.get("base_url"):
        _url(llm["base_url"], "benchmark.postprocess.llm.base_url")
    postprocess.setdefault("timeout", 120)
    postprocess.setdefault("max_retries", 2)
    for key, value in (
        ("postprocess.timeout", postprocess["timeout"]),
        ("scoring_timeout", benchmark.config.get("scoring_timeout", 120)),
    ):
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError(f"TravelPlanner {key} must be positive and finite")
    if type(postprocess["max_retries"]) is not int or postprocess["max_retries"] < 0:
        raise ValueError(
            "TravelPlanner postprocess.max_retries must be a nonnegative integer"
        )
    database = benchmark.config.get("database_path")
    if database is None:
        from huggingface_hub import snapshot_download

        database = (
            Path(
                snapshot_download(
                    DATABASE_SOURCE,
                    repo_type="space",
                    revision=DATABASE_REVISION,
                    allow_patterns=[
                        "database/" + name
                        for name in _travelplanner_grade.DATABASE_FILES
                    ],
                )
            )
            / "database"
        )
        source = {"source": DATABASE_SOURCE, "revision": DATABASE_REVISION}
    else:
        database = Path(database).expanduser().resolve()
        source = {"source": "local", "path": str(database)}
    checksum = _travelplanner_grade.check_database(database)
    benchmark._database_checksum = checksum
    benchmark.metadata = MappingProxyType(
        {
            **benchmark.metadata,
            "database": {**source, "checksum": checksum},
            "postprocess_model": llm["model_name"],
            "postprocess_extraction": "native-fenced-or-complete-json",
            "postprocess_profile": "official"
            if llm["model_name"] == _travelplanner_postprocess.MODEL
            else "custom-model",
        }
    )
    secrets = sorted(
        set(_secrets(postprocess) + _secrets(os.environ)), key=len, reverse=True
    )

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("TravelPlanner requires a loaded benchmark Task")
        # Sole-planning supplies all reference information in the native prompt.
        # Hidden constraints and annotated plans never enter the task workspace.
        result = await supplied_context._run_prompt(target, task.question)
        result = replace(
            result,
            messages=tuple(
                m
                for m in result.messages
                if m.get("type")
                not in ("TravelPlannerVerifier", "TravelPlannerConversion")
            ),
        )
        if result.status == "finished":
            phase = "conversion"
            try:
                plan, usage = await asyncio.wait_for(
                    _travelplanner_postprocess.convert(result.output, postprocess),
                    timeout=postprocess["timeout"],
                )
                result = replace(
                    result, usage={**result.usage, "benchmark_postprocess": usage}
                )
                phase = "scoring"
                metrics = await _evaluate(
                    benchmark._rows_by_id[task.task_id],
                    plan,
                    database,
                    checksum,
                    benchmark.config.get("scoring_timeout", 120),
                )
                result = replace(
                    result,
                    messages=(
                        *result.messages,
                        {
                            "type": "TravelPlannerVerifier",
                            "task_id": task.task_id,
                            "task_checksum": task.metadata["task_checksum"],
                            "snapshot_id": target.snapshot.id,
                            "scorer_revision": REVISION,
                            "database_checksum": checksum,
                            "postprocess_model": llm["model_name"],
                            "plan": plan,
                            "metrics": metrics,
                        },
                    ),
                )
            except Exception as exc:
                error_kind = type(exc).__name__
                # A completed converter call still costs tokens when its output
                # violates the native format. Keep that evidence on failures.
                if isinstance(exc, _travelplanner_postprocess._ConversionError):
                    error_kind = "invalid converter output"
                    result = replace(
                        result,
                        usage={**result.usage, "benchmark_postprocess": exc.usage},
                        messages=(
                            *result.messages,
                            {
                                "type": "TravelPlannerConversion",
                                "task_id": task.task_id,
                                "status": "error",
                                "model": llm["model_name"],
                                "output": exc.reply,
                                "error": str(exc),
                            },
                        ),
                    )
                result = replace(
                    result,
                    status="error",
                    error_message=f"TravelPlanner {phase} failed ({error_kind})",
                )
        return replace(
            result,
            output=_redact(result.output, secrets),
            messages=_redact(result.messages, secrets),
            usage=_redact(result.usage, secrets),
            error_message=_redact(result.error_message, secrets),
        )

    agent._env_runner = run


async def _evaluate(row, plan, database, checksum, timeout):
    request = {
        "row": row,
        "plan": plan,
        "database": str(database),
        "database_checksum": checksum,
    }
    # Native scorer has no network work. Keep all target, Worker and converter
    # authentication, candidate code and .env files out of this process.
    names = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TMP", "TEMP"}
    env = {k: v for k, v in os.environ.items() if k.upper() in names}
    with TemporaryDirectory(prefix="flashrsi-travelplanner-grade-") as temporary:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "flashrsi.integrations._travelplanner_grade",
            cwd=temporary,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(json.dumps(request).encode()), timeout=timeout
            )
            if process.returncode:
                raise RuntimeError("Official TravelPlanner constraints failed")
            return json.loads(stdout)
        finally:
            await _stop(process)
