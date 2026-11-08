"""RRSI on the framework's Worker, Benchmark and Snapshot.

Independent mechanism port reviewed against google-research/rrsi at
be50316e1db05914068a973f322770ef08ed7ba1; see docs/rrsi.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import shutil
from collections.abc import Mapping
from contextlib import ExitStack
from difflib import unified_diff
from functools import partial
from pathlib import Path
from statistics import mean, pstdev, stdev
from tempfile import TemporaryDirectory

from flashrsi._progress import Progress, progress_note, progress_scope
from flashrsi._records import _trajectory_record
from flashrsi.algorithms import _replies
from flashrsi.algorithms.rrsi import _prompts as prompts
from flashrsi.algorithms.rrsi._candidate_queue import (
    candidate_schedule,
    execute_candidates,
)
from flashrsi.algorithms.rrsi._cost import estimate_cost, selection_cost
from flashrsi.algorithms.rrsi._scheduling import execution_seconds, task_schedule
from flashrsi.algorithms.rrsi._worker_budget import (
    WorkerOutputBudgetExhausted,
    retry_worker_phase,
)
from flashrsi.core import Algorithm, Snapshot
from flashrsi.core.snapshot import discard_runtime_files

_COMPONENTS = frozenset(
    {
        "prompt",
        "control_flow",
        "config",
        "output_plumbing",
        "context_mgmt",
        "client_tool",
        "skill",
        "memory",
        "subagent",
    }
)

_GENERIC_SIGNALS = {
    "memory": [r"\bMemory\(", r"\.remember\(", r"\.recall\(", r"_STATE_DIR"],
    "skill": [
        r"skills/",
        r"SkillRegistry",
        r"skill_use",
        r"skill_catalog",
        r"upload_skills",
    ],
    "client_tool": [r"ToolRegistry", r"register_tool", r"tool_spec", r"CLIENT_TOOLS"],
    "subagent": [r"\bsubcall\(", r"sub_agent", r"subagent"],
}

_CREDENTIAL_PATTERN = (
    r"AIza[0-9A-Za-z_-]{35}|sk-[A-Za-z0-9]{20,}|api_key\s*=\s*[\"'][^\"']{8,}"
)

_STRUCTURAL = {"client_tool", "skill", "memory", "subagent"}

_ANALYSIS_MAX_TURNS = 30
_DIGEST_MAX_CHARS = 6000
_PROTOCOL_RETRIES = 3
_LOGGER = logging.getLogger(__name__)


class _InvalidMeasurement(ValueError):
    """A completed evaluation cannot support RRSI's score/cost comparison."""


