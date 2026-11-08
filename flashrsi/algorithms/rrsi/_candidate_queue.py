"""One bounded queue for a round's candidates, with explicit trial identity."""

from __future__ import annotations

import asyncio
import copy
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
    rank: int = 0
    phase: str = "candidate"

    @property
    def key(self):
        return (
            self.step,
            self.variant,
            self.snapshot.id,
            self.task.task_id,
            self.task.metadata.get("task_checksum"),
            self.trial,
            self.phase,
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
            "dispatch_rank": self.rank,
            "phase": self.phase,
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
            rank=rank,
        )
        for rank, item in enumerate(plan["jobs"])
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


async def cancel_and_wait(tasks):
    """Never interrupt a child's asynchronous cleanup a second time."""
    for task in tasks:
        if not task.done() and not task.cancelling():
            task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class EvaluationQueue:
    """Accept ready batches; smoke and full trials share the same Target slots.

    Smoke has priority over waiting full trials. Otherwise use the frozen
    per-round task rank, then candidate index. Running jobs are never preempted.
    """

    def __init__(self, target, *, label="shared candidate queue", on_dispatch=None):
        self.target = target
        self.config = copy.deepcopy(target.config)
        self.baseline = target.snapshot
        self.concurrency = self.config.get("concurrency", 1)
        if type(self.concurrency) is not int or self.concurrency < 1:
            raise ValueError("agent.concurrency must be a positive integer")
        self.label = label
        self.on_dispatch = on_dispatch
        self.pending = asyncio.PriorityQueue()
        self._futures = set()
        self._keys = set()
        self._sequence = 0
        self._error = None
        self._closed = False

    async def __aenter__(self):
        with progress_scope(self.label):
            self.progress = await Progress(0).__aenter__()
        self.workers = [
            asyncio.create_task(self._worker()) for _ in range(self.concurrency)
        ]
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self._closed = True
        for future in self._futures:
            if not future.done():
                future.cancel()
        await cancel_and_wait(self.workers)
        await self.progress.__aexit__(exc_type, exc, traceback)

    async def submit(self, jobs):
        if self._error is not None:
            raise self._error
        if self._closed:
            raise RuntimeError("evaluation queue is closed")
        jobs = tuple(jobs)
        keys = {job.key for job in jobs}
        if len(keys) != len(jobs) or keys & self._keys:
            raise ValueError("shared candidate queue contains duplicate jobs")
        for snapshot in {job.snapshot.id: job.snapshot for job in jobs}.values():
            self.target._validate_snapshot(snapshot, baseline=self.baseline)
        self._keys.update(keys)
        futures = [asyncio.get_running_loop().create_future() for _ in jobs]
        self._futures.update(futures)
        self.progress.total += len(jobs)
        if self.progress.bar is not None:
            self.progress.bar.total = self.progress.total
        for job, future in zip(jobs, futures, strict=True):
            self._sequence += 1
            priority = (0 if job.phase == "smoke" else 1, job.rank, job.variant)
            self.pending.put_nowait((priority, self._sequence, job, future))
        try:
            rows = await asyncio.gather(*futures)
            return collect_results(
                jobs, zip((job.key for job in jobs), rows, strict=True)
            )
        finally:
            self._futures.difference_update(futures)

    def _fail(self, error):
        self._error = error
        # Wake every producer, including ones waiting for smoke. The round's
        # gather then cancels proposal processes as well as evaluation tasks.
        for future in self._futures:
            if not future.done():
                future.set_exception(error)
        for task in self.workers:
            if (
                task is not asyncio.current_task()
                and not task.done()
                and not task.cancelling()
            ):
                task.cancel()

    async def _worker(self):
        while True:
            _, _, job, future = await self.pending.get()
            if future.done():
                continue
            label = (
                f"{job.phase} candidate={job.variant + 1} snapshot={job.snapshot.id[:12]} "
                f"{job.task.task_id} [repeat={job.trial + 1}]"
            )
            self.progress.start(label)
            status = "error"
            token = self.target._execution.set(
                _Execution(job.snapshot, None, copy.deepcopy(self.config))
            )
            try:
                if self.on_dispatch is not None:
                    self.on_dispatch(job)
                # Existing adapter hooks and their timers/deadlines start only
                # after acquiring a slot. Native cleanup remains in this slot.
                with progress_session("off"):
                    rows = await self.target.rollout((job.task,), snapshot=job.snapshot)
                if len(rows) != 1:
                    raise ValueError("shared candidate rollout must return one job")
                _validate_result(job, rows[0])
                status = rows[0].status
                if not future.done():
                    future.set_result(rows[0])
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as error:
                self._fail(error)
                return
            finally:
                self.target._execution.reset(token)
                self.progress.finish(label, status)


async def execute_candidates(target, jobs):
    """Evaluate a fixed batch through the same queue used by the pipeline."""
    async with EvaluationQueue(target) as queue:
        return await queue.submit(jobs)
