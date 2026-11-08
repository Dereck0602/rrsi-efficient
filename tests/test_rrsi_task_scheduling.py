"""Historical durations change dispatch order without changing the experiment."""

import asyncio
import heapq
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_rrsi import change, saved, setup

from flashrsi.algorithms.rrsi._scheduling import execution_seconds, task_schedule
from flashrsi.core import Agent, Task, Trajectory
from flashrsi.core import agent as agent_module


def test_lpt_uses_all_repeats_and_improves_a_tail_heavy_queue():
    tasks = tuple(Task(str(i), str(i)) for i in range(30))
    durations = {str(i): 20.0 if i >= 26 else 1.0 for i in range(30)}
    fifo = task_schedule(tasks, 2, durations, "fifo")
    lpt = task_schedule(tasks, 2, durations, "longest_first")

    def makespan(schedule):
        slots = [0.0] * 8
        for job in schedule["jobs"]:
            end = heapq.heappop(slots) + durations[job["task_id"]]
            heapq.heappush(slots, end)
        return max(slots)

    assert makespan(lpt) < makespan(fifo)
    assert sorted(job["index"] for job in lpt["jobs"]) == list(range(60))
    assert {job["task_id"] for job in lpt["jobs"][:8]} == {"26", "27", "28", "29"}
    assert all(
        [job["trial"] for job in lpt["jobs"] if job["task_id"] == task.task_id]
        == [0, 1]
        for task in tasks
    )


def test_missing_history_uses_mean_and_ties_keep_canonical_order():
    tasks = tuple(Task(name, name) for name in ("fast", "unknown", "slow"))
    plan = task_schedule(tasks, 2, {"fast": 1, "slow": 9}, "longest_first")
    assert plan["unknown_task_estimate_seconds"] == 5
    assert [job["index"] for job in plan["jobs"]] == [2, 5, 1, 4, 0, 3]
    no_history = task_schedule(tasks, 2, {}, "longest_first")
    assert [job["index"] for job in no_history["jobs"]] == list(range(6))
    assert no_history["known_tasks"] == 0
    ties = task_schedule(tasks, 2, {task.task_id: 5 for task in tasks}, "longest_first")
    assert [job["index"] for job in ties["jobs"]] == list(range(6))


def test_siblings_share_previous_round_mean_including_rejected_candidates(
    monkeypatch, tmp_path
):
    markers = iter(["ONE", "TWO", "THREE", "FOUR"])

    def edit(prompt, directory):
        (directory / "skills/main.md").write_text(next(markers))
        return [change()]

    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        T=2,
        m=2,
        k=2,
        task_order="longest_first",
        outcomes={
            "SEED": (0.5, 100),
            "ONE": (1, 100),
            "TWO": (0, 100),
            "THREE": (1, 100),
            "FOUR": (1, 100),
        },
    )
    original = algo.target.rollout
    durations = {"SEED": (1, 10), "ONE": (20, 1), "TWO": (40, 3)}

    async def timed(tasks, *, snapshot):
        rows = await original(tasks, snapshot=snapshot)
        marker = snapshot.files["skills/main.md"].content.decode()
        times = durations.get(marker, (1, 1))
        return tuple(
            replace(
                row,
                messages=(
                    *row.messages,
                    {
                        "type": "TaskExecutionTiming",
                        "elapsed_seconds": times[int(row.task.task_id[-1])],
                    },
                ),
            )
            for row in rows
        )

    monkeypatch.setattr(algo.target, "rollout", timed)
    asyncio.run(algo.evolve())
    measurements = saved(algo, "measurement")
    assert len(measurements) == 5
    assert measurements[0]["scheduling"]["known_tasks"] == 0
    for measurement in measurements[1:3]:
        plan = measurement["scheduling"]
        assert [job["index"] for job in plan["jobs"]] == [1, 3, 0, 2]
        assert [job["estimated_seconds"] for job in plan["jobs"]] == [10, 10, 1, 1]
    for measurement in measurements[3:]:
        plan = measurement["scheduling"]
        assert [job["index"] for job in plan["jobs"]] == [0, 2, 1, 3]
        assert [job["estimated_seconds"] for job in plan["jobs"]] == [30, 30, 2, 2]
    assert any(
        row["variant"] == 1 and not row["accepted"]
        for row in saved(algo, "edit-history")
    )


def test_slot_timing_excludes_queue_wait_and_includes_error_cleanup(monkeypatch):
    clock = {"now": 0.0}
    monkeypatch.setattr(
        agent_module, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )

    class Probe(Agent):
        name = "timing-probe"

        async def _run(self, prompt, working_dir=None):
            raise AssertionError("environment handles this task")

    async def environment(current, task):
        clock["now"] += 3
        try:
            await asyncio.sleep(0)
            return Trajectory(
                status="error",
                error_message="fixture timeout",
                usage={"total_tokens": 17},
            )
        finally:
            clock["now"] += 7

    agent = Probe({"concurrency": 1})
    agent._env_runner = environment
    rows = asyncio.run(agent.rollout([Task("a", "a", env="fixture")] * 2))
    assert [execution_seconds(row) for row in rows] == [10, 10]
    assert all(
        row.status == "error" and row.usage["total_tokens"] == 17 for row in rows
    )


def test_legacy_harbor_timing_and_new_timing_precedence(tmp_path):
    record = {
        "task_name": "t",
        "task_checksum": "checksum",
        "started_at": "2026-11-06T00:00:00Z",
        "finished_at": "2026-11-06T00:10:00Z",
    }
    (tmp_path / "result.json").write_text(json.dumps(record))
    row = Trajectory(
        task=Task("t", "t"),
        messages=(
            {
                "type": "HarborVerifier",
                "task_checksum": "checksum",
                "artifacts": {"directory": str(tmp_path)},
            },
        ),
    )
    assert execution_seconds(row) == 600
    with_new = replace(
        row,
        messages=(
            *row.messages,
            {"type": "TaskExecutionTiming", "elapsed_seconds": 620},
        ),
    )
    assert execution_seconds(with_new) == 620
    record["task_name"] = "another-task"
    (tmp_path / "result.json").write_text(json.dumps(record))
    assert execution_seconds(row) is None
    record["task_name"] = "t"
    record["task_checksum"] = "different-checksum"
    (tmp_path / "result.json").write_text(json.dumps(record))
    assert execution_seconds(row) is None
    (tmp_path / "result.json").write_text("broken JSON")
    assert execution_seconds(row) is None
    (tmp_path / "result.json").unlink()
    assert execution_seconds(row) is None


@pytest.mark.parametrize("value", [None, True, -1, 0, float("nan"), float("inf"), "1"])
def test_invalid_timing_does_not_enter_priority_estimates(value):
    row = Trajectory(
        messages=({"type": "TaskExecutionTiming", "elapsed_seconds": value},)
    )
    assert execution_seconds(row) is None
    plan = task_schedule((Task("t", "t"),), 2, {"t": value}, "longest_first")
    assert plan["known_tasks"] == 0


@pytest.mark.parametrize("value", ["shortest_first", None, 8, True])
def test_invalid_policy_is_rejected_before_execution(monkeypatch, tmp_path, value):
    with pytest.raises(ValueError, match="task_order"):
        setup(monkeypatch, tmp_path, lambda *_: [], task_order=value)
