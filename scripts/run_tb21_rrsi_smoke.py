"""Run one real RRSI round on two training tasks, without touching held-out tasks."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from run_tb21_rrsi import DEFAULT_CONFIG, check_models, experiment_plan  # noqa: E402

from flashrsi._launch import _load, _validate_host, _validate_roles  # noqa: E402
from flashrsi._records import _redact, _secrets  # noqa: E402
from flashrsi.runner import _prepare, _run  # noqa: E402

DEFAULT_TASKS = (
    "terminal-bench/openssl-selfsigned-cert",
    "terminal-bench/cancel-async-tasks",
)


def restrict_to_smoke_tasks(benchmark, task_ids):
    """Explicitly reuse the two train tasks for a final integration check."""
    original = benchmark.load()
    train = {task.task_id: task for task in original["train"]}
    if (
        len(task_ids) != 2
        or len(set(task_ids)) != 2
        or not set(task_ids) <= train.keys()
    ):
        raise ValueError(
            "Smoke test requires exactly two distinct original train tasks"
        )
    selected = tuple(train[task_id] for task_id in task_ids)
    benchmark._splits = MappingProxyType(
        {"train": selected, "dev": (), "test": selected}
    )
    benchmark._tasks_by_id = {task.task_id: task for task in selected}
    benchmark.metadata = MappingProxyType(
        {
            **benchmark.metadata,
            "integration_smoke_test": True,
            "smoke_task_ids": list(task_ids),
            "final_test_reuses_train_tasks": True,
            "heldout_evaluated": False,
        }
    )


async def execute(config, task_ids, plan):
    algorithm = await _prepare(config)
    restrict_to_smoke_tasks(algorithm.benchmark, task_ids)
    algorithm._check()
    try:
        run_dir, result = await _run(config, algorithm=algorithm)
    finally:
        if algorithm._run_dir is not None:
            (algorithm._run_dir / "smoke-plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2) + "\n"
            )
    records = list((run_dir / "trajectories").glob("*.json"))
    coverage = {
        label: sum(path.name.endswith(f"-{label}.json") for path in records)
        for label in (
            "baseline",
            "calibration",
            "rrsi-context",
            "critic",
            "smoke-gate",
            "candidate",
            "measurement",
            "round-summary",
            "test",
            "worker",
        )
    }
    report = {
        "status": result["status"],
        "run_dir": str(run_dir),
        "task_ids": list(task_ids),
        "heldout_evaluated": False,
        "integration_check_reward": result["test_score"],
        "record_counts": coverage,
        "candidate_measurement_exercised": coverage["candidate"] > 0,
        "round_completed": coverage["round-summary"] == 1,
        "final_evaluation_saved": coverage["test"] == 1,
    }
    (run_dir / "smoke-result.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return run_dir


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "run"), nargs="?", default="plan")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--tasks", nargs=2, default=DEFAULT_TASKS)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "runs/tb21-rrsi-deepseek-smoke"
    )
    args = parser.parse_args()
    config = None
    try:
        config = _load(args.config)
        _validate_roles(config)
        _validate_host(config)
        full_plan = experiment_plan(config)
        tasks = tuple(args.tasks)
        if len(set(tasks)) != 2 or not set(tasks) <= set(full_plan["splits"]["train"]):
            raise ValueError("Choose two distinct tasks from the original train split")
        if not set(tasks) <= full_plan["prepared_verifiers"].keys():
            raise ValueError(
                "Both smoke tasks must have prepared verifier dependencies"
            )
        config.run.output_dir = str(args.output_dir.resolve())
        config.run.progress = "log"
        config.algorithm.T = 1
        config.benchmark.smoke_task_ids = tasks
        plan = {
            "mode": "two-task-integration-smoke",
            "dataset_commit": full_plan["dataset_commit"],
            "train_task_ids": tasks,
            "final_check_task_ids": tasks,
            "heldout_evaluated": False,
            "rounds": 1,
            "repeats": config.algorithm.k,
            "candidates": config.algorithm.m,
            "task_concurrency": config.agent.concurrency,
            "digest_concurrency": config.algorithm.digest_concurrency,
            "target_max_tokens": config.agent.llm.max_tokens,
            "worker_max_tokens": config.algorithm.worker.llm.max_tokens,
            "invalid_missing_frac": config.algorithm.invalid_missing_frac,
            "task_order": config.algorithm.task_order,
            "agent_timeout_sec": {t: full_plan["agent_timeout_sec"][t] for t in tasks},
            "prepared_verifiers": {
                t: full_plan["prepared_verifiers"][t] for t in tasks
            },
            "models": full_plan["models"],
            "output_dir": config.run.output_dir,
        }
        if args.mode == "plan":
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
        )
        check_models(config)
        plans = args.output_dir / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        plan_path = plans / f"{stamp}.json"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        print(f"Smoke plan: {plan_path}", flush=True)
        print(
            f"Two-task smoke: T=1 k={config.algorithm.k} m={config.algorithm.m}; "
            f"concurrency={config.agent.concurrency}; tasks={','.join(tasks)}; "
            "final check reuses these two tasks; no held-out evaluation",
            flush=True,
        )
        asyncio.run(execute(config, tasks, plan))
        return 0
    except Exception:
        secrets = _secrets(os.environ) + (
            _secrets(config) if config is not None else []
        )
        print(_redact(traceback.format_exc(), secrets), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
