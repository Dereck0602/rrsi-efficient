"""Cross-candidate scheduling preserves harness identity and RRSI decisions."""

import asyncio
from collections import Counter
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from test_rrsi import change, saved, setup

from flashrsi._progress import progress_session
from flashrsi.agents import Codex, MiniAgent
from flashrsi.algorithms.rrsi._candidate_queue import (
    EvaluationQueue,
    candidate_schedule,
    collect_results,
    execute_candidates,
)
from flashrsi.algorithms.rrsi._scheduling import execution_seconds
from flashrsi.core import Task, Trajectory
from flashrsi.core import agent as agent_module


def versions(algo, *markers):
    result = []
    for marker in markers:
        with algo.target.snapshot.check_out() as directory:
            (directory / "skills/main.md").write_text(marker)
            result.append(algo.target.snapshot.commit(directory))
    return result


def proposals(snapshots):
    return [(snapshot, [change()], "fixture diff", "") for snapshot in snapshots]


@pytest.mark.parametrize("policy", ["fifo", "longest_first"])
def test_queue_isolates_versions_repeats_and_config_with_global_eight_slots(
    monkeypatch, tmp_path, policy
):
    algo, _ = setup(
        monkeypatch,
        tmp_path,
        lambda *_: [],
        k=2,
        m=2,
        shared_candidate_queue=True,
        task_order=policy,
    )
    target = algo.target
    target.config.concurrency = 8
    target.config.marker = "frozen"
    a, b, c = versions(algo, "A", "B", "C")
    tasks = tuple(
        Task(str(i), str(i), env="fixture", metadata={"task_checksum": f"hash-{i}"})
        for i in range(30)
    )
    algo.benchmark._splits = {**algo.benchmark._splits, "train": tasks}
    algo._task_duration_estimates = {str(i): float(30 - i) for i in range(30)}
    graded = []
    grade = algo.benchmark.grade

    def grade_repeat(rows):
        assert tuple(row.task for row in rows) == tasks
        assert len({row.messages[0]["snapshot_id"] for row in rows}) == 1
        graded.append(tuple(row.output for row in rows))
        return grade(rows)

    monkeypatch.setattr(algo.benchmark, "grade", grade_repeat)
    output = StringIO()

    async def scenario():
        ready, fast, tail = asyncio.Event(), asyncio.Event(), asyncio.Event()
        b_finished = asyncio.Event()
        calls = Counter()
        completed, exports, directories = [], [], []
        active = maximum = 0

        async def run(current, prompt, working_dir=None):
            marker = (current.files_dir / "skills/main.md").read_text()
            assert marker == ("A" if current.snapshot.id == a.id else "B")
            assert current.config.marker == "frozen"
            exports.append(current.files_dir)
            repeat = current.config.trial_index
            key = (marker, prompt, repeat)
            state = working_dir / "state"
            assert not state.exists()
            state.write_text(str(key))
            await fast.wait()
            if key == ("A", "0", 0):
                await tail.wait()
            else:
                await asyncio.sleep(0)
            assert state.read_text() == str(key)
            assert current.config.trial_index == repeat
            assert current.snapshot.id == (a.id if marker == "A" else b.id)
            completed.append(key)
            if sum(item[0] == "B" for item in completed) == 60:
                b_finished.set()
            return Trajectory(
                output="1" if marker == "A" else "0",
                usage={"total_tokens": 100 if marker == "A" else 300},
            )

        async def environment(current, task):
            nonlocal active, maximum
            assert "trial_index" not in current.config
            key = (current.snapshot.id, task.task_id)
            current.config.trial_index = calls[key]
            calls[key] += 1
            active += 1
            maximum = max(maximum, active)
            if active == 8:
                ready.set()
            try:
                with TemporaryDirectory(dir=tmp_path) as directory:
                    directory = Path(directory)
                    directories.append(directory)
                    row = await current.run(task.question, directory)
                    return replace(
                        row,
                        messages=(
                            *row.messages,
                            {
                                "type": "HarborVerifier",
                                "snapshot_id": current.snapshot.id,
                                "task_id": task.task_id,
                                "task_checksum": task.metadata["task_checksum"],
                            },
                        ),
                    )
            finally:
                await asyncio.sleep(0)
                active -= 1

        monkeypatch.setattr(MiniAgent, "_run", run)
        target._env_runner = environment
        pending = asyncio.create_task(algo._measure_candidates(proposals([a, b]), 3))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            target.update(c)
            target.config.marker = "changed outside queue"
            target.config.concurrency = 30
            algo._task_duration_estimates = {str(i): float(i) for i in range(30)}
            fast.set()
            await asyncio.wait_for(b_finished.wait(), 5)
            assert ("A", "0", 0) not in completed
            assert not graded  # Candidate B finishing early must not select it.
            tail.set()
            measurements = await asyncio.wait_for(pending, 5)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert active == 0 and maximum == 8
        assert calls == Counter(
            {(s.id, task.task_id): 2 for s in (a, b) for task in tasks}
        )
        assert len(exports) == len(set(exports)) == 120
        assert len(directories) == len(set(directories)) == 120
        assert all(not p.exists() for p in directories + exports)
        for variant, reward, cost in ((0, 1, 100), (1, 0, 300)):
            row = measurements[variant]
            assert row["score"] == reward and row["cost"] == cost
            assert row["per_task"] == {task.task_id: [reward] * 2 for task in tasks}
            assert row["cost_accounting"]["observed_tokens"] == cost * 60
            assert [x["trial"] for x in row["evidence"]] == [0] * 30 + [1] * 30
        assert graded == [("1",) * 30] * 2 + [("0",) * 30] * 2
        records = saved(algo, "candidate")
        assert [record["snapshot_id"] for record in records] == [a.id] * 2 + [b.id] * 2
        assert all(len(record["trajectories"]) == 30 for record in records)
        queue = saved(algo, "candidate-queue")[0]
        assert queue["concurrency"] == 8 and len(queue["jobs"]) == 120
        assert [job["variant"] for job in queue["jobs"]] == [0, 1] * 60
        assert queue["jobs"][0]["estimated_seconds"] == 30
        assert target._execution.get() is None
        assert "trial_index" not in target.config

    with progress_session("log", stream=output):
        asyncio.run(scenario())
    assert "120/120 (100%) active=0 queued=0" in output.getvalue()
    assert target.snapshot.id == c.id


