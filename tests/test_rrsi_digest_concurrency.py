"""Parallel investigations isolate Worker contexts, files and cancellation."""

import asyncio
from io import StringIO

import pytest
from test_rrsi import Worker, saved, setup

from flashrsi._progress import progress_session


def requests_and_traces(count):
    requests = [{"task_id": str(i)} for i in range(count)]
    traces = {
        str(i): {
            "task_id": str(i),
            "evidence": {"task_id": str(i), "trajectory": {"output": str(i)}},
        }
        for i in range(count)
    }
    return requests, traces


@pytest.mark.parametrize("limit", [1, 8])
def test_digests_are_bounded_isolated_and_return_in_request_order(
    monkeypatch, tmp_path, limit
):
    import json

    original = Worker._run
    active = maximum = 0
    directories, completions = [], []

    async def run(self, prompt, working_dir=None):
        nonlocal active, maximum
        task = json.loads((working_dir / "trace.json").read_text())["task_id"]
        assert "digest_marker" not in self.config
        self.config.digest_marker = task
        directories.append(working_dir)
        active += 1
        maximum = max(maximum, active)
        try:
            await asyncio.sleep((8 - int(task) % 8) / 1000)
            assert self.config.digest_marker == task
            completions.append(task)
            return await original(self, prompt, working_dir)
        finally:
            active -= 1

    monkeypatch.setattr(Worker, "_run", run)
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [], digest_concurrency=limit)
    requests, traces = requests_and_traces(16)
    output = StringIO()
    with progress_session("log", stream=output):
        results = asyncio.run(algo._digest_many(requests, traces))
    assert [row["task_id"] for row in results] == [str(i) for i in range(16)]
    assert maximum == limit and active == 0
    assert len(set(directories)) == 16
    assert all(not p.exists() for p in directories)
    assert "digest_marker" not in algo.worker.config
    assert algo.worker._execution.get() is None
    assert len(saved(algo, "worker")) == 16
    assert "digests [####################] 16/16 (100%) active=0" in output.getvalue()
    if limit == 8:
        assert completions != [str(i) for i in range(16)]


@pytest.mark.parametrize("cancel", [False, True])
def test_digest_failure_or_cancellation_waits_for_all_worker_cleanup(
    monkeypatch, tmp_path, cancel
):
    import json

    async def scenario():
        ready, never = asyncio.Event(), asyncio.Event()
        started, cleaned, directories = [], [], []

        async def run(self, prompt, working_dir=None):
            task = json.loads((working_dir / "trace.json").read_text())["task_id"]
            started.append(task)
            directories.append(working_dir)
            if len(started) == 8:
                ready.set()
            try:
                await ready.wait()
                if not cancel and task == "0":
                    raise RuntimeError("digest failed")
                await never.wait()
            finally:
                await asyncio.sleep(0.02 if task == "1" else 0)
                cleaned.append(task)

        monkeypatch.setattr(Worker, "_run", run)
        algo, _ = setup(monkeypatch, tmp_path, lambda *_: [], digest_concurrency=8)
        requests, traces = requests_and_traces(16)
        pending = asyncio.create_task(algo._digest_many(requests, traces))
        await asyncio.wait_for(ready.wait(), timeout=2)
        if cancel:
            pending.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
            await pending
        assert sorted(started) == sorted(cleaned)
        assert all(not p.exists() for p in directories)
        assert not list((algo._run_dir / "working").iterdir())
        assert not any(
            t is not asyncio.current_task() and not t.done()
            for t in asyncio.all_tasks()
        )

    output = StringIO()
    with progress_session("log", stream=output):
        asyncio.run(scenario())
    last = output.getvalue().splitlines()[-1]
    assert "state=interrupted" in last and "active=0" in last
    assert "16/16" not in last


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_digest_concurrency_is_rejected(monkeypatch, tmp_path, limit):
    with pytest.raises(ValueError, match="digest_concurrency"):
        setup(monkeypatch, tmp_path, lambda *_: [], digest_concurrency=limit)