class RRSI(Algorithm):
    """Anneal edit budgets and explore stalled components from trajectory evidence.

    Keep candidates under a historical score floor and token-cost rule.
    """

    name = "rrsi"
    description = (
        "Regularized harness evolution with history, critic and cost selection."
    )
    evolve_scope = {"*"}
    _defaults = {
        "T": 20,
        "k": 2,
        "m": 2,
        "b_min": 1,
        "b_max": 4,
        "w": 3,
        "m_draft": 1,
        "delta": None,
        "delta_z": 2.0,
        "beta0": 0.10,
        "beta1": 40.0,
        "w_s": 100.0,
        "w_c": 15.0,
        "w_n": 0.5,
        "n_prune": 4,
        "repair_rounds": 5,
        "worker_output_retries": 1,
        "invalid_missing_frac": 0.15,
        "seed": 7,
        "n_fail_traces": 22,
        "n_success_traces": 6,
        "calibration_evals": 1,
        "smoke_n": 2,
        "digest_concurrency": 1,
        "task_order": "fifo",
        "shared_candidate_queue": False,
        "cost_policy": "complete",
        "cost_estimate_max_frac": 0.25,
        "component_signals": {},
        "weight_metric": None,
        "guards": {},
    }
    _ranges = {
        "T": (int, 1),
        "k": (int, 1),
        "m": (int, 1),
        "b_min": (int, 1),
        "b_max": (int, 1),
        "w": (int, 1),
        "m_draft": (int, 0),
        "delta": (float | None, 0),
        "delta_z": (float, 0),
        "beta0": (float, 0),
        "beta1": (float, 0),
        "w_s": (float, 0),
        "w_c": (float, 0),
        "w_n": (float, 0),
        "n_prune": (int, 1),
        "repair_rounds": (int, 0),
        "worker_output_retries": (int, 0),
        "invalid_missing_frac": (float, 0),
        "seed": int,
        "n_fail_traces": (int, 0),
        "n_success_traces": (int, 0),
        "calibration_evals": (int, 1),
        "smoke_n": (int, 0),
        "digest_concurrency": (int, 1),
        "task_order": ("fifo", "longest_first"),
        "shared_candidate_queue": bool,
        "cost_policy": ("complete", "estimate"),
        "cost_estimate_max_frac": (float, 0),
    }

    async def evolve(self) -> Snapshot:
        with ExitStack() as resources:
            resources.enter_context(progress_scope("RRSI"))
            resources.callback(setattr, self, "_run_dir", self._run_dir)
            if self._run_dir is None:
                self._run_dir = Path(resources.enter_context(TemporaryDirectory()))
                for name in ("snapshots", "trajectories", "working"):
                    (self._run_dir / name).mkdir()

            current = seed = self.target.snapshot
            self._task_duration_estimates = {}
            self._pending_task_durations = {}
            start_step = 0
            round_resume = getattr(self, "_round_resume", None)
            if round_resume is not None:
                state = round_resume.restore(self)
                current, incumbent = state["current"], state["incumbent"]
                delta, baseline_score = state["delta"], state["baseline_score"]
                best_score, best_snapshot_id = (
                    state["best_score"],
                    state["best_snapshot_id"],
                )
                scores, history, analysis = (
                    state["scores"],
                    state["history"],
                    state["analysis"],
                )
                start_step = state["start_step"]
                progress_note(
                    f"Restored/reselected round=1; current={incumbent['score']:.2%}; "
                    f"best={best_score:.2%}; snapshot={current.id}; continuing at round=2"
                )
                for decision in round_resume.decisions:
                    _LOGGER.info(
                        "RRSI replay candidate=%d decision=%s",
                        decision["variant"] + 1,
                        json.dumps(decision),
                    )
            else:
                resume = getattr(self, "_baseline_resume", None)
                if resume is not None:
                    resume.record(
                        self._run_dir,
                        {"target": self.target.config, "worker": self.worker.config},
                    )
                    progress_note(
                        f"Restoring {sum(len(batch) for batch in resume.batches)} baseline trials; "
                        "no baseline inference; continuing at round=1"
                    )
                incumbent = await self._measure(current, "baseline")
                delta = self.config.delta
                if delta is None:
                    evaluations = [incumbent]
                    for _ in range(self.config.calibration_evals - 1):
                        evaluations.append(
                            await self._measure(current, "calibration-eval")
                        )
                    calibration = _calibrate(
                        evaluations, self.config.delta_z, self.config.seed
                    )
                    delta = calibration["delta"]
                else:
                    calibration = {"delta": delta, "source": "configured", "n_evals": 0}
                self._record("calibration", calibration)
                baseline_score = incumbent["score"]
                best_score = incumbent["score"]
                best_snapshot_id = current.id
                scores, history = [best_score], []
                analysis = {"failure_modes": [], "success_habits": []}

            async with Progress(self.config.T - start_step, unit="rounds") as rounds:
                for step in range(start_step, self.config.T):
                    # Freeze one estimate per task for all sibling candidates.
                    # Round 1 uses baseline/calibration; later rounds use all
                    # completed candidate measurements from the previous round.
                    self._refresh_task_duration_estimates()
                    round_label = f"round={step + 1}/{self.config.T}"
                    rounds.start(round_label)
                    with progress_scope(round_label):
                        traces = _select_traces(
                            incumbent,
                            self.config.n_fail_traces,
                            self.config.n_success_traces,
                        )
                        progress_note("Worker: analyzing trajectories")
                        analysis_failure = None
                        try:
                            analysis = await retry_worker_phase(
                                self,
                                "analysis",
                                partial(
                                    self._analyze,
                                    current.id,
                                    traces,
                                    {
                                        key: analysis[key]
                                        for key in ("failure_modes", "success_habits")
                                    },
                                ),
                                step=step,
                            )
                        except WorkerOutputBudgetExhausted as error:
                            analysis_failure = str(error)
                        context = {
                            "step": step,
                            "incumbent_id": current.id,
                            "score": incumbent["score"],
                            "cost": incumbent["cost"],
                            "cost_accounting": incumbent["cost_accounting"],
                            "best_score": best_score,
                            "delta": delta,
                            "budget": _edit_budget(step, self.config),
                            "analysis": analysis,
                            "traces": {
                                task: trace["evidence"]
                                for task, trace in traces.items()
                            },
                            "history": history,
                            **_directives(history, scores, step, delta, self.config),
                        }
                        if analysis_failure:
                            context["skipped_reason"] = analysis_failure
                        self._record("rrsi-context", context)
                        # Every sibling reads this fixed, redacted record.
                        evidence = max(
                            (self._run_dir / "trajectories").glob("*-rrsi-context.json")
                        )
                        proposals = [
                            await self._propose_candidate(
                                current, variant, context, evidence
                            )
                            for variant in range(
                                0 if analysis_failure else self.config.m
                            )
                        ]
                        previous_id = current.id
                        current, incumbent = await self._select(
                            current, incumbent, proposals, context, history
                        )
                        if incumbent["score"] > best_score:
                            best_snapshot_id = current.id
                        best_score = max(best_score, incumbent["score"])
                        scores.append(incumbent["score"])
                        summary = {
                            "round": step + 1,
                            "rounds": self.config.T,
                            "current_score": incumbent["score"],
                            "best_score": best_score,
                            "baseline_score": baseline_score,
                            "gain_from_baseline": best_score - baseline_score,
                            "current_snapshot_id": current.id,
                            "best_snapshot_id": best_snapshot_id,
                            "accepted_candidate": current.id != previous_id,
                            "current_error_trials": incumbent["error_trials"],
                            "trials_total": len(incumbent["evidence"]),
                            "delta": delta,
                        }
                        if analysis_failure:
                            summary.update(status="skipped", reason=analysis_failure)
                        self._record("round-summary", summary)
                        _LOGGER.info(
                            "RRSI round=%d/%d complete | evolve current=%.2f%% "
                            "best=%.2f%% baseline=%.2f%% gain=%+.2fpp | "
                            "accepted=%s current_errors=%d/%d | "
                            "current_snapshot=%s best_snapshot=%s",
                            step + 1,
                            self.config.T,
                            incumbent["score"] * 100,
                            best_score * 100,
                            baseline_score * 100,
                            (best_score - baseline_score) * 100,
                            "yes" if summary["accepted_candidate"] else "no",
                            summary["current_error_trials"],
                            summary["trials_total"],
                            current.id[:12],
                            best_snapshot_id[:12],
                        )
                    rounds.finish(round_label)

            return self._promote(
                seed,
                current,
                score=incumbent["score"],
                cost=incumbent["cost"],
                cost_accounting=incumbent["cost_accounting"],
                best_score=best_score,
                delta=delta,
            )

    async def _propose_candidate(self, parent, variant, context, evidence):
        with progress_scope(f"candidate={variant + 1}/{self.config.m}"):
            progress_note("Worker: proposing harness edits")
            try:
                return await retry_worker_phase(
                    self,
                    "candidate",
                    partial(self._propose, parent, variant, context, evidence),
                    step=context["step"],
                    variant=variant,
                )
            except WorkerOutputBudgetExhausted as error:
                return None, [], "", str(error)

    async def _select(
        self, parent, incumbent, proposals, context, history, *, measurements=None
    ):
        accepted = {row["component"] for row in history if row["accepted"]}
        start = len(history)
        winner, winning_measurement = None, None
        winning_variant = None
        if measurements is None and self.config.shared_candidate_queue:
            measurements = await self._measure_candidates(proposals, context["step"])
        for variant, (snapshot, edits, changes, reason) in enumerate(proposals):
            decision = {
                "admissible": False,
                "reason": reason,
                "delta_S": None,
                "delta_C": None,
            }
            feedback = []
            if snapshot is not None:
                try:
                    with progress_scope(f"candidate={variant + 1}/{self.config.m}"):
                        if measurements is None:
                            measurement = await self._measure(snapshot, "candidate")
                        else:
                            measurement = measurements[variant]
                            if isinstance(measurement, _InvalidMeasurement):
                                raise measurement
                except _InvalidMeasurement as error:
                    decision["reason"] = str(error)
                else:
                    decision = _judge(
                        measurement,
                        incumbent,
                        context["best_score"],
                        context["delta"],
                        edits,
                        accepted,
                        self.config,
                    )
                    feedback = _attribution(
                        edits, incumbent, measurement, 1 / self.config.k
                    )
                    if decision["admissible"] and (
                        winner is None
                        or measurement["score"] > winning_measurement["score"]
                    ):
                        winner, winning_measurement = snapshot, measurement
                        winning_variant = variant
            for index, edit in enumerate(edits or [{"component": None, "paths": []}]):
                history.append(
                    {
                        **edit,
                        "t": context["step"],
                        "variant": variant,
                        "parent_id": parent.id,
                        "candidate_id": snapshot.id if snapshot is not None else None,
                        "diff": changes,
                        "attribution": feedback[index] if feedback else None,
                        **decision,
                    }
                )
        for row in history[start:]:
            selected = row["variant"] == winning_variant
            row.update(
                accepted=selected,
                outcome="ACCEPTED"
                if selected
                else "LOST"
                if row["admissible"]
                else "REJECTED",
            )
            self._record("edit-history", row)

        return (parent, incumbent) if winner is None else (winner, winning_measurement)

    async def _measure_candidates(self, proposals, step):
        candidates = tuple(
            (variant, snapshot)
            for variant, (snapshot, *_rest) in enumerate(proposals)
            if snapshot is not None
        )
        if not candidates:
            return {}
        tasks = tuple(self.benchmark.train)
        jobs, scheduling = candidate_schedule(
            candidates,
            tasks,
            self.config.k,
            getattr(self, "_task_duration_estimates", {}),
            self.config.task_order,
            step,
        )
        for _, snapshot in candidates:
            self._save_snapshot(snapshot)
        self._record(
            "candidate-queue",
            {
                "step": step,
                "concurrency": self.target.config.get("concurrency", 1),
                "policy": self.config.task_order,
                "jobs": [job.record() for job in jobs],
            },
        )
        progress_note(
            f"shared candidate queue: candidates={len(candidates)}; "
            f"executions={len(jobs)}; task order={self.config.task_order}; "
            f"historical timings={scheduling['known_tasks']}/{len(tasks)}"
        )
        trajectories = await execute_candidates(self.target, jobs)
        return self._summarize_candidates(candidates, tasks, trajectories, scheduling)

    def _summarize_candidates(self, candidates, tasks, trajectories, scheduling):
        measurements = {}
        # All executions finish before grading/selection. Preserve generation
        # order for records, cost estimates, history and equal-score tie breaks.
        for variant, snapshot in candidates:
            with progress_scope(f"candidate={variant + 1}/{self.config.m}"):
                batches = tuple(
                    self._grade_and_record(
                        snapshot,
                        tasks,
                        trajectories[variant][
                            trial * len(tasks) : (trial + 1) * len(tasks)
                        ],
                        label="candidate",
                    )
                    for trial in range(self.config.k)
                )
                try:
                    measurements[variant] = self._summarize_measurement(
                        snapshot,
                        "candidate",
                        batches,
                        {**scheduling, "variant": variant},
                    )
                except _InvalidMeasurement as error:
                    measurements[variant] = error
        return measurements

    def _refresh_task_duration_estimates(self):
        estimates = getattr(self, "_task_duration_estimates", {}).copy()
        estimates.update(
            {
                task: mean(values)
                for task, values in getattr(self, "_pending_task_durations", {}).items()
                if values
            }
        )
        self._task_duration_estimates = estimates
        self._pending_task_durations = {}

    async def _measure(self, snapshot, label):
        resume = (
            getattr(self, "_baseline_resume", None) if label == "baseline" else None
        )
        self._baseline_resume = None
        scheduling = {"policy": "restored_baseline"}
        if resume is None:
            tasks = tuple(self.benchmark.train)
            self._save_snapshot(snapshot)
            scheduling = task_schedule(
                tasks,
                self.config.k,
                getattr(self, "_task_duration_estimates", {}),
                self.config.task_order,
            )
            slots = tasks * self.config.k
            order = [job["index"] for job in scheduling["jobs"]]
            queued = tuple(slots[index] for index in order)
            # One rollout pins one Snapshot/configuration and uses one semaphore
            # across all repeats. A slow first repeat cannot hold up the next.
            # Keep task IDs/checksums intact; every occurrence gets a fresh run.
            with progress_scope(f"repeats={self.config.k}"), progress_scope(label):
                progress_note(
                    f"task order={self.config.task_order}; "
                    f"historical timings={scheduling['known_tasks']}/{len(tasks)}; "
                    f"executions={len(slots)}"
                )
                dispatched = await self.target.rollout(queued, snapshot=snapshot)
            if tuple(row.task for row in dispatched) != queued:
                raise ValueError("repeated rollout must preserve tasks and order")
            trajectories = [None] * len(slots)
            for index, trajectory in zip(order, dispatched, strict=True):
                trajectories[index] = trajectory
            # Results.per_task is keyed by task_id. Grade/record each repeat
            # separately so no trial is overwritten, including its token usage.
            batches = tuple(
                self._grade_and_record(
                    snapshot,
                    tasks,
                    tuple(trajectories[trial * len(tasks) : (trial + 1) * len(tasks)]),
                    label=label,
                )
                for trial in range(self.config.k)
            )
        else:
            batches = resume.batches
            for results in batches:
                self._save_snapshot(snapshot)
                self._record(
                    label,
                    {
                        "snapshot_id": snapshot.id,
                        "trajectories": [_trajectory_record(row) for row in results],
                        "reused_from": str(resume.source),
                    },
                )
                progress_note(f"baseline restored: tasks={len(results)}/{len(results)}")
        return self._summarize_measurement(snapshot, label, batches, scheduling)

    def _summarize_measurement(self, snapshot, label, batches, scheduling):
        """Use the same scoring/cost rules for serial and shared-queue results."""
        trials = []
        for trial, results in enumerate(batches):
            scores = results.per_task
            for trajectory in results:
                score = scores[trajectory.task.task_id]
                if not 0 <= score <= 1:
                    raise ValueError("RRSI requires primary scores in [0, 1]")
                observed_tokens, usage_complete = _token_accounting(trajectory.usage)
                duration = execution_seconds(trajectory)
                if duration is not None:
                    if not hasattr(self, "_pending_task_durations"):
                        self._pending_task_durations = {}
                    self._pending_task_durations.setdefault(
                        trajectory.task.task_id, []
                    ).append(duration)
                trials.append(
                    {
                        "task_id": trajectory.task.task_id,
                        "trial": trial,
                        "reward": score,
                        "weight": trajectory.metrics.get(self.config.weight_metric)
                        if self.config.weight_metric
                        else 1.0,
                        "tokens": observed_tokens if usage_complete else None,
                        "observed_tokens": observed_tokens,
                        "usage_complete": usage_complete,
                        "execution_seconds": duration,
                        "trajectory": self._trajectory_evidence(trajectory),
                    }
                )
        per_task, weights = {}, {}
        for row in trials:
            task = row["task_id"]
            if row["trajectory"]["status"] == "error":
                row["reward"] = 0.0
            per_task.setdefault(task, []).append(row["reward"])
            weights.setdefault(task, []).append(row["weight"])
        score = _weighted_score(
            [row["reward"] for row in trials], [row["weight"] for row in trials]
        )
        guards = {}
        for metric in self.config.guards:
            values = [row["trajectory"]["metrics"].get(metric) for row in trials]
            if None in values:
                raise _InvalidMeasurement(f"missing guard metric: {metric}")
            guards[metric] = mean(values)
        costs = [row["tokens"] for row in trials]
        cost = mean(costs) if None not in costs else None
        if cost is not None and cost <= 0:
            cost = None  # Relative cost cannot use a zero denominator.
        complete = sum(row["usage_complete"] for row in trials)
        missing = sum(row["observed_tokens"] is None for row in trials)
        observed = sum(row["observed_tokens"] or 0 for row in trials)
        accounting = {
            "complete": cost is not None,
            "trials_total": len(trials),
            "trials_complete": complete,
            "trials_partial": len(trials) - complete - missing,
            "trials_missing": missing,
            "observed_tokens": observed,
            "observed_mean_tokens": observed / len(trials),
        }
        errors = sum(row["trajectory"]["status"] == "error" for row in trials)
        too_many_errors = errors > self.config.invalid_missing_frac * len(trials)
        warnings = []
        if cost is None:
            warnings.append(
                "token cost unavailable: "
                f"{complete}/{len(trials)} trials have complete usage; "
                "preserving observed tokens, comparison requires a clear score gain"
            )
        if too_many_errors:
            warnings.append(f"too many failed trials: {errors}/{len(trials)}")
        measurement = {
            "score": score,
            "scheduling": scheduling,
            "cost": cost,
            "cost_accounting": accounting,
            "error_trials": errors,
            "warnings": warnings,
            "per_task": per_task,
            "weights": weights,
            "guard_metrics": guards,
            "evidence": trials,
        }
        estimate_cost(measurement, self.config)
        estimate = accounting.get("estimate", {})
        if estimate.get("eligible"):
            warnings[:] = [
                warning
                for warning in warnings
                if not warning.startswith("token cost unavailable:")
            ]
            _LOGGER.info(
                "RRSI %s: selection token estimate=%.0f; estimated trials=%d/%d; "
                "provider usage remains incomplete; applying conservative sensitivity cost",
                label,
                estimate["total_tokens"],
                estimate["estimated_trials"],
                len(trials),
            )
        for warning in warnings:
            _LOGGER.warning("RRSI %s: %s", label, warning)
        self._record("measurement", {"snapshot_id": snapshot.id, **measurement})
        # Framework recording owns redaction, including full file evidence.
        record = max((self._run_dir / "trajectories").glob("*-measurement.json"))
        measurement["evidence"] = json.loads(record.read_text())["evidence"]
        # Preserve the reliability gate: an invalid baseline aborts, while
        # _select rejects an invalid candidate. Persist the evidence first.
        if too_many_errors:
            raise _InvalidMeasurement("too many failed trials")
        return measurement

    async def _analyze(self, snapshot_id, traces, prior, *, retry_note=""):
        table = [
            {key: value for key, value in trace.items() if key != "evidence"}
            for trace in traces.values()
        ]
        interaction, digests = [], []
        for analysis_turn in range(_ANALYSIS_MAX_TURNS):
            progress_note(
                f"Worker analyst: decision={analysis_turn + 1}/{_ANALYSIS_MAX_TURNS}"
            )
            raw = await self.worker.ask(
                prompts.ANALYZE.format(
                    incumbent=snapshot_id,
                    tasks=json.dumps(table),
                    prior=json.dumps(prior),
                    interaction=json.dumps(interaction),
                )
                + retry_note
            )
            try:
                action = json.loads(_replies.tagged(raw, "ANALYSIS"))
                if not isinstance(action, dict):
                    raise ValueError("return one JSON action object")
                if action.get("action") == "report":
                    if not digests:
                        raise ValueError(
                            "investigate at least one trace before reporting"
                        )
                    report = {
                        key: action.get(key) or []
                        for key in (
                            "failure_modes",
                            "capability_gaps",
                            "success_habits",
                        )
                    }
                    if not all(
                        isinstance(rows, list)
                        and all(isinstance(row, dict) for row in rows)
                        for rows in report.values()
                    ):
                        raise ValueError("report sections must be lists of objects")
                    report["failure_modes"].sort(
                        key=lambda row: -(row.get("n_tasks") or 0)
                    )
                    return {**report, "digests": digests}
                if action.get("action") != "digest_many" or not isinstance(
                    action.get("requests"), list
                ):
                    raise ValueError("request digest_many or return report")
                requests = [
                    request
                    for request in action["requests"][:8]
                    if isinstance(request, dict) and request.get("task_id") in traces
                ]
                if not requests:
                    raise ValueError("request at least one task from the task table")
            except (ValueError, TypeError) as error:
                interaction.append({"reply": raw, "error": str(error)})
                continue
            with progress_scope(f"analysis-batch={analysis_turn + 1}"):
                results = await self._digest_many(requests, traces)
            digests.extend(results)
            interaction.append({"action": action, "results": results})
        raise ValueError("RRSI analyst exhausted its 30 turns without a report")

    async def _digest_many(self, requests, traces):
        """Run independent investigations concurrently, preserving request order."""
        limit = self.config.digest_concurrency
        progress_note(f"Worker digest batch: count={len(requests)} concurrency={limit}")
        semaphore = asyncio.Semaphore(limit)
        async with Progress(len(requests), unit="digests") as progress:

            async def execute(request):
                async with semaphore:
                    task_id = request["task_id"]
                    progress.start(task_id)
                    status = "error"
                    try:
                        result = await self._digest(
                            traces[task_id],
                            request.get("lens", "failure"),
                            request.get("questions", []),
                        )
                        status = "finished"
                        return result
                    except asyncio.CancelledError:
                        status = "cancelled"
                        raise
                    finally:
                        progress.finish(task_id, status)

            if limit == 1:
                return [await execute(request) for request in requests]
            jobs = [asyncio.create_task(execute(request)) for request in requests]
            try:
                return list(await asyncio.gather(*jobs))
            except BaseException:
                # A failed digester must not leave other Worker subprocesses
                # writing into directories that their contexts are removing.
                for job in jobs:
                    if not job.done() and not job.cancelling():
                        job.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                raise

    async def _digest(self, trace, lens, questions):
        """Keep trace investigation outside every target checkout."""
        progress_note(f"Worker: digesting {trace['task_id']}")
        if not isinstance(lens, str) or lens not in prompts.DIGEST_SCHEMAS:
            lens = "failure"
        evidence = trace["evidence"]
        # Native file tools paginate by line; a single long JSON line hides
        # even short outputs and metrics behind the context's size limit.
        original = json.dumps(evidence, ensure_ascii=False, indent=2).encode()
        objection = ""
        for _ in range(_PROTOCOL_RETRIES):
            with TemporaryDirectory(
                prefix="rrsi-digest-", dir=self._run_dir / "working"
            ) as temporary:
                directory = Path(temporary)
                path = directory / "trace.json"
                path.write_bytes(original)
                raw = await self.worker.edit(
                    directory,
                    prompts.DIGEST.format(
                        evidence="trace.json",
                        task_id=trace["task_id"],
                        lens=lens,
                        questions=json.dumps(questions),
                        max_chars=_DIGEST_MAX_CHARS,
                        schema=prompts.DIGEST_SCHEMAS[lens],
                        objection=objection,
                    ),
                )
                try:
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or path.read_bytes() != original
                    ):
                        raise ValueError("digester changed its read-only trace")
                    value = _replies.tagged(raw, "DIGEST")
                    if len(value) > _DIGEST_MAX_CHARS:
                        raise ValueError("digest exceeds 6000 characters")
                    result = json.loads(value)
                    if (
                        not isinstance(result, dict)
                        or result.get("task_id") != trace["task_id"]
                        or result.get("lens") != lens
                    ):
                        raise ValueError(
                            "digest must identify the requested task and lens"
                        )
                    quotes = result.get("evidence", [])
                    if not isinstance(quotes, list):
                        raise ValueError("digest evidence must be a list")
                    for index, row in enumerate(quotes):
                        if (
                            not isinstance(row, dict)
                            or not isinstance(row.get("quote"), str)
                            or not row["quote"]
                            or not _quote_in_trace(row["quote"], evidence)
                        ):
                            raise ValueError(
                                f"digest evidence[{index}].quote must be an exact "
                                "substring of one decoded trace value; put field "
                                "names in where, not in quote"
                            )
                    return result
                except ValueError as error:
                    objection = str(error)
        raise ValueError(f"RRSI digest invalid: {objection}")

    async def _propose(self, parent, variant, context, evidence_file, *, retry_note=""):
        step, budget = context["step"], context["budget"]
        reserved = (
            context["untried"]
            if context["stalled"] and variant >= self.config.m - self.config.m_draft
            else []
        )
        edits, changes, objection = [], "", ""
        with parent.check_out(working_dir=self._run_dir / "working") as directory:
            for attempt in range(self.config.repair_rounds + 1):
                progress_note(
                    f"Worker: edit attempt={attempt + 1}/{self.config.repair_rounds + 1}"
                )
                with TemporaryDirectory(
                    prefix="rrsi-evidence-", dir=directory
                ) as temporary:
                    reference = Path(temporary) / "context.json"
                    shutil.copy2(evidence_file, reference)
                    # Repair retains the checkout, so its manifest must account
                    # for earlier edits as well as this call's new changes.
                    repair_diff = reference.with_name("current-diff.txt")
                    repair_diff.write_text(changes, encoding="utf-8")
                    reply = await self.worker.edit(
                        directory,
                        prompts.PROPOSE.format(
                            step=step,
                            variant=variant,
                            evidence=reference.relative_to(directory).as_posix(),
                            current_diff=repair_diff.relative_to(directory).as_posix(),
                            budget=budget,
                            components=json.dumps(sorted(_COMPONENTS)),
                            permissions=json.dumps(dict(parent.component_rules)),
                            reserved=json.dumps(reserved),
                            objection=objection,
                        )
                        + retry_note,
                    )
                discard_runtime_files(directory)
                edits, changes = [], ""
                try:
                    snapshot = self._commit(parent, directory)
                    changed = parent.changed_paths(snapshot)
                    if not changed:
                        raise ValueError("the harness was left unchanged")
                    changes = _diff(parent, snapshot)
                    edits = _parse_edits(reply, changed, budget)
                    if re.search(_CREDENTIAL_PATTERN, changes):
                        raise ValueError("credential pattern matched the diff")
                except ValueError as error:
                    objection = str(error)
                else:
                    review = await self._review(
                        prompts.CRITIC.format(edits=json.dumps(edits), diff=changes)
                    )
                    self._record(
                        "critic", {"step": step, "variant": variant, "review": review}
                    )
                    objection = (
                        json.dumps(review) if review["verdict"] == "reject" else ""
                    )
                    if not objection:
                        for edit in edits:
                            tag = _normalize_component(
                                edit["component"],
                                _diff(parent, snapshot, edit["paths"]),
                                self.config.component_signals,
                            )
                            if tag != edit["component"]:
                                edit["declared_component"], edit["component"] = (
                                    edit["component"],
                                    tag,
                                )
                        if reserved and not any(
                            edit["component"] in reserved for edit in edits
                        ):
                            objection = f"reserved slot must exercise one of {reserved}, judged by the diff"
                    if not objection:
                        failure = await self._smoke(snapshot, context["traces"])
                        return (None if failure else snapshot), edits, changes, failure
                self._record(
                    "repair",
                    {
                        "step": step,
                        "variant": variant,
                        "attempt": attempt,
                        "objection": objection,
                        "diff": changes,
                    },
                )
        return None, edits, changes, objection

    async def _review(self, payload):
        progress_note("Worker: critic review")
        for _ in range(_PROTOCOL_RETRIES):
            raw = await self.worker.ask(payload)
            try:
                review = json.loads(_replies.tagged(raw, "REVIEW"))
                if isinstance(review, dict) and review.get("verdict") in (
                    "accept",
                    "reject",
                ):
                    return review
            except ValueError:
                pass
        return {
            "verdict": "reject",
            "reasons": ["critic output unparseable after 3 attempts"],
        }

    async def _smoke(self, snapshot, traces):
        try:
            for path, entry in snapshot.files.items():
                if path.endswith(".py"):
                    compile(entry.content, path, "exec")
        except SyntaxError as error:
            self._record(
                "smoke-gate",
                {
                    "snapshot_id": snapshot.id,
                    "passed": False,
                    "stage": "compile",
                    "error": str(error),
                },
            )
            return "smoke compile failed: " + str(error)
        tasks = [task for task in self.benchmark.train if task.task_id in traces][
            : self.config.smoke_n
        ]
        smoke = await self._evaluate(snapshot, tasks, label="smoke") if tasks else ()
        failure = "smoke failed" if any(row.status == "error" for row in smoke) else ""
        self._record(
            "smoke-gate",
            {
                "snapshot_id": snapshot.id,
                "passed": not failure,
                "stage": "rollout" if tasks else "compile",
                "task_ids": [task.task_id for task in tasks],
            },
        )
        return failure

    def _check(self):
        config = self.config
        unknown = set(config) - self._defaults.keys() - {"name", "worker"}
        if unknown:
            raise ValueError(f"unknown RRSI options: {', '.join(sorted(unknown))}")
        if not self.benchmark.train:
            raise ValueError("RRSI requires a non-empty train split")
        ids = [task.task_id for task in self.benchmark.train]
        if len(set(ids)) != len(ids):
            raise ValueError("RRSI requires unique train task_id values")
        if (
            self.target is self.worker
            or self.worker.config.get("type", "agent") != "agent"
        ):
            raise ValueError("RRSI requires a separate file-editing Agent Worker")
        if config.b_min > config.b_max:
            raise ValueError("RRSI b_min must not exceed b_max")
        if config.m_draft > config.m:
            raise ValueError("RRSI m_draft must not exceed m")
        if config.delta is None and config.k < 2 and config.calibration_evals < 2:
            raise ValueError("RRSI automatic noise calibration requires k >= 2")
        if config.invalid_missing_frac > 1:
            raise ValueError("RRSI invalid_missing_frac must be in [0, 1]")
        if config.cost_estimate_max_frac > 1:
            raise ValueError("RRSI cost_estimate_max_frac must be in [0, 1]")
        if config.n_fail_traces + config.n_success_traces == 0:
            raise ValueError("RRSI analysis requires at least one representative trace")
        if (
            not isinstance(config.component_signals, Mapping)
            or set(config.component_signals) - _COMPONENTS
        ):
            raise ValueError(
                "component_signals must map semantic component names to regex lists"
            )
        for patterns in config.component_signals.values():
            if (
                not isinstance(patterns, list)
                or not patterns
                or not all(isinstance(pattern, str) for pattern in patterns)
            ):
                raise ValueError(
                    "component_signals values must be non-empty regex lists"
                )
            for pattern in patterns:
                try:
                    re.compile(pattern)
                except re.error as error:
                    raise ValueError(f"invalid component signal: {error}") from None
        if config.weight_metric is not None and (
            not isinstance(config.weight_metric, str) or not config.weight_metric
        ):
            raise ValueError("weight_metric must be a metric name or null")
        if not isinstance(config.guards, Mapping):
            raise ValueError("guards must map metric names to max_drop/max_rise limits")
        for metric, limits in config.guards.items():
            if (
                not isinstance(metric, str)
                or not metric
                or not isinstance(limits, Mapping)
                or not limits
                or set(limits) - {"max_drop", "max_rise"}
            ):
                raise ValueError("invalid guard metric or limits")
            if any(
                type(limit) not in (int, float) or not math.isfinite(limit) or limit < 0
                for limit in limits.values()
            ):
                raise ValueError("guard limits must be finite nonnegative numbers")