@pytest.mark.parametrize("shared", [False, True])
def test_selection_and_attribution_ignore_completion_order_and_failed_candidates(
    monkeypatch, tmp_path, shared
):
    algo, _ = setup(
        monkeypatch, tmp_path, lambda *_: [], k=2, m=4, shared_candidate_queue=shared
    )
    algo.target.config.concurrency = 4
    a, b, bad = versions(algo, "A", "B", "BAD")
    original = MiniAgent._run

    async def run(current, prompt, working_dir=None):
        marker = (current.files_dir / "skills/main.md").read_text()
        if marker == "SEED":
            return await original(current, prompt, working_dir)
        await asyncio.sleep(0.005 if marker == "A" else 0)
        return Trajectory(
            output="1",
            usage={"total_tokens": 100},
            status="error" if marker == "BAD" else "finished",
            error_message="fixture timeout" if marker == "BAD" else None,
        )

    monkeypatch.setattr(MiniAgent, "_run", run)

    async def scenario():
        parent = algo.target.snapshot
        incumbent = await algo._measure(parent, "baseline")
        history = []
        winner, measured = await algo._select(
            parent,
            incumbent,
            [
                proposals([bad])[0],
                (None, [], "", "worker budget exhausted"),
                *proposals([a, b]),
            ],
            {"step": 0, "best_score": 0.5, "delta": 0.02},
            history,
        )
        assert winner.id == a.id
        assert measured["score"] == 1 and measured["cost"] == 100
        assert [row["outcome"] for row in history] == [
            "REJECTED",
            "REJECTED",
            "ACCEPTED",
            "LOST",
        ]
        assert history[0]["reason"] == "too many failed trials"
        assert history[1]["reason"] == "worker budget exhausted"
        assert history[2]["attribution"] == history[3]["attribution"]
        assert algo.target.snapshot.id == parent.id  # Promotion is still deferred.
        assert len(saved(algo, "measurement")) == 4
        assert len(saved(algo, "candidate")) == 6

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [True, False])
def test_cancel_or_identity_failure_awaits_cleanup_of_all_active_jobs(
    monkeypatch, tmp_path, cancel
):
    algo, _ = setup(
        monkeypatch, tmp_path, lambda *_: [], k=2, m=2, shared_candidate_queue=True
    )
    algo.target.config.concurrency = 8
    a, b = versions(algo, "A", "B")
    tasks = tuple(Task(str(i), str(i), env="fixture") for i in range(6))
    algo.benchmark._splits = {**algo.benchmark._splits, "train": tasks}

    async def scenario():
        ready, release = asyncio.Event(), asyncio.Event()
        started, cleaned = [], []

        async def environment(current, task):
            index = len(started)
            started.append((current.snapshot.id, task.task_id))
            if len(started) == 8:
                ready.set()
            try:
                if index == 0 and not cancel:
                    await release.wait()
                    return Trajectory(
                        messages=({"type": "RunConfig", "snapshot_id": "wrong"},)
                    )
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.append(index)

        algo.target._env_runner = environment
        pending = asyncio.create_task(algo._measure_candidates(proposals([a, b]), 0))
        await asyncio.wait_for(ready.wait(), 2)
        if cancel:
            pending.cancel()
        else:
            release.set()
        with pytest.raises(asyncio.CancelledError if cancel else ValueError):
            await asyncio.wait_for(pending, 2)
        assert len(started) == 8 and sorted(cleaned) == list(range(8))
        assert not saved(algo, "measurement")
        assert not saved(algo, "candidate")
        assert not any(
            task is not asyncio.current_task() and not task.done()
            for task in asyncio.all_tasks()
        )
        assert algo.target._execution.get() is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "duplicate",
        "unknown",
        "task",
        "checksum",
        "snapshot",
        "no_config",
        "verifier",
    ],
)
def test_mixed_or_incomplete_results_fail_closed(monkeypatch, tmp_path, fault):
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [])
    a, b = versions(algo, "A", "B")
    task = Task("same", "same", metadata={"task_checksum": "expected"})
    jobs, _ = candidate_schedule(((0, a), (1, b)), (task,), 2, {}, "fifo", 4)
    completed = [
        (
            job.key,
            Trajectory(
                task=task,
                messages=(
                    {"type": "RunConfig", "snapshot_id": job.snapshot.id},
                    {
                        "type": "HarborVerifier",
                        "snapshot_id": job.snapshot.id,
                        "task_id": task.task_id,
                        "task_checksum": "expected",
                    },
                ),
            ),
        )
        for job in jobs
    ]
    key, row = completed[0]
    if fault == "missing":
        completed.pop()
    elif fault == "duplicate":
        completed.append(completed[0])
    elif fault == "unknown":
        completed[0] = ((99, *key[1:]), row)
    elif fault in {"task", "checksum"}:
        completed[0] = (
            key,
            replace(
                row,
                task=replace(
                    task,
                    **(
                        {"task_id": "other"}
                        if fault == "task"
                        else {"metadata": {"task_checksum": "wrong"}}
                    ),
                ),
            ),
        )
    elif fault == "no_config":
        completed[0] = (key, replace(row, messages=row.messages[1:]))
    elif fault == "snapshot":
        completed[0] = (
            key,
            replace(
                row,
                messages=(
                    {"type": "RunConfig", "snapshot_id": b.id},
                    *row.messages[1:],
                ),
            ),
        )
    else:
        completed[0] = (
            key,
            replace(
                row,
                messages=(
                    row.messages[0],
                    {**row.messages[1], "task_checksum": "wrong"},
                ),
            ),
        )
    with pytest.raises(ValueError, match="shared candidate queue"):
        collect_results(jobs, completed)


