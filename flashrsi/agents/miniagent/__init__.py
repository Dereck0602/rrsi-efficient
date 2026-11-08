"""Run the selected MiniAgent source in an independent Python process."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi._progress import progress_note
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
            timed_out = False
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(json.dumps(request, default=dict).encode()),
                    timeout=timeout,
                )
            except TimeoutError:
                timed_out = True
            finally:
                # Also reap children spawned by native tools before files_dir is
                # cleaned. Cancellation propagates after this awaited cleanup.
                await _stop(process)
            if timed_out:
                return _recover(
                    root / "runs", f"MiniAgent timed out after {timeout:g}s", secrets
                )
            if process.returncode:
                detail = _redact(stderr.decode(errors="replace"), secrets)
                return _recover(
                    root / "runs",
                    f"MiniAgent process exited with {process.returncode}: "
                    + detail[-4000:].strip(),
                    secrets,
                )
            # Native messages and usage are preserved; only the status is mapped.
            try:
                result = _redact(json.loads(stdout), secrets)
            except (ValueError, UnicodeDecodeError):
                return _recover(
                    root / "runs", "MiniAgent returned invalid result JSON", secrets
                )
            success = result["stop_reason"] == "completed"
            usage = {"model_calls": result["usage"], "turns": result["turns"]}
            request_errors = result.get("model_request_errors", [])
            if request_errors and config.get("_worker_system_prompt"):
                retries = sum(
                    row.get("retry_delay_seconds") is not None for row in request_errors
                )
                progress_note(
                    f"Worker API: failed_attempts={len(request_errors)} "
                    f"retries={retries} status={'finished' if success else 'error'}"
                )
            if result.get("usage_complete") is False:
                usage.update(
                    accounting_complete=False,
                    accounting_note="Some model requests failed without returning usage.",
                )
            return Trajectory(
                status="finished" if success else "error",
                output=result["answer"] if success else "",
                messages=[
                    *result.get("messages", []),
                    *(
                        {"type": "MiniAgentModelRequestError", **row}
                        for row in request_errors
                    ),
                ],
                usage=usage,
                error_message=None if success else result["stop_reason"],
            )


def _recover(runs_root, error, secrets):
    """Salvage flushed native events after the child and its tools have stopped.

    A killed request may have consumed unreported tokens. Preserve the known
    counters as a lower bound, never invent usage for an in-flight request.
    """
    calls, messages = [], []
    turns = unreadable = 0
    for path in sorted(runs_root.glob("*/trace.jsonl")):
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        unreadable += 1
                        continue
                    if not isinstance(event, dict):
                        unreadable += 1
                        continue
                    turn = event.get("turn")
                    if type(turn) is int:
                        turns = max(turns, turn)
                    if event.get("kind") == "model_usage":
                        calls.append(
                            {k: v for k, v in event.items() if k not in {"kind", "t"}}
                        )
                    # Keep execution evidence, not another copy of credentials
                    # or the prompt/configuration the caller already records.
                    if event.get("kind") not in {"llm_config", "run_start", "run_end"}:
                        messages.append({"type": "MiniAgentPartialTrace", **event})
        except OSError:
            unreadable += 1
    return Trajectory(
        status="error",
        error_message=_redact(error, secrets),
        messages=_redact(messages, secrets),
        usage=_redact(
            {
                "model_calls": calls,
                "turns": turns,
                "accounting_complete": False,
                "accounting_source": "trace.jsonl",
                "accounting_note": (
                    "Recovered reported usage only; interrupted requests may have "
                    "consumed additional unreported tokens."
                ),
                "unreadable_trace_entries": unreadable,
            },
            secrets,
        ),
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
