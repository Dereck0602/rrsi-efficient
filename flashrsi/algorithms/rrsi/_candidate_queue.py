"""One bounded queue for a round's candidates, with explicit trial identity."""

from __future__ import annotations

import asyncio
import copy
from collections import deque
from dataclasses import dataclass

from flashrsi._progress import Progress, progress_scope, progress_session
from flashrsi.algorithms.rrsi._scheduling import task_schedule
from flashrsi.core import Snapshot, Task
from flashrsi.core.agent import _Execution


@dataclass(frozen=True)
class CandidateJob:
    step: int
    variant: int
    snapshot: Snapshot
    task: Task
    trial: int
    index: int
    estimated_seconds: float | None

    @property
    def key(self):
        return (
            self.step,
            self.variant,
            self.snapshot.id,
            self.task.task_id,
            self.task.metadata.get("task_checksum"),
            self.trial,
        )

    def record(self):
        return {
            "step": self.step,
            "variant": self.variant,
            "snapshot_id": self.snapshot.id,
            "task_id": self.task.task_id,
            "task_checksum": self.task.metadata.get("task_checksum"),
            "trial": self.trial,
            "index": self.index,
            "estimated_seconds": self.estimated_seconds,
        }


def candidate_schedule(candidates, tasks, repeats, estimates, policy, step):
    """Interleave candidates at each canonical slot, then apply stable LPT."""
    if len({task.task_id for task in tasks}) != len(tasks):
        raise ValueError("shared candidate queue requires unique train task IDs")
    if len({variant for variant, _ in candidates}) != len(candidates):
        raise ValueError("shared candidate queue requires unique variants")
    plan = task_schedule(tasks, repeats, estimates, policy)
    jobs = tuple(
        CandidateJob(
            step,
            variant,
            snapshot,
            tasks[item["index"] % len(tasks)],
            item["trial"],
            item["index"],
            item["estimated_seconds"],
        )
        for item in plan["jobs"]
        for variant, snapshot in candidates
    )
    # The single-candidate schedule remains compatible with existing records.
    return jobs, {**plan, "shared_candidate_queue": True, "step": step}


def _validate_result(job, row):
    if row.task != job.task:
        raise ValueError(f"shared candidate queue task mismatch: {job.key}")
    configs = [m for m in row.messages if m.get("type") == "RunConfig"]
    if not configs or any(m.get("snapshot_id") != job.snapshot.id for m in configs):
        raise ValueError(f"shared candidate queue Snapshot mismatch: {job.key}")
    for event in row.messages:
        if event.get("type") != "HarborVerifier":
            continue
        if (
            event.get("snapshot_id") != job.snapshot.id
            or event.get("task_id") != job.task.task_id
            or (
                job.task.metadata.get("task_checksum") is not None
                and event.get("task_checksum") != job.task.metadata["task_checksum"]
            )
        ):
            raise ValueError(f"shared candidate queue verifier mismatch: {job.key}")


def collect_results(jobs, completed):
    """Demultiplex by full job key, rejecting duplicate/missing/wrong results."""
    expected = {job.key: job for job in jobs}
    if len(expected) != len(jobs):
        raise ValueError("shared candidate queue contains duplicate jobs")
    results = {}
    for key, row in completed:
        if key not in expected or key in results:
            raise ValueError("shared candidate queue returned duplicate/unknown job")
        _validate_result(expected[key], row)
        results[key] = row
    if results.keys() != expected.keys():
        raise ValueError("shared candidate queue returned missing jobs")
    grouped = {}
    for job in sorted(jobs, key=lambda job: (job.variant, job.index)):
        grouped.setdefault(job.variant, []).append(results[job.key])
    return {variant: tuple(rows) for variant, rows in grouped.items()}


async def execute_candidates(target, jobs):
    """Preserve adapter rollout hooks and wait for cleanup before freeing slots."""
    config = copy.deepcopy(target.config)
    concurrency = config.get("concurrency", 1)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("agent.concurrency must be a positive integer")
    baseline = target.snapshot
    for snapshot in {job.snapshot.id: job.snapshot for job in jobs}.values():
        target._validate_snapshot(snapshot, baseline=baseline)
    pending = deque(jobs)
    completed = []
    with progress_scope("shared candidate queue"):
        async with Progress(len(jobs)) as progress:

            async def worker():
                while pending:
                    job = pending.popleft()
                    label = (
                        f"candidate={job.variant + 1} snapshot={job.snapshot.id[:12]} "
                        f"{job.task.task_id} [repeat={job.trial + 1}]"
                    )
                    progress.start(label)
                    status = "error"
                    token = target._execution.set(
                        _Execution(job.snapshot, None, copy.deepcopy(config))
                    )
                    try:
                        # Each existing rollout receives one explicitly pinned
                        # job. Its timer/deadlines start only after dispatch.
                        # Show the aggregate queue instead of one bar per job.
                        with progress_session("off"):
                            rows = await target.rollout(
                                (job.task,), snapshot=job.snapshot
                            )
                        if len(rows) != 1:
                            raise ValueError(
                                "shared candidate rollout must return one job"
                            )
                        _validate_result(job, rows[0])
                        completed.append((job.key, rows[0]))
                        status = rows[0].status
                    except asyncio.CancelledError:
                        status = "cancelled"
                        raise
                    finally:
                        target._execution.reset(token)
                        progress.finish(label, status)

            workers = [
                asyncio.create_task(worker())
                for _ in range(min(concurrency, len(jobs)))
            ]
            try:
                await asyncio.gather(*workers)
            except BaseException:
                for worker_task in workers:
                    if not worker_task.done() and not worker_task.cancelling():
                        worker_task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                raise
    return collect_results(jobs, completed)