def test_schedule_freezes_lpt_estimates_and_interleaves_duplicate_snapshots(
    monkeypatch, tmp_path
):
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [])
    snapshot = algo.target.snapshot
    tasks = tuple(Task(name, name) for name in ("fast", "unknown", "slow"))
    candidates = ((0, snapshot), (2, snapshot))
    jobs, plan = candidate_schedule(
        candidates, tasks, 2, {"fast": 1, "slow": 9}, "longest_first", 1
    )
    assert [job.task.task_id for job in jobs] == ["slow"] * 4 + ["unknown"] * 4 + [
        "fast"
    ] * 4
    assert [job.variant for job in jobs] == [0, 2] * 6
    assert [job.trial for job in jobs] == [0, 0, 1, 1] * 3
    assert [job.estimated_seconds for job in jobs] == [9] * 4 + [5] * 4 + [1] * 4
    assert plan["unknown_task_estimate_seconds"] == 5
    assert len({job.key for job in jobs}) == 12
    fifo, _ = candidate_schedule(candidates, tasks, 2, {}, "longest_first", 1)
    assert [job.index for job in fifo] == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5]
    for invalid_tasks, invalid_candidates in (
        ((tasks[0], tasks[0]), candidates),
        (tasks, candidates * 2),
    ):
        with pytest.raises(ValueError, match="requires unique"):
            candidate_schedule(invalid_candidates, invalid_tasks, 2, {}, "fifo", 1)