def _edit_budget(t, config):
    value = (
        config.b_min
        + (config.b_max - config.b_min) * (1 + math.cos(math.pi * t / config.T)) / 2
    )
    return math.ceil(round(value, 9))


def _directives(history, scores, t, delta, config):
    measured = [row for row in history if row["delta_S"] is not None]
    tried = {row["component"] for row in measured}
    untried = sorted(_COMPONENTS - tried)
    stalled = t >= config.w and scores[t] - scores[t - config.w] <= delta
    prune = []
    for component in sorted(tried):
        recent = [
            row["delta_S"]
            for row in measured
            if row["component"] == component and t - row["t"] <= config.n_prune
        ]
        if not recent or max(recent) <= 0:
            prune.append(
                {
                    "component": component,
                    "recent_best_gain": max(recent) if recent else None,
                    "accepted_edits": [
                        row
                        for row in history
                        if row["component"] == component and row["accepted"]
                    ],
                }
            )
    return {"stalled": stalled, "untried": untried, "prune": prune}


def _noise_band(trials, z, seed, weights=None):
    """Bootstrap trials within each task, preserving the evolve-set weights."""
    rng = random.Random(seed)
    weights = weights or {task: [1.0] * len(values) for task, values in trials.items()}
    samples = []
    for _ in range(2000):
        numerator = denominator = 0.0
        for task, values in trials.items():
            for _ in values:
                index = rng.randrange(len(values))
                weight = weights[task][index]
                numerator += values[index] * weight
                denominator += weight
        samples.append(numerator / denominator)
    return round(z * math.sqrt(2) * pstdev(samples), 6)


