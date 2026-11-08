"""Plan, check, run, or resume the fixed TB2.1 30/0/59 DeepSeek experiment."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import tomllib
import urllib.request
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Execute the current checkout, including its concurrency/timeout support.
from flashrsi._launch import _load, _plan, _validate_host, _validate_roles  # noqa: E402
from flashrsi._progress import MODES  # noqa: E402
from flashrsi._records import _config_record, _redact, _secrets  # noqa: E402
from flashrsi.algorithms import RRSI  # noqa: E402
from flashrsi.benchmarks.terminal_bench import TerminalBench  # noqa: E402

REVISION = "7131e4375048a0e408a8fb404b5f499d726b695b"
COUNTS = {"train": 30, "dev": 0, "test": 59}
DEFAULT_CONFIG = ROOT / "examples/terminal_bench21_rrsi_deepseek.yaml"


def experiment_plan(config):
    """Use the real loader; reject source or protocol drift before any rollout."""
    harbor_version = version("harbor")
    if harbor_version != "0.23.0":
        raise ValueError(
            f"This experiment requires harbor==0.23.0, got {harbor_version}"
        )
    from flashrsi.integrations.harbor import _agent_timeout

    runtime_cache = None
    if cache_dir := config.agent.get("harbor", {}).get("agent_runtime_cache_dir"):
        from flashrsi.integrations._container_payload import runtime_cache_metadata

        runtime_cache = runtime_cache_metadata(cache_dir)

    benchmark = config.benchmark
    if (
        benchmark.name != "terminal_bench"
        or benchmark.get("version") != "2.1"
        or dict(benchmark.get("split_counts", {})) != COUNTS
        or benchmark.get("seed") != 42
        or benchmark.get("evolution_data_path") is not None
        or config.algorithm.name != "rrsi"
    ):
        raise ValueError("This launcher requires TB2.1 + RRSI, seed 42, and 30/0/59")
    concurrency = config.agent.get("concurrency", 1)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("agent.concurrency must be a positive integer")
    data = Path(benchmark.data_path).resolve()
    revision = subprocess.check_output(
        ["git", "-C", str(data.parent), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != REVISION:
        raise ValueError(f"Expected TB2.1 commit {REVISION}, got {revision}")
    changes = subprocess.check_output(
        ["git", "-C", str(data.parent), "status", "--porcelain", "--", data.name],
        text=True,
    )
    if changes.strip():
        raise ValueError(
            "TB2.1 task files have local changes; use the pinned clean dataset"
        )
    if len(list(data.glob("*/task.toml"))) != 89:
        raise ValueError("Expected exactly 89 native TB2.1 tasks")

    splits = TerminalBench(benchmark).load()
    timeouts, checksums, verifier_timeouts, prepared_verifiers = {}, {}, {}, {}
    for role, tasks in splits.items():
        if len(tasks) != COUNTS[role]:
            raise ValueError(f"Unexpected {role} task count")
        for task in tasks:
            native = tomllib.loads((Path(task.env) / "task.toml").read_text())
            timeouts[task.task_id] = _agent_timeout(
                config.agent, native.get("agent", {}).get("timeout_sec")
            )
            verifier_timeouts[task.task_id] = native.get("verifier", {}).get(
                "timeout_sec"
            )
            checksums[task.task_id] = task.metadata["task_checksum"]
            if manifest := config.agent.harbor.get("verifier_dependencies_manifest"):
                from harbor.models.task.task import Task as HarborTask

                from flashrsi.integrations._verifier_dependencies import prepared_entry

                entry = prepared_entry(
                    manifest, task.task_id, HarborTask(Path(task.env))
                )
                if entry:
                    prepared_verifiers[task.task_id] = entry
    params = {**RRSI._defaults, **config.algorithm}
    rounds, candidates, repeats = params["T"], params["m"], params["k"]
    for key, value in (("T", rounds), ("m", candidates), ("k", repeats)):
        if type(value) is not int or value < 1:
            raise ValueError(f"algorithm.{key} must be a positive integer")
    baseline = (
        30 * repeats * (params["calibration_evals"] if params["delta"] is None else 1)
    )
    measurements = rounds * candidates * 30 * repeats
    smokes = (
        rounds
        * candidates
        * min(
            params["smoke_n"], 30, params["n_fail_traces"] + params["n_success_traces"]
        )
    )
    return {
        "protocol": "tb21-flashrsi-rrsi-sha256-seed42-30-0-59-v1",
        "dataset_commit": revision,
        "harbor_version": harbor_version,
        "data_path": str(data),
        "split_counts": COUNTS,
        "splits": {
            role: [task.task_id for task in tasks] for role, tasks in splits.items()
        },
        "task_checksums": checksums,
        "prepared_verifiers": prepared_verifiers,
        "prepared_agent_runtime": runtime_cache,
        "task_concurrency": concurrency,
        "measurement_scheduling": "one shared queue across all repeats",
        "agent_timeout_sec": timeouts,
        "verifier_timeout_sec": verifier_timeouts,
        "agent_max_turns": config.agent.get("max_turns", 10),
        "worker_timeout_sec": config.algorithm.worker.get("timeout", 300),
        "worker_max_turns": config.algorithm.worker.get("max_turns", 10),
        "models": {
            role: {
                key: values.llm.get(key)
                for key in ("model_name", "base_url", "max_tokens", "reasoning_effort")
            }
            for role, values in (
                ("target", config.agent),
                ("worker", config.algorithm.worker),
            )
        },
        "rrsi": _config_record(
            {
                key: value
                for key, value in params.items()
                if key not in {"worker", "name"}
            }
        ),
        "target_task_execution_budget": {
            "baseline": baseline,
            "candidate_measurements_max": measurements,
            "candidate_smokes_max": smokes,
            "final_test": 59,
            "total_max": baseline + measurements + smokes + 59,
            "note": "Excludes Worker calls; rejected proposals can reduce execution count. Test runs once per task.",
        },
        "output_dir": config.run.output_dir,
        "dependencies": _plan(config),
    }


def check_models(config):
    """GET model lists only, without inference or exposing API keys."""
    for role, values in (("target", config.agent), ("worker", config.algorithm.worker)):
        llm = values.llm
        request = urllib.request.Request(
            llm.base_url.rstrip("/") + "/models",
            headers={"Authorization": "Bearer " + llm.api_key},
        )
        # These are explicit local endpoints; do not send them via a shell proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=10) as response:
            models = {row["id"] for row in json.load(response).get("data", [])}
        if llm.model_name not in models:
            raise ValueError(f"{role}: configured model is absent from /v1/models")
        print(f"{role}: model endpoint reachable (host check only)", flush=True)


async def execute(config, resume=None, *, check_only=False):
    from flashrsi.runner import _prepare, _run

    algorithm = await _prepare(config)
    if resume is not None:
        resume.bind(algorithm)
        print(
            f"{resume.metadata()['mode']} resume validated: {resume.metadata()['reused_baseline_trials']} "
            f"saved trials; target snapshot={resume.target.id}",
            flush=True,
        )
    if not check_only:
        return await _run(config, algorithm=algorithm)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=("plan", "check", "run"), nargs="?", default="plan"
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--resume-from",
        type=Path,
        metavar="RUN_DIR",
        help="Resume an interrupted RRSI run into a new output directory",
    )
    parser.add_argument(
        "--resume-mode",
        choices=("auto", "baseline", "round", "trial"),
        default="auto",
        help="auto uses trial checkpoints or the latest complete legacy round",
    )
    parser.add_argument(
        "--reselect-round-one",
        action="store_true",
        help="With --resume-from, replay completed round-one selection under the configured cost policy and continue at round two",
    )
    parser.add_argument(
        "--rounds", type=int, help="Override RRSI T; does not change the partition"
    )
    parser.add_argument(
        "--concurrency", type=int, help="Override Target task concurrency"
    )
    parser.add_argument(
        "--min-timeout", type=float, help="Agent timeout floor in seconds"
    )
    parser.add_argument(
        "--progress",
        choices=MODES,
        help="auto: live bars in a terminal, append-only progress in log files",
    )
    args = parser.parse_args(argv)
    config = None
    try:
        config = _load(args.config)
        if args.progress is not None:
            config.run.progress = args.progress
        if args.rounds is not None:
            config.algorithm.T = args.rounds
        if args.concurrency is not None:
            config.agent.concurrency = args.concurrency
        if args.min_timeout is not None:
            config.agent.harbor.agent_min_timeout = args.min_timeout
        _validate_roles(config)
        _validate_host(config)
        plan = experiment_plan(config)
        resume = None
        if args.resume_mode != "auto" and args.resume_from is None:
            raise ValueError("--resume-mode requires --resume-from")
        if args.reselect_round_one and args.resume_from is None:
            raise ValueError("--reselect-round-one requires --resume-from")
        if args.resume_from is not None:
            from flashrsi.algorithms.rrsi._resume import BaselineResume
            from flashrsi.algorithms.rrsi._round_resume import RoundOneResume

            if args.reselect_round_one:
                if args.resume_mode != "auto":
                    raise ValueError(
                        "--reselect-round-one cannot be combined with --resume-mode"
                    )
                resume = RoundOneResume(args.resume_from, config)
            elif args.resume_mode == "baseline" or (
                args.resume_mode == "auto"
                and not (args.resume_from / "checkpoints/identity.json").exists()
                and not any(
                    (args.resume_from / "trajectories").glob("*-round-summary.json")
                )
            ):
                resume = BaselineResume(args.resume_from, config)
            else:
                from flashrsi.algorithms.rrsi._checkpoint import CheckpointResume

                resume = CheckpointResume(
                    args.resume_from,
                    config,
                    mode="trial" if args.resume_mode == "auto" else args.resume_mode,
                )
            plan["resume"] = resume.metadata()
            budget = plan["target_task_execution_budget"]
            budget["reused_baseline"] = plan["resume"]["reused_baseline_trials"]
            budget["reused_candidates"] = plan["resume"].get(
                "reused_candidate_trials", 0
            )
            budget["new_executions_max"] = (
                budget["total_max"]
                - budget["reused_baseline"]
                - budget["reused_candidates"]
            )
            if args.reselect_round_one:
                budget["new_executions_max"] -= config.algorithm.m * min(
                    config.algorithm.smoke_n,
                    30,
                    config.algorithm.get(
                        "n_fail_traces", RRSI._defaults["n_fail_traces"]
                    )
                    + config.algorithm.get(
                        "n_success_traces", RRSI._defaults["n_success_traces"]
                    ),
                )
        # Check mode must validate the same serialization that run will persist.
        serialized_plan = json.dumps(plan, ensure_ascii=False, indent=2)
        if args.mode == "plan":
            print(serialized_plan)
            return 0

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        check_models(config)
        if args.mode == "check":
            asyncio.run(execute(config, resume, check_only=True))
            print(
                "Ready: train=30, dev=0, test=59. No model inference or task execution."
            )
            print(
                "Container model access, tool calls, and usage accounting require a live trial."
            )
            return 0

        # Freeze the exact task versions and run settings before starting any model.
        plans = Path(config.run.output_dir) / "plans"
        plans.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        plan_path = plans / f"{stamp}.json"
        plan_path.write_text(serialized_plan + "\n")
        print(f"Experiment plan: {plan_path}", flush=True)
        run_dir, result = asyncio.run(execute(config, resume))
        print(f"Completed: {run_dir}")
        print(f"Heldout reward: {result['test_score']}")
        return 0
    except Exception as exc:
        secrets = _secrets(os.environ) + (
            _secrets(config) if config is not None else []
        )
        print(_redact(str(exc), secrets), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