def test_shared_queue_wait_excluded_and_cleanup_included_in_timing(
    monkeypatch, tmp_path
):
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [])
    algo.target.config.concurrency = 1
    a, b = versions(algo, "A", "B")
    clock = {"now": 0.0}
    monkeypatch.setattr(
        agent_module, "time", SimpleNamespace(monotonic=lambda: clock["now"])
    )

    async def environment(current, task):
        clock["now"] += 3
        try:
            await asyncio.sleep(0)
            return await current.run(task.question)
        finally:
            clock["now"] += 7

    algo.target._env_runner = environment
    jobs, _ = candidate_schedule(
        ((0, a), (1, b)), (Task("t", "t", env="fixture"),), 2, {}, "fifo", 0
    )
    rows = asyncio.run(execute_candidates(algo.target, jobs))
    assert [execution_seconds(row) for batch in rows.values() for row in batch] == [
        10
    ] * 4
    assert clock["now"] == 40


def test_existing_adapter_rollout_hook_is_preserved(monkeypatch, tmp_path):
    algo, calls = setup(monkeypatch, tmp_path, lambda *_: [], cls=Codex)
    snapshot = algo.target.snapshot
    original = Codex._run

    async def run(current, prompt, working_dir=None):
        assert current.config._codex_evaluation is True
        return await original(current, prompt, working_dir)

    monkeypatch.setattr(Codex, "_run", run)
    jobs, _ = candidate_schedule(
        ((0, snapshot), (1, snapshot)), algo.benchmark.train, 2, {}, "fifo", 0
    )
    result = asyncio.run(execute_candidates(algo.target, jobs))
    assert len(calls) == 8 and list(result) == [0, 1]
    assert "_codex_evaluation" not in algo.target.config


@pytest.mark.parametrize("value", [None, 0, 1, "true"])
def test_shared_queue_flag_requires_boolean(monkeypatch, tmp_path, value):
    with pytest.raises(ValueError, match="shared_candidate_queue"):
        setup(monkeypatch, tmp_path, lambda *_: [], shared_candidate_queue=value)


def test_all_failed_proposals_keep_incumbent_without_starting_queue(
    monkeypatch, tmp_path
):
    algo, calls = setup(
        monkeypatch, tmp_path, lambda *_: [], shared_candidate_queue=True
    )

    async def scenario():
        parent = algo.target.snapshot
        incumbent = await algo._measure(parent, "baseline")
        calls.clear()
        history = []
        winner, measured = await algo._select(
            parent,
            incumbent,
            [(None, [], "", "worker budget exhausted")],
            {"step": 0, "best_score": 0.5, "delta": 0.02},
            history,
        )
        assert winner is parent and measured is incumbent
        assert not calls and not saved(algo, "candidate-queue")
        assert history[0]["outcome"] == "REJECTED"

    asyncio.run(scenario())


