"""Full repeats share capacity without sharing task state or losing trials."""

import asyncio
from collections import Counter
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from test_rrsi import saved, setup

from flashrsi._progress import progress_session
from flashrsi.agents import MiniAgent
from flashrsi.core import Task, Trajectory


@pytest.mark.parametrize("task_order", ["fifo", "longest_first"])
def test_second_repeat_starts_before_slow_first_repeat_finishes(
    monkeypatch, tmp_path, task_order
):
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [], k=2, task_order=task_order)
    target = algo.target
    target.config.concurrency = 8
    target.config.marker = "pinned"
    initial = target.snapshot
    with initial.check_out() as directory:
        (directory / "skills/main.md").write_text("NEXT")
        next_version = initial.commit(directory)
    tasks = tuple(Task(str(i), str(i), env="fixture") for i in range(30))
    algo.benchmark._splits = {**algo.benchmark._splits, "train": tasks}
    algo._task_duration_estimates = {str(i): float(30 - i) for i in range(30)}
    grade = algo.benchmark.grade
    graded = []

    def grade_repeat(rows):
        # A grader receives the original task IDs once each, never a collapsed
        # dictionary of both repeats or fabricated IDs that change the task.
        assert tuple(row.task for row in rows) == tasks
        graded.append(tuple(row.output for row in rows))
        return grade(rows)

    monkeypatch.setattr(algo.benchmark, "grade", grade_repeat)
    output = StringIO()

    async def scenario():
        saturated, release_fast = asyncio.Event(), asyncio.Event()
        second_started, release_tail = asyncio.Event(), asyncio.Event()
        calls = Counter()
        directories, exports, completions = [], [], []
        active = maximum = 0

        async def run(current, prompt, working_dir=None):
            repeat = current.config.trial_index
            key = (prompt, repeat)
            exports.append(current.files_dir)
            assert current.snapshot.id == initial.id
            assert current.config.marker == "pinned"
            assert (current.files_dir / "skills/main.md").read_text() == "SEED"
            history = working_dir / "conversation.txt"
            assert not history.exists()
            history.write_text(str(key))
            await release_fast.wait()
            if key == ("0", 0):
                await release_tail.wait()
            elif repeat == 1:
                second_started.set()
            await asyncio.sleep(0)
            assert history.read_text() == str(key)
            assert current.config.trial_index == repeat
            completions.append(key)
            return Trajectory(
                output=str(repeat),
                status="error" if key == ("1", 0) else "finished",
                error_message="fixture task error" if key == ("1", 0) else None,
                usage={"total_tokens": (repeat + 1) * 100},
            )

        async def environment(current, task):
            nonlocal active, maximum
            assert "trial_index" not in current.config
            current.config.trial_index = calls[task.task_id]
            calls[task.task_id] += 1
            active += 1
            maximum = max(maximum, active)
            if active == 8:
                saturated.set()
            try:
                with TemporaryDirectory(dir=tmp_path) as directory:
                    directory = Path(directory)
                    directories.append(directory)
                    return await current.run(task.question, working_dir=directory)
            finally:
                await asyncio.sleep(0)  # Environment cleanup occupies its slot.
                active -= 1

        monkeypatch.setattr(MiniAgent, "_run", run)
        target._env_runner = environment
        pending = asyncio.create_task(algo._measure(initial, "candidate"))
        try:
            await asyncio.wait_for(saturated.wait(), 2)
            target.update(next_version)
            target.config.marker = "changed outside the batch"
            release_fast.set()
            await asyncio.wait_for(second_started.wait(), 2)
            assert ("0", 0) not in completions
            release_tail.set()
            measurement = await asyncio.wait_for(pending, 5)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert maximum == 8 and active == 0
        assert calls == Counter({task.task_id: 2 for task in tasks})
        assert len(set(directories)) == len(set(exports)) == 60
        assert all(not p.exists() for p in directories + exports)
        assert measurement["score"] == 0.5
        assert measurement["cost"] == 150
        assert measurement["error_trials"] == 1
        assert measurement["cost_accounting"]["observed_tokens"] == 9000
        assert measurement["per_task"] == {task.task_id: [0, 1] for task in tasks}
        assert [row["trial"] for row in measurement["evidence"]] == [0] * 30 + [1] * 30
        batches = saved(algo, "candidate")
        assert len(batches) == 2
        assert {batch["snapshot_id"] for batch in batches} == {initial.id}
        assert [len(batch["trajectories"]) for batch in batches] == [30, 30]
        assert graded == [("0",) * 30, ("1",) * 30]
        assert target._execution.get() is None
        assert "trial_index" not in target.config

    with progress_session("log", stream=output):
        asyncio.run(scenario())
    log = output.getvalue()
    assert "repeats=2 | candidate | tasks" in log
    assert "60/60 (100%) active=0 queued=0 errors=1" in log
    assert "[repeat=1/2]" in log and "[repeat=2/2]" in log
    assert target.snapshot.id == next_version.id


def test_cancellation_waits_for_cleanup_in_both_repeats(monkeypatch, tmp_path):
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [], k=2)
    algo.target.config.concurrency = 8
    tasks = tuple(Task(str(i), str(i), env="fixture") for i in range(6))
    algo.benchmark._splits = {**algo.benchmark._splits, "train": tasks}
    output = StringIO()

    async def scenario():
        ready, never = asyncio.Event(), asyncio.Event()
        started, cleaned = [], []

        async def environment(current, task):
            key = (task.task_id, len(started))
            started.append(key)
            if len(started) == 8:
                ready.set()
            try:
                await never.wait()
            finally:
                await asyncio.sleep(0.01 if key[1] == 1 else 0)
                cleaned.append(key)

        algo.target._env_runner = environment
        pending = asyncio.create_task(algo._measure(algo.target.snapshot, "candidate"))
        await asyncio.wait_for(ready.wait(), 2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(started) == 8
        assert sorted(started) == sorted(cleaned)
        assert Counter(task for task, _ in started)["0"] == 2
        assert not saved(algo, "measurement")
        assert not any(
            job is not asyncio.current_task() and not job.done()
            for job in asyncio.all_tasks()
        )

    with progress_session("log", stream=output):
        asyncio.run(scenario())
    final = output.getvalue().splitlines()[-1]
    assert "state=interrupted" in final and "active=0" in final
    assert "cancelled=8" in final and "0/12" in final


@pytest.mark.parametrize("repeats,concurrency", [(1, 8), (2, 1), (3, 8)])
def test_repeat_counts_and_serial_execution_still_work(
    monkeypatch, tmp_path, repeats, concurrency
):
    algo, calls = setup(monkeypatch, tmp_path, lambda *_: [], k=repeats)
    algo.target.config.concurrency = concurrency
    measurement = asyncio.run(algo._measure(algo.target.snapshot, "baseline"))
    assert len(calls) == 2 * repeats
    assert len(saved(algo, "baseline")) == repeats
    assert measurement["per_task"] == {
        task.task_id: [0.5] * repeats for task in algo.benchmark.train
    }
    assert measurement["score"] == 0.5 and measurement["cost"] == 100