def _calibrate(evaluations, z, seed):
    """Repeated baselines, falling back to the upstream pooled trial bootstrap."""
    scores = [evaluation["score"] for evaluation in evaluations]
    sd = stdev(scores) * math.sqrt(2) if len(scores) > 1 else 0.0
    if sd > 0:
        delta = round(z * sd, 6)
        source = "repeated base evaluations"
    else:
        trials = {task: [] for task in evaluations[0]["per_task"]}
        weights = {task: [] for task in trials}
        for evaluation in evaluations:
            for task in trials:
                trials[task].extend(evaluation["per_task"][task])
                weights[task].extend(evaluation["weights"][task])
        # Pooling increases k; rescale its SE to the original evaluation size.
        delta = _noise_band(trials, z * math.sqrt(len(evaluations)), seed, weights)
        source = "bootstrap within-task baseline trials"
    return {"delta": delta, "source": source, "n_evals": len(evaluations)}


def _judge(measurement, incumbent, best_score, delta, edits, accepted, config):
    ds = measurement["score"] - incumbent["score"]
    candidate_cost = selection_cost(measurement, config)
    incumbent_cost = selection_cost(incumbent, config)
    comparable = candidate_cost is not None and incumbent_cost is not None
    estimated = comparable and "estimated" in (candidate_cost[3], incumbent_cost[3])
    # For estimated costs, charge the candidate's high scenario against the
    # incumbent's known lower bound. A favourable point estimate alone cannot win.
    dc = (
        (candidate_cost[2] - incumbent_cost[1]) / incumbent_cost[1]
        if comparable
        else None
    )
    dc_point = (
        (candidate_cost[0] - incumbent_cost[0]) / incumbent_cost[0]
        if comparable
        else None
    )
    novelty = len({edit["component"] for edit in edits} & _STRUCTURAL - accepted)
    if measurement["score"] < best_score - delta:
        admissible, reason = False, "below historical best minus noise band"
    elif not comparable:
        # Missing counters cannot earn a cost-saving or novelty-only promotion.
        # Continue search using a clear measured gain; do not fabricate cost.
        admissible = ds > delta
        reason = (
            "clear score gain with incomplete token accounting"
            if admissible
            else "no clear score gain with incomplete token accounting"
        )
    elif ds > delta:
        admissible = dc <= config.beta0 + config.beta1 * ds
        reason = "gain/cost budget"
    else:
        admissible = config.w_s * ds - config.w_c * dc + config.w_n * novelty > 0
        reason = "within-band score/cost/novelty rule"
    violations = []
    for metric, limits in config.get("guards", {}).items():
        change = (
            measurement["guard_metrics"][metric] - incumbent["guard_metrics"][metric]
        )
        if "max_drop" in limits and -change > limits["max_drop"]:
            violations.append(f"{metric} fell {-change:.6f}")
        if "max_rise" in limits and change > limits["max_rise"]:
            violations.append(f"{metric} rose {change:.6f}")
    if admissible and violations:
        admissible, reason = False, "domain guard violated: " + "; ".join(violations)
    return {
        "admissible": admissible,
        "reason": reason,
        "delta_S": ds,
        "delta_C": dc,
        "cost_comparison": "estimated_conservative"
        if estimated
        else "full"
        if comparable
        else "unavailable",
        "delta_C_point": dc_point,
        "utility": config.w_s * ds - config.w_c * dc + config.w_n * novelty
        if comparable
        else None,
        "utility_point": config.w_s * ds - config.w_c * dc_point + config.w_n * novelty
        if comparable
        else None,
        "novelty": novelty,
        "guards": violations,
    }


