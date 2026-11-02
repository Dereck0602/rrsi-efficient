"""Run the selected MiniAgent source in an independent Python process."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi._records import _redact, _secrets
from flashrsi.core import Agent, Trajectory

_ENTRY = Path(__file__).with_name("_entry.py")


class MiniAgent(Agent):
    """Pass inputs to the selected harness and map its result to a Trajectory."""

    name = "miniagent"
    description = "A workspace-driven ReAct harness with optional file editing."
    _container_dependencies = ("openai", "requests")
    # agent.yaml loads this as the system prompt; prompts/extract.md serves the
    # evidence summariser and is not the agent's instructions.
    instruction_file = "prompts/system.md"

    async def _run(self, prompt: str, working_dir: Path | None = None) -> Trajectory:
        config = self.config
        timeout = config.get("timeout", 300)
        # files_dir belongs to Agent; logs and the task cwd belong to this run.
        with TemporaryDirectory(prefix="flashrsi-miniagent-") as temporary:
            root = Path(temporary)
            task_dir = working_dir or root / "task"
            if working_dir is None:
                task_dir.mkdir()
            # These are the native run() arguments, passed unchanged by _entry.py.
            request = {
                "question": prompt,
                "llm_config": config["llm"],
                "web_config": config.get("web", {}),
                "debug_llm_io": config.get("debug_llm_io", False),
                "max_turns": config.get("max_turns", 10),
                "workspace_dir": str(self.files_dir),
                "runs_root": str(root / "runs"),
            }
            if config.get("_worker_system_prompt"):
                request["system_prompt_append"] = config["_worker_system_prompt"]
            if working_dir is not None:
                request["working_dir"] = str(working_dir)
            secrets = sorted(
                set(_secrets(os.environ) + _secrets(config)), key=len, reverse=True
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-B",
                str(_ENTRY),
                cwd=task_dir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(json.dumps(request, default=dict).encode()),
                    timeout=timeout,
                )
            except TimeoutError:
                return Trajectory(
                    status="error",
                    error_message=f"MiniAgent timed out after {timeout:g}s",
                )
            finally:
                # Also reap children spawned by native tools before files_dir is
                # cleaned. Cancellation propagates after this awaited cleanup.
                await _stop(process)
            if process.returncode:
                detail = _redact(stderr.decode(errors="replace"), secrets)
                return Trajectory(
                    status="error",
                    error_message=f"MiniAgent process exited with {process.returncode}: "
                    + detail[-4000:].strip(),
                )
            # Native messages and usage are preserved; only the status is mapped.
            result = _redact(json.loads(stdout), secrets)
            success = result["stop_reason"] == "completed"
            return Trajectory(
                status="finished" if success else "error",
                output=result["answer"] if success else "",
                messages=result.get("messages", []),
                usage={"model_calls": result["usage"], "turns": result["turns"]},
                error_message=None if success else result["stop_reason"],
            )


async def _stop(process):
    """Finish all work using files_dir before Agent removes that directory."""
    if os.name == "posix":
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except TimeoutError:
                pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.returncode is None:
        process.kill()
    await process.wait()
