"""Ready candidates overlap evaluation without exchanging evidence or state."""

import asyncio
import json
import re
from collections import Counter
from contextvars import ContextVar
from dataclasses import replace

import pytest
from test_rrsi import Worker, change, saved, setup

from flashrsi.agents import MiniAgent
from flashrsi.algorithms.rrsi._pipeline import GENERATION
from flashrsi.core import Trajectory


def edit(prompt, directory):
    variant = int(re.search(r"candidate (\d+)", prompt)[1])
    (directory / "skills/main.md").write_text(f"C{variant}")
    return [change()]


@pytest.mark.parametrize("pipeline", [False, True])
def test_parallel_producers_use_frozen_context_and_pipeline_keeps_canonical_winner(
    monkeypatch, tmp_path, pipeline
):
    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        m=3,
        k=2,
        smoke_n=1,
        proposal_concurrency=2,
        shared_candidate_queue=True,
        pipeline_evaluation=pipeline,
    )
    algo.target.config.concurrency = 2
    algo.worker.config.marker = "frozen"
    algo.worker.config.llm.max_tokens = 16384
    tasks = tuple(replace(task, env="fixture") for task in algo.benchmark.train)
    algo.benchmark._splits = {**algo.benchmark._splits, "train": tasks}
    original_worker = Worker._run
    phase = ContextVar("test_phase", default="baseline")
    record = algo._record

    def observe(label, payload):
        if label == "candidate-dispatch":
            phase.set(payload["phase"])
        record(label, payload)

    monkeypatch.setattr(algo, "_record", observe)

    async def scenario():
        both_generating = asyncio.Event()
        release_zero = asyncio.Event()
        third_generating = asyncio.Event()
        full_started = asyncio.Event()
        release_full = asyncio.Event()
        active_workers = max_workers = active_targets = max_targets = 0
        contexts, directories, calls = [], [], Counter()

        async def worker_run(current, prompt, working_dir=None):
            nonlocal active_workers, max_workers
            generation = GENERATION.get()
            if not prompt.startswith("[RRSI: propose]"):
                if generation is not None:
                    assert current.config.marker == "frozen"
                return await original_worker(current, prompt, working_dir)
            variant = generation.variant
            active_workers += 1
            max_workers = max(max_workers, active_workers)
            directories.append(working_dir)
            assert (working_dir / "skills/main.md").read_text() == "SEED"
            reference = prompt.split("`")[1]
            contexts.append((working_dir / reference).read_bytes())
            current.config.local_variant = variant
            try:
                if variant == 2:
                    third_generating.set()
                if active_workers == 2:
                    both_generating.set()
                await both_generating.wait()
                if variant == 0:
                    await release_zero.wait()
                assert current.config.marker == "frozen"
                assert current.config.llm.max_tokens == 16384
                assert current.config.local_variant == variant
                return await original_worker(current, prompt, working_dir)
            finally:
                await asyncio.sleep(0)
                active_workers -= 1

        async def target_run(current, prompt, working_dir=None):
            marker = (current.files_dir / "skills/main.md").read_text()
            calls[(marker, prompt, phase.get())] += 1
            if marker != "SEED" and phase.get() == "candidate":
                full_started.set()
                await release_full.wait()
            return Trajectory(
                output="0.5" if marker == "SEED" else "1", usage={"total_tokens": 100}
            )

        async def environment(current, task):
            nonlocal active_targets, max_targets
            active_targets += 1
            max_targets = max(max_targets, active_targets)
            try:
                return await current.run(task.question)
            finally:
                await asyncio.sleep(0.001)
                active_targets -= 1

        monkeypatch.setattr(Worker, "_run", worker_run)
        monkeypatch.setattr(MiniAgent, "_run", target_run)
        algo.target._env_runner = environment
        pending = asyncio.create_task(algo.evolve())
        try:
            await asyncio.wait_for(both_generating.wait(), 3)
            algo.worker.config.marker = "changed outside generation"
            algo.worker.config.llm.max_tokens = 1
            await asyncio.wait_for(third_generating.wait(), 3)
            if pipeline:
                await asyncio.wait_for(full_started.wait(), 3)
                assert not release_zero.is_set()
                assert not saved(algo, "edit-history")
                assert len(saved(algo, "measurement")) == 1
            else:
                assert not full_started.is_set()
                assert not saved(algo, "candidate")
            release_zero.set()
            release_full.set()
            result = await asyncio.wait_for(pending, 5)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert result.files["skills/main.md"].content == b"C0"
        assert max_workers == 2 and active_workers == 0
        assert max_targets == 2 and active_targets == 0
        assert len(set(contexts)) == 1
        assert json.loads(contexts[0])["history"] == []
        assert len(set(directories)) == 3 and all(not d.exists() for d in directories)
        assert not list((algo._run_dir / "working").iterdir())
        for marker in ("C0", "C1", "C2"):
            assert sum(count for (m, _, _), count in calls.items() if m == marker) == 5
        history = saved(algo, "edit-history")
        assert [row["variant"] for row in history] == [0, 1, 2]
        assert [row["outcome"] for row in history] == ["ACCEPTED", "LOST", "LOST"]
        assert saved(algo, "candidate-ready")[0]["variant"] == 1
        assert len(saved(algo, "candidate")) == 6
        assert len(saved(algo, "measurement")) == 4
        worker_records = [row for row in saved(algo, "worker") if "variant" in row]
        assert Counter(row["variant"] for row in worker_records) == {0: 2, 1: 2, 2: 2}
        assert all(row["step"] == 0 for row in worker_records)
        assert algo.worker._execution.get() is None
        assert algo.target._execution.get() is None
        assert GENERATION.get() is None
        assert "local_variant" not in algo.worker.config

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["cancel", "worker", "target", "target_cancel"])
def test_pipeline_failure_cleans_worker_and_target_before_return(
    monkeypatch, tmp_path, failure
):
    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        m=2,
        k=2,
        proposal_concurrency=2,
        pipeline_evaluation=True,
        shared_candidate_queue=True,
    )
    algo.target.config.concurrency = 2
    tasks = tuple(replace(task, env="fixture") for task in algo.benchmark.train)
    algo.benchmark._splits = {**algo.benchmark._splits, "train": tasks}
    original_worker = Worker._run

    async def scenario():
        worker_started, target_started, release_error = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        started, cleaned, directories = [], [], []

        async def worker_run(current, prompt, working_dir=None):
            if prompt.startswith("[RRSI: propose]") and GENERATION.get().variant == 0:
                started.append("worker")
                directories.append(working_dir)
                worker_started.set()
                try:
                    await release_error.wait()
                    if failure == "worker":
                        raise RuntimeError("fixture worker failure")
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0.01)
                    cleaned.append("worker")
            return await original_worker(current, prompt, working_dir)

        async def environment(current, task):
            marker = current.snapshot.files["skills/main.md"].content.decode()
            if marker == "SEED":
                return await current.run(task.question)
            key = f"target-{len(started)}"
            started.append(key)
            target_started.set()
            try:
                await release_error.wait()
                if failure == "target":
                    return Trajectory(
                        messages=({"type": "RunConfig", "snapshot_id": "wrong"},)
                    )
                if failure == "target_cancel":
                    raise asyncio.CancelledError()
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.append(key)

        monkeypatch.setattr(Worker, "_run", worker_run)
        algo.target._env_runner = environment
        pending = asyncio.create_task(algo.evolve())
        try:
            await asyncio.wait_for(worker_started.wait(), 3)
            await asyncio.wait_for(target_started.wait(), 3)
            if failure == "cancel":
                pending.cancel()
            release_error.set()
            expected = (
                asyncio.CancelledError
                if failure in {"cancel", "target_cancel"}
                else RuntimeError
                if failure == "worker"
                else ValueError
            )
            with pytest.raises(expected):
                await asyncio.wait_for(pending, 3)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        assert sorted(started) == sorted(cleaned)
        assert all(not d.exists() for d in directories)
        assert not list((algo._run_dir / "working").iterdir())
        assert not saved(algo, "candidate") and not saved(algo, "edit-history")
        assert not any(
            t is not asyncio.current_task() and not t.done()
            for t in asyncio.all_tasks()
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, True, 1.5, None])
def test_invalid_proposal_concurrency_rejected(monkeypatch, tmp_path, value):
    with pytest.raises(ValueError, match="proposal_concurrency"):
        setup(monkeypatch, tmp_path, edit, proposal_concurrency=value)


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_pipeline_requires_boolean(monkeypatch, tmp_path, value):
    with pytest.raises(ValueError, match="pipeline_evaluation"):
        setup(monkeypatch, tmp_path, edit, pipeline_evaluation=value)


