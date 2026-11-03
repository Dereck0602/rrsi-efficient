"""Independent native Agent samples followed by host-only mathematical judging."""

import copy
import importlib.util
import math
from dataclasses import replace

from flashrsi._records import _redact, _secrets
from flashrsi.benchmarks.livemathbench import REVISION
from flashrsi.core import Trajectory
from flashrsi.integrations import _livemathbench_judge, supplied_context


def bind(agent, benchmark):
    supplied_context._check(agent)
    if importlib.util.find_spec("openai") is None:
        raise ValueError("Install flashrsi-lite[all] for LiveMathBench judging")
    judge = copy.deepcopy(dict(benchmark.config.get("judge", {})))
    llm = judge.setdefault("llm", {})
    llm.setdefault("model_name", "Qwen/Qwen2.5-72B-Instruct")
    if not llm.get("model_name") or not llm.get("api_key"):
        raise ValueError(
            "LiveMathBench requires independent benchmark.judge.llm.api_key and model_name"
        )
    if set(llm) & {"tools", "tool_choice", "messages", "model", "stream", "functions"}:
        raise ValueError(
            "LiveMathBench judge.llm must not override messages, tools, model or streaming"
        )
    judge.setdefault("timeout", 300)
    timeout = judge["timeout"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("LiveMathBench judge.timeout must be positive and finite")
    if type(judge.get("max_retries", 1)) is not int or judge.get("max_retries", 1) < 0:
        raise ValueError(
            "LiveMathBench judge.max_retries must be a nonnegative integer"
        )
    secrets = _secrets(judge)

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("LiveMathBench requires a loaded benchmark Task")
        row = benchmark._rows_by_id[task.task_id]
        samples, labels, calls, judge_usage = [], [], [], []
        error = None
        for index in range(benchmark._n):
            result = await supplied_context._run_prompt(target, task.question)
            correct = 0
            # A failed target sample counts as incorrect without shrinking n.
            # A judge infrastructure failure invalidates the whole task instead.
            if result.status == "finished":
                try:
                    correct, usage = await _livemathbench_judge.judge(
                        row["question"],
                        row["answer"],
                        result.output,
                        task.metadata["language"],
                        judge,
                    )
                    if type(correct) is not int or correct not in (0, 1):
                        raise ValueError("Invalid LiveMathBench judge label")
                    judge_usage.append(usage)
                except Exception as exc:
                    error = f"LiveMathBench judge failed ({type(exc).__name__})"
            samples.append(
                {
                    "type": "LiveMathBenchSample",
                    "sample_index": index,
                    "status": result.status,
                    "output": result.output,
                    "messages": [
                        dict(m)
                        for m in result.messages
                        if m.get("type")
                        not in ("LiveMathBenchSample", "LiveMathBenchVerifier")
                    ],
                    "usage": dict(result.usage),
                    "error_message": result.error_message,
                    "correct": correct if error is None else None,
                }
            )
            # Keep the existing usage accounting shape: MiniAgent has model_calls;
            # Claude Code reports one session-level usage record per sample.
            calls.extend(
                result.usage.get("model_calls")
                or [
                    {
                        "call_id": result.usage.get("session_id"),
                        "usage": dict(result.usage),
                    }
                ]
            )
            labels.append(correct)
            if error:
                break
        messages = tuple(samples)
        if error is None:
            messages += (
                {
                    "type": "LiveMathBenchVerifier",
                    "task_id": task.task_id,
                    "task_checksum": task.metadata["task_checksum"],
                    "snapshot_id": target.snapshot.id,
                    "judge_revision": REVISION,
                    "judge_model": llm["model_name"],
                    "n": benchmark._n,
                    "k": benchmark._ks,
                    "labels": labels,
                },
            )
        result = Trajectory(
            status="error" if error else "finished",
            output="\n\n".join(
                f"Sample {i + 1}:\n{s['output']}" for i, s in enumerate(samples)
            ),
            messages=messages,
            error_message=error,
            usage={"model_calls": calls, "benchmark_judge": {"samples": judge_usage}},
        )
        return replace(
            result,
            output=_redact(result.output, secrets),
            messages=_redact(result.messages, secrets),
            usage=_redact(result.usage, secrets),
            error_message=_redact(result.error_message, secrets),
        )

    agent._env_runner = run
