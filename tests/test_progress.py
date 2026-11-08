"""Progress follows actual completion and remains usable in redirected logs."""

import asyncio
import subprocess
import sys
from io import StringIO

import pytest

from flashrsi import _progress
from flashrsi._progress import Progress, progress_note, progress_scope, progress_session
from flashrsi.core import Agent, Task, Trajectory


class Stream(StringIO):
    def __init__(self, tty=False):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.mark.parametrize(
    "mode,tty,bar",
    [
        ("auto", False, False),
        ("auto", True, True),
        ("log", True, False),
        ("bar", False, True),
        ("off", True, False),
    ],
)
def test_modes_close_nested_bars_and_restore_context(mode, tty, bar):
    stream = Stream(tty)

    async def scenario():
        with progress_session(mode, stream=stream), progress_scope("RRSI"):
            async with Progress(1, unit="rounds") as rounds:
                rounds.start("round=1/1")
                with progress_scope("candidate=1/1"):
                    progress_note("Worker: editing")
                    async with Progress(2) as tasks:
                        for name in ("a", "b"):
                            tasks.start(name)
                            tasks.finish(name)
                rounds.finish("round=1/1")
        assert not any(t is not asyncio.current_task() for t in asyncio.all_tasks())

    asyncio.run(scenario())
    output = stream.getvalue()
    assert _progress._SESSION.get() is None
    assert _progress._SCOPE.get() == ()
    if mode == "off":
        assert output == ""
    elif bar:
        assert "\r" in output
        assert "100%" in output
        assert "RRSI | candidate=1/1" in output
        from tqdm import tqdm

        assert not tqdm._instances
    else:
        assert "\r" not in output and "\x1b" not in output
        assert "2/2 (100%) active=0 queued=0 errors=0" in output
        assert "1/1 (100%)" in output
        assert "active_rounds=0 queued_rounds=0" in output


def test_parallel_counts_follow_completion_and_report_heartbeat(monkeypatch):
    monkeypatch.setattr(_progress, "_HEARTBEAT_SECONDS", 0.005)
    stream = Stream()

    class Probe(Agent):
        name = "progress-probe"

        async def _run(self, prompt, working_dir=None):
            if prompt == "slow":
                await asyncio.sleep(0.04)
                return Trajectory(output=prompt)
            await asyncio.sleep(0)
            return Trajectory(status="error", error_message="private error detail")

    async def scenario():
        with progress_session("auto", stream=stream), progress_scope("final-test"):
            results = await Probe({"concurrency": 2}).rollout(
                [Task("slow", "slow"), Task("fast", "fast")]
            )
        assert [t.task.task_id for t in results] == ["slow", "fast"]
        assert not any(t is not asyncio.current_task() for t in asyncio.all_tasks())

    asyncio.run(scenario())
    lines = stream.getvalue().splitlines()
    assert "0/2 (0%) active=0 queued=2" in lines[0]
    assert "1/2 (50%) active=1 queued=0 errors=1" in lines[1]
    assert "last=fast:error" in lines[1]
    assert len([line for line in lines if "1/2 (50%)" in line]) > 1
    assert "2/2 (100%) active=0 queued=0 errors=1" in lines[-1]
    assert "state=completed" in lines[-1]
    assert "private error detail" not in stream.getvalue()


def test_container_can_import_core_without_terminal_dependency():
    # Containers install only adapter dependencies; tqdm is a host UI dependency.
    source = """
import importlib.abc
import sys
class BlockTqdm(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname.split('.')[0] == 'tqdm':
            raise ImportError('tqdm is absent in the task container')
sys.meta_path.insert(0, BlockTqdm())
from flashrsi.core import Agent
"""
    result = subprocess.run(
        [sys.executable, "-c", source], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