def test_pipeline_requires_shared_queue(monkeypatch, tmp_path):
    with pytest.raises(ValueError, match="requires shared_candidate_queue"):
        setup(monkeypatch, tmp_path, edit, pipeline_evaluation=True)


@pytest.mark.parametrize("failure", ["compile", "smoke"])
def test_failed_gate_never_enters_full_evaluation(monkeypatch, tmp_path, failure):
    def candidate_edit(prompt, directory):
        if "candidate 0." in prompt and failure == "compile":
            (directory / "tools/runtime.py").write_text("def broken(:\n")
            return [change("control_flow", "tools/runtime.py")]
        return edit(prompt, directory)

    algo, _ = setup(
        monkeypatch,
        tmp_path,
        candidate_edit,
        m=2,
        smoke_n=1,
        proposal_concurrency=2,
        shared_candidate_queue=True,
        pipeline_evaluation=True,
    )
    algo.target.config.concurrency = 2

    async def run(current, prompt, working_dir=None):
        marker = (current.files_dir / "skills/main.md").read_text()
        bad = marker == "C0" and failure == "smoke"
        return Trajectory(
            output="0.5" if marker == "SEED" else "1",
            usage={"total_tokens": 100},
            status="error" if bad else "finished",
            error_message="fixture smoke failure" if bad else None,
        )

    monkeypatch.setattr(MiniAgent, "_run", run)
    result = asyncio.run(algo.evolve())
    assert result.files["skills/main.md"].content == b"C1"
    assert [r["variant"] for r in saved(algo, "candidate-queue")] == [1]
    history = saved(algo, "edit-history")
    assert history[0]["candidate_id"] is None
    assert history[0]["outcome"] == "REJECTED"
    assert history[1]["outcome"] == "ACCEPTED"
    assert len(saved(algo, "candidate")) == 1