def _attribution(edits, incumbent, candidate, threshold):
    gains = {
        task: _task_score(candidate, task) - _task_score(incumbent, task)
        for task in incumbent["per_task"]
    }
    drops = [task for task, gain in gains.items() if gain <= -threshold]
    rows = []
    for edit in edits:
        predicted = [task for task in edit["predicted_affected"] if task in gains]
        hits = [task for task in predicted if gains[task] > 0]
        rows.append(
            {
                "n_predicted": len(predicted),
                "predicted_hit": hits,
                "hit_rate": len(hits) / len(predicted) if predicted else None,
                "unpredicted_regressions": [
                    task for task in drops if task not in predicted
                ],
            }
        )
    return rows


def _task_score(measurement, task):
    rewards = measurement["per_task"][task]
    weights = measurement.get("weights", {}).get(task, [1.0] * len(rewards))
    return _weighted_score(rewards, weights)


def _weighted_score(rewards, weights):
    if any(weight is None or weight <= 0 for weight in weights):
        raise _InvalidMeasurement(
            "complete positive weight_metric accounting is required"
        )
    return sum(reward * weight for reward, weight in zip(rewards, weights)) / sum(
        weights
    )


def _tokens(usage):
    """Count native provider usage once, including cached and delegated work."""
    observed, complete = _token_accounting(usage)
    return observed if complete else None


