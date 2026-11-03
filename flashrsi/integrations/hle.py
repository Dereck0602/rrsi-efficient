"""Isolated HLE execution followed by the fixed official structured judge."""

import copy
import importlib.util
import math
from dataclasses import replace

from flashrsi._records import _redact, _secrets
from flashrsi.integrations import _hle_judge, supplied_context


def bind(agent, benchmark):
    supplied_context._check(agent)
    if importlib.util.find_spec("openai") is None:
        raise ValueError("Install flashrsi-lite[all] for HLE judging")
    config = copy.deepcopy(dict(benchmark.config.get("judge", {})))
    llm = config.get("llm", {})
    if not llm.get("model_name") or not llm.get("api_key"):
        raise ValueError(
            "HLE requires independent benchmark.judge.llm model_name and api_key"
        )
    timeout = config.setdefault("timeout", 300)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("benchmark.judge.timeout must be positive and finite")
    secrets = sorted(set(_secrets(config)), key=len, reverse=True)

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("HLE requires a loaded benchmark Task")
        row = benchmark._rows_by_id[task.task_id]
        result = await supplied_context._run_prompt(target, task.question)
        result = replace(
            result,
            messages=tuple(
                m for m in result.messages if m.get("type") != "HLEVerifier"
            ),
        )
        if result.status != "finished":
            return result
        try:
            verdict, usage = await _hle_judge.judge(
                row["question"], row["answer"], result.output, config
            )
            result = replace(result, usage={**result.usage, "benchmark_judge": usage})
            # Parsed schema enforces these fields; keep helper replacements from
            # silently accepting truthy strings or a missing verdict as success.
            if verdict["correct"] not in ("yes", "no") or verdict["strict"] is not True:
                raise ValueError("Invalid HLE judge verdict")
            confidence = verdict["confidence"]
            if type(confidence) is not int or not 0 <= confidence <= 100:
                raise ValueError("Invalid HLE judge confidence")
            result = replace(
                result,
                messages=(
                    *result.messages,
                    {
                        "type": "HLEVerifier",
                        "task_id": task.task_id,
                        "task_checksum": task.metadata["task_checksum"],
                        "snapshot_id": target.snapshot.id,
                        "judge_revision": _hle_judge.REVISION,
                        "judge_model": config["llm"]["model_name"],
                        "correct": verdict["correct"] == "yes",
                        "confidence": confidence,
                    },
                ),
            )
        except Exception as exc:
            # Exceptions from a remote judge may contain reference text. Return
            # the failure kind only, never its hidden prompt or extracted answer.
            result = replace(
                result,
                status="error",
                error_message=f"HLE judge failed ({type(exc).__name__})",
            )
        return replace(
            result,
            output=_redact(result.output, secrets),
            messages=_redact(result.messages, secrets),
            usage=_redact(result.usage, secrets),
            error_message=_redact(result.error_message, secrets),
        )

    agent._env_runner = run