def test_shared_and_serial_measurements_have_identical_scores_costs_and_evidence(
    monkeypatch, tmp_path
):
    algo, _ = setup(
        monkeypatch,
        tmp_path,
        lambda *_: [],
        k=2,
        m=2,
        cost_policy="estimate",
        cost_estimate_max_frac=0.25,
    )
    algo.target.config.concurrency = 3
    a, b = versions(algo, "A", "B")
    counts = Counter()

    async def run(current, prompt, working_dir=None):
        marker = (current.files_dir / "skills/main.md").read_text()
        key = (marker, prompt)
        repeat = counts[key]
        counts[key] += 1
        await asyncio.sleep(0.002 if marker == "A" else 0)
        return Trajectory(
            output="1" if prompt == "train-0" else "0.5",
            usage={} if prompt == "train-1" and repeat == 1 else {"total_tokens": 100},
            messages=({"role": "assistant", "content": "some measured output"},),
        )

    monkeypatch.setattr(MiniAgent, "_run", run)

    async def scenario():
        serial = [await algo._measure(snapshot, "candidate") for snapshot in (a, b)]
        counts.clear()
        shared = await algo._measure_candidates(proposals([a, b]), 0)

        # Only scheduling metadata and measured wall time should differ for
        # these fixed outputs; include cost-estimate eligibility and evidence.
        def normalized(measurement):
            measurement = {k: v for k, v in measurement.items() if k != "scheduling"}
            measurement["evidence"] = [
                {
                    **{k: v for k, v in row.items() if k != "execution_seconds"},
                    "trajectory": {
                        **row["trajectory"],
                        "messages": [
                            m
                            for m in row["trajectory"]["messages"]
                            if m.get("type") != "TaskExecutionTiming"
                        ],
                    },
                }
                for row in measurement["evidence"]
            ]
            return measurement

        assert [normalized(m) for m in serial] == [
            normalized(shared[i]) for i in range(2)
        ]
        assert shared[0]["cost_accounting"]["estimate"]["eligible"] is True

    asyncio.run(scenario())


def test_later_ready_smoke_then_long_task_precede_waiting_short_trials(
    monkeypatch, tmp_path
):
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [])
    algo.target.config.concurrency = 1
    snapshot = algo.target.snapshot
    tasks = (Task("long", "long"), Task("short", "short"))
    a, _ = candidate_schedule(
        ((0, snapshot),), tasks, 1, {"long": 10, "short": 1}, "longest_first", 0
    )
    b, _ = candidate_schedule(
        ((1, snapshot),), tasks, 1, {"long": 10, "short": 1}, "longest_first", 0
    )
    smoke = (replace(b[0], phase="smoke"),)

    async def scenario():
        first_started, release = asyncio.Event(), asyncio.Event()
        dispatched = []
        original = algo.target.rollout

        async def rollout(tasks, *, snapshot):
            if len(dispatched) == 1:
                first_started.set()
                await release.wait()
            return await original(tasks, snapshot=snapshot)

        monkeypatch.setattr(algo.target, "rollout", rollout)
        async with EvaluationQueue(
            algo.target,
            on_dispatch=lambda j: dispatched.append(
                (j.phase, j.variant, j.task.task_id)
            ),
        ) as queue:
            first = asyncio.create_task(queue.submit(a))
            await asyncio.wait_for(first_started.wait(), 2)
            second = asyncio.create_task(queue.submit(b))
            gate = asyncio.create_task(queue.submit(smoke))
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(first, second, gate), 2)
        assert dispatched == [
            ("candidate", 0, "long"),
            ("smoke", 1, "long"),
            ("candidate", 1, "long"),
            ("candidate", 0, "short"),
            ("candidate", 1, "short"),
        ]

    asyncio.run(scenario())