def _token_accounting(usage):
    """Return (known token lower bound, complete); never substitute missing by 0."""
    if not isinstance(usage, Mapping):
        return None, False
    complete = usage.get("accounting_complete") is not False

    def combine(rows):
        known = [value for value, _ in rows if value is not None]
        return (
            sum(known) if known else None,
            complete and bool(rows) and all(ok for _, ok in rows),
        )

    models = usage.get("model_usage")
    if isinstance(models, Mapping) and models:
        # Native session totals include delegated work absent from turn counters.
        # Partial session accounting must not fall back to the parent's counters.
        return combine([_token_accounting(value) for value in models.values()])
    if "total_tokens" in usage:
        value = usage["total_tokens"]
        return (value, complete) if _counter(value) else (None, False)
    for input_key, output_key in (
        ("input_tokens", "output_tokens"),
        ("prompt_tokens", "completion_tokens"),
        ("inputTokens", "outputTokens"),
    ):
        if input_key in usage or output_key in usage:
            values = [usage.get(input_key), usage.get(output_key)]
            if input_key == "input_tokens":
                values += [
                    usage.get(key, 0)
                    for key in (
                        "cache_creation_input_tokens",
                        "cache_read_input_tokens",
                    )
                ]
            elif input_key == "inputTokens":
                values += [
                    usage.get(key, 0)
                    for key in ("cacheCreationInputTokens", "cacheReadInputTokens")
                ]
            known = [value for value in values if _counter(value)]
            # Default cache zeros do not prove any policy usage was reported.
            observed = (
                sum(known)
                if any(_counter(usage.get(key)) for key in (input_key, output_key))
                else None
            )
            return observed, complete and all(_counter(value) for value in values)
    calls = usage.get("model_calls")
    if isinstance(calls, (tuple, list)) and calls:
        return combine(
            [
                _token_accounting(
                    call.get("usage") if isinstance(call, Mapping) else None
                )
                for call in calls
            ]
        )
    return None, False