def test_single_producer_releases_slot_while_its_evaluation_runs(monkeypatch, tmp_path):
    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        m=2,
        proposal_concurrency=1,
        shared_candidate_queue=True,
        pipeline_evaluation=True,
    )
    algo.target.config.concurrency = 2
    phase = ContextVar("single_producer_phase", default="baseline")
    record = algo._record

    def observe(label, payload):
        if label == "candidate-dispatch":
            phase.set(payload["phase"])
        record(label, payload)

    monkeypatch.setattr(algo, "_record", observe)
    original_worker = Worker._run

    async def scenario():
        evaluating, generated_next = asyncio.Event(), asyncio.Event()

        async def worker_run(current, prompt, working_dir=None):
            if prompt.startswith("[RRSI: propose]") and GENERATION.get().variant == 1:
                await evaluating.wait()
                generated_next.set()
            return await original_worker(current, prompt, working_dir)

        async def target_run(current, prompt, working_dir=None):
            marker = (current.files_dir / "skills/main.md").read_text()
            if marker == "C0" and phase.get() == "candidate":
                evaluating.set()
                await generated_next.wait()
            return Trajectory(
                output="0.5" if marker == "SEED" else "1", usage={"total_tokens": 100}
            )

        monkeypatch.setattr(Worker, "_run", worker_run)
        monkeypatch.setattr(MiniAgent, "_run", target_run)
        result = await asyncio.wait_for(algo.evolve(), 3)
        assert generated_next.is_set()
        assert result.files["skills/main.md"].content == b"C0"

    asyncio.run(scenario())
