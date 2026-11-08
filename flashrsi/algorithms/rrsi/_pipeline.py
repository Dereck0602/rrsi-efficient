"""Independent candidate producers feed one bounded Target execution queue."""

from __future__ import annotations

import asyncio
import copy
from contextvars import ContextVar
from dataclasses import dataclass, replace

from flashrsi._progress import Progress, progress_scope
from flashrsi.algorithms.rrsi._candidate_queue import (
    EvaluationQueue,
    cancel_and_wait,
    candidate_schedule,
)
from flashrsi.core.agent import _Execution


@dataclass(frozen=True)
class CandidateGeneration:
    step: int
    variant: int
    queue: EvaluationQueue


GENERATION = ContextVar("rrsi_candidate_generation", default=None)


async def smoke_rollout(algorithm, snapshot, tasks, generation):
    """Smoke shares evaluation slots and still grades/records exactly once."""
    algorithm._save_snapshot(snapshot)
    jobs, _ = candidate_schedule(
        ((generation.variant, snapshot),), tasks, 1, {}, "fifo", generation.step
    )
    jobs = tuple(replace(job, phase="smoke") for job in jobs)
    rows = await generation.queue.submit(jobs)
    return algorithm._grade_and_record(
        snapshot, tuple(tasks), rows[generation.variant], label="smoke"
    )


async def generate_candidates(algorithm, parent, context, evidence):
    config = algorithm.config
    if config.proposal_concurrency == 1 and not config.pipeline_evaluation:
        return [
            await algorithm._propose_candidate(parent, variant, context, evidence)
            for variant in range(config.m)
        ], None

    tasks = tuple(algorithm.benchmark.train)
    estimates = dict(getattr(algorithm, "_task_duration_estimates", {}))
    step = context["step"]
    worker = algorithm.worker
    worker_config = copy.deepcopy(worker.config)
    active = worker._execution.get()
    worker_snapshot = active.snapshot if active is not None else worker._snapshot
    proposals = [None] * config.m
    trajectories = {}
    # This one frozen schedule is used by every candidate, regardless of when
    # it finishes generation or how quickly its evaluation tasks complete.
    _, scheduling = candidate_schedule(
        (), tasks, config.k, estimates, config.task_order, step
    )
    scheduling = {**scheduling, "pipeline_evaluation": config.pipeline_evaluation}
    semaphore = asyncio.Semaphore(config.proposal_concurrency)
    dispatches = 0

    def dispatch(job):
        nonlocal dispatches
        dispatches += 1
        algorithm._record(
            "candidate-dispatch", {"dispatch": dispatches, **job.record()}
        )

    async with EvaluationQueue(
        algorithm.target,
        label="candidate pipeline",
        on_dispatch=dispatch,
        checkpoint=getattr(algorithm, "_checkpoints", None),
    ) as queue:
        with progress_scope("candidate generation"):
            async with Progress(config.m, unit="candidates") as progress:

                async def produce(variant):
                    async with semaphore:
                        label = f"candidate={variant + 1}/{config.m}"
                        progress.start(label)
                        status = "error"
                        generation_token = GENERATION.set(
                            CandidateGeneration(step, variant, queue)
                        )
                        worker_token = worker._execution.set(
                            _Execution(
                                worker_snapshot, None, copy.deepcopy(worker_config)
                            )
                        )
                        try:
                            proposal = await algorithm._propose_candidate(
                                parent, variant, copy.deepcopy(context), evidence
                            )
                            proposals[variant] = proposal
                            snapshot = proposal[0]
                            algorithm._record(
                                "candidate-ready",
                                {
                                    "step": step,
                                    "variant": variant,
                                    "snapshot_id": snapshot.id
                                    if snapshot is not None
                                    else None,
                                    "eligible": snapshot is not None,
                                    "reason": proposal[3],
                                },
                            )
                            status = "finished"
                        except asyncio.CancelledError:
                            status = "cancelled"
                            raise
                        finally:
                            worker._execution.reset(worker_token)
                            GENERATION.reset(generation_token)
                            progress.finish(label, status)
                    # Release the generation slot before waiting on evaluation.
                    # No score or partial trace enters a sibling's context.
                    if config.pipeline_evaluation and snapshot is not None:
                        jobs, _ = candidate_schedule(
                            ((variant, snapshot),),
                            tasks,
                            config.k,
                            estimates,
                            config.task_order,
                            step,
                        )
                        algorithm._save_snapshot(snapshot)
                        algorithm._record(
                            "candidate-queue",
                            {
                                "step": step,
                                "variant": variant,
                                "pipeline_evaluation": True,
                                "concurrency": queue.concurrency,
                                "policy": config.task_order,
                                "jobs": [job.record() for job in jobs],
                            },
                        )
                        rows = await queue.submit(jobs)
                        trajectories[variant] = rows[variant]

                producers = [
                    asyncio.create_task(produce(variant)) for variant in range(config.m)
                ]
                try:
                    await asyncio.gather(*producers)
                except BaseException:
                    # Cancel all proposal processes and await their directory
                    # cleanup; queue exit also waits for native Target cleanup.
                    await cancel_and_wait(producers)
                    raise
    measurements = None
    if config.pipeline_evaluation:
        candidates = tuple(
            (variant, proposal[0])
            for variant, proposal in enumerate(proposals)
            if proposal[0] is not None
        )
        measurements = algorithm._summarize_candidates(
            candidates, tasks, trajectories, scheduling
        )
    return proposals, measurements
