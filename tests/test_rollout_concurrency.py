"""Parallel tasks retain batch identity, input order, and awaited cleanup."""

import asyncio
import json
from io import StringIO

import pytest

from flashrsi._progress import progress_session
from flashrsi.core import Agent, Task, Trajectory


class Probe(Agent):
    name = "concurrency-probe"

    async def _run(self, prompt, working_dir=None):
        await asyncio.sleep(0)
        return Trajectory(output=prompt)


def test_bounded_environment_rollouts_pin_snapshot_and_isolate_configs(tmp_path):
    (tmp_path / "components.yaml").write_text("prompt: ['prompt.md']\n")
    (tmp_path / "prompt.md").write_text("initial")
    agent = Probe({"source_dir": tmp_path, "concurrency": 8, "marker": "initial"})
    agent.set_scope({"prompt": "rw"})
    baseline = agent.snapshot
    with baseline.check_out() as directory:
        (directory / "prompt.md").write_text("next")
        candidate = baseline.commit(directory)
    tasks = tuple(Task(str(i), str(i), env="fixture") for i in range(24))

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        active = maximum = 0

        async def environment(current, task):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == 8:
                started.set()
            assert "task_marker" not in current.config
            current.config.task_marker = task.task_id
            try:
                await release.wait()
                await asyncio.sleep((8 - int(task.task_id) % 8) / 1000)
                return Trajectory(
                    output=json.dumps(
                        {
                            "snapshot": current.snapshot.id,
                            "marker": current.config.marker,
                            "task": current.config.task_marker,
                        }
                    )
                )
            finally:
                active -= 1

        agent._env_runner = environment
        pending = asyncio.create_task(agent.rollout(tasks))
        await asyncio.wait_for(started.wait(), timeout=2)
        agent.update(candidate)
        agent.config.marker = "next"
        release.set()
        results = await pending
        assert maximum == 8
        assert active == 0
        assert tuple(row.task for row in results) == tasks
        for task, result in zip(tasks, results, strict=True):
            assert json.loads(result.output) == {
                "snapshot": baseline.id,
                "marker": "initial",
                "task": task.task_id,
            }
        assert agent._execution.get() is None

    asyncio.run(scenario())
    assert agent.snapshot.id == candidate.id
    assert agent.config.marker == "next"
    assert "task_marker" not in agent.config


@pytest.mark.parametrize("cancel", [False, True])
def test_failure_or_cancellation_awaits_all_active_cleanup(cancel):
    agent = Probe({"concurrency": 3})
    output = StringIO()

    async def scenario():
        started, cleaned = [], []
        ready = asyncio.Event()
        never = asyncio.Event()

        async def environment(current, task):
            started.append(task.task_id)
            if len(started) == 3:
                ready.set()
            try:
                await ready.wait()
                if not cancel and task.task_id == "0":
                    raise RuntimeError("broken environment")
                await never.wait()
            finally:
                # A fast child's cancellation must not interrupt a slower
                # sibling while it is cleaning up its native environment.
                await asyncio.sleep(0.02 if task.task_id == "1" else 0)
                cleaned.append(task.task_id)

        agent._env_runner = environment
        pending = asyncio.create_task(
            agent.rollout(Task(str(i), "question", env="fixture") for i in range(10))
        )
        await asyncio.wait_for(ready.wait(), timeout=2)
        if cancel:
            pending.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await pending
        assert sorted(cleaned) == sorted(started)
        assert not any(
            task is not asyncio.current_task() and not task.done()
            for task in asyncio.all_tasks()
        )
        assert agent._execution.get() is None

    with progress_session("log", stream=output):
        asyncio.run(scenario())
    final = output.getvalue().splitlines()[-1]
    assert "state=interrupted" in final
    assert "active=0" in final
    assert "10/10" not in final
    assert "cancelled=0" not in final


@pytest.mark.parametrize("concurrency", [1, 8])
def test_plain_tasks_and_empty_batches(concurrency):
    agent = Probe({"concurrency": concurrency})
    tasks = tuple(Task(str(i), str(i)) for i in range(5))
    results = asyncio.run(agent.rollout(tasks))
    assert [row.output for row in results] == [task.question for task in tasks]
    assert asyncio.run(agent.rollout([])) == ()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "8"])
def test_invalid_concurrency_fails_before_execution(value):
    with pytest.raises(ValueError, match="concurrency"):
        asyncio.run(Probe({"concurrency": value}).rollout([Task("a", "a")]))