def _counter(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _select_traces(measurement, failures, successes):
    ranked = sorted(
        measurement["per_task"], key=lambda task: _task_score(measurement, task)
    )
    failed = ranked[:failures]
    passed = (
        [task for task in ranked[-successes:] if task not in failed]
        if successes
        else []
    )
    evidence = {}
    for row in measurement["evidence"]:
        evidence.setdefault(row["task_id"], []).append(row)
    selected = {}
    for task in failed + passed:
        rewards = measurement["per_task"][task]
        reward = min(rewards) if task in failed else max(rewards)
        index = rewards.index(reward)
        selected[task] = {
            "task_id": task,
            "rewards": rewards,
            "score": _task_score(measurement, task),
            "reward": reward,
            "trial": index,
            "lens": "failure" if task in failed else "success",
            "evidence": evidence[task][index],
        }
    return selected


def _quote_in_trace(quote, value):
    """Search decoded strings so multiline and Unicode quotes remain valid."""
    if isinstance(value, Mapping):
        return any(_quote_in_trace(quote, item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_quote_in_trace(quote, item) for item in value)
    return quote in str(value)


def _parse_edits(reply, changed, budget):
    value = json.loads(_replies.tagged(reply, "EDITS"))
    edits = value.get("edits") if isinstance(value, dict) else None
    if not isinstance(edits, list) or not 1 <= len(edits) <= budget:
        raise ValueError(f"declare between 1 and {budget} independent edits")
    covered = set()
    identifiers = set()
    for edit in edits:
        if not isinstance(edit, dict):
            raise ValueError("each edit must be an object")
        component = edit.get("component")
        if isinstance(component, str):
            component = edit["component"] = component.lower()
        if not isinstance(component, str) or component not in _COMPONENTS:
            raise ValueError("edit component must be an RRSI semantic tag")
        for key in ("id", "hypothesis", "targets_mode", "retroactive_check"):
            if not isinstance(edit.get(key), str) or not edit[key].strip():
                raise ValueError(f"each edit requires a non-empty {key}")
        if edit["id"] in identifiers:
            raise ValueError("edit ids must be unique within a candidate")
        identifiers.add(edit["id"])
        predictions = edit.get("predicted_affected")
        if (
            not isinstance(predictions, list)
            or not predictions
            or not all(isinstance(task, str) and task for task in predictions)
        ):
            raise ValueError("each edit requires predicted_affected task ids")
        if edit.get("operation", "change") not in ("change", "prune"):
            raise ValueError("edit operation must be change or prune")
        files = edit.get("paths")
        if not isinstance(files, list) or not files:
            raise ValueError("each edit requires a non-empty paths list")
        for path in files:
            if not isinstance(path, str) or path not in changed:
                raise ValueError("declared paths must actually change")
        covered.update(files)
    if covered != set(changed):
        raise ValueError(
            "declare every changed file relative to the incumbent; "
            f"missing paths: {json.dumps(sorted(set(changed) - covered))}"
        )
    return edits


def _diff(before, after, paths=None):
    """Include additions, deletions, modes and binary changes in the review."""
    chunks = []
    for path in before.changed_paths(after) if paths is None else paths:
        old, new = before.files.get(path), after.files.get(path)
        chunks.append(f"FILE {path}\n")
        chunks.append(
            f"executable: {old.executable if old else None} -> "
            f"{new.executable if new else None}\n"
        )
        try:
            first = old.content.decode("utf-8") if old else ""
            second = new.content.decode("utf-8") if new else ""
        except UnicodeDecodeError:
            # Opaque edits cannot be inspected by this text critic.
            raise ValueError(f"RRSI cannot review a binary edit: {path}") from None
        chunks.extend(
            unified_diff(
                first.splitlines(keepends=True),
                second.splitlines(keepends=True),
                fromfile=f"before/{path}" if old else "/dev/null",
                tofile=f"after/{path}" if new else "/dev/null",
            )
        )
    return "".join(chunks)


def _normalize_component(declared, changes, signals):
    """Match upstream diff evidence and fallback, independently of file scope."""
    rules = [*signals.items(), *_GENERIC_SIGNALS.items()]
    if any(
        component == declared
        and any(re.search(pattern, changes) for pattern in patterns)
        for component, patterns in rules
    ):
        return declared
    lines = [
        line[1:].strip()
        for line in changes.splitlines()
        if line.startswith(("+", "-"))
        and not line.startswith(("+++", "---"))
        and line[1:].strip()
    ]
    if lines and all(re.match(r"(?:[frb]?[\"']|#)", line) for line in lines):
        return "prompt"
    for component, patterns in rules:
        if any(re.search(pattern, changes) for pattern in patterns):
            return component
    return "prompt"


__all__ = ["RRSI"]
