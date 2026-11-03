"""Bind both native shell harnesses to web tools and an independent host judge."""

import copy
import importlib.util
import math
from dataclasses import replace

from flashrsi._records import _redact, _secrets
from flashrsi.benchmarks.browsecomp._official import REVISION, decrypt
from flashrsi.integrations import _browsecomp_judge, _web_tools


def bind(agent, benchmark):
    if importlib.util.find_spec("openai") is None:
        raise ValueError("Install flashrsi-lite[all] for BrowseComp judging")
    retrieval = _web_tools.settings(agent, benchmark.config, "BrowseComp")
    judge = copy.deepcopy(dict(benchmark.config.get("judge", {})))
    if not judge.get("llm", {}).get("model_name") or not judge.get("llm", {}).get(
        "api_key"
    ):
        raise ValueError(
            "BrowseComp requires independent benchmark.judge.llm model_name and api_key"
        )
    judge.setdefault("timeout", 300)
    for config in (retrieval, judge):
        timeout = config["timeout"]
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("BrowseComp timeouts must be positive and finite")
    secrets = sorted(set(_secrets(retrieval) + _secrets(judge)), key=len, reverse=True)

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("BrowseComp requires a loaded benchmark Task")
        row = benchmark._rows_by_id[task.task_id]
        result = await _web_tools.run(target, task, retrieval, "BrowseComp")
        if result.status == "finished":
            try:
                correct, usage = await _browsecomp_judge.judge(
                    decrypt(row["problem"], row["canary"]),
                    decrypt(row["answer"], row["canary"]),
                    result.output,
                    judge,
                )
                if type(correct) is not bool:
                    raise ValueError("Invalid BrowseComp verdict")
                result = replace(
                    result,
                    usage={**result.usage, "benchmark_judge": usage},
                    messages=(
                        *result.messages,
                        {
                            "type": "BrowseCompVerifier",
                            "task_id": task.task_id,
                            "task_checksum": task.metadata["task_checksum"],
                            "snapshot_id": target.snapshot.id,
                            "judge_revision": REVISION,
                            "judge_model": judge["llm"]["model_name"],
                            "correct": correct,
                        },
                    ),
                )
            except Exception as exc:
                result = replace(
                    result,
                    status="error",
                    error_message=f"BrowseComp judge failed ({type(exc).__name__})",
                )
        return replace(
            result,
            output=_redact(result.output, secrets),
            messages=_redact(result.messages, secrets),
            usage=_redact(result.usage, secrets),
            error_message=_redact(result.error_message, secrets),
        )

    agent._env_runner = run
