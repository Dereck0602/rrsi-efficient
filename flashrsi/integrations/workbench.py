"""Expose native WorkBench tools to either harness and score on the host."""

import ast
import asyncio
import hashlib
import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi.benchmarks.workbench import DATABASE_FILES, REVISION
from flashrsi.integrations import _web_tools


class _Session:
    """One child process owns one native thread-local database for one task."""

    def __init__(self, upstream, data, directory):
        for name in DATABASE_FILES:
            destination = directory / "data" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(data / name, destination)
        names = {"PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TMP", "TEMP"}
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).with_name("_workbench_process.py")),
            ],
            cwd=directory,
            env={k: v for k, v in os.environ.items() if k.upper() in names},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        self.responses = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.description = self.request({"upstream": str(upstream)})["tools"]
        except BaseException:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def request(self, value):
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()
        try:
            line = self.responses.get(timeout=120)
        except queue.Empty:
            self.process.kill()
            raise RuntimeError("WorkBench native tool/scorer timed out") from None
        if line is None:
            raise RuntimeError("WorkBench native tool/scorer process stopped")
        return json.loads(line)

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


def bind(agent, benchmark):
    if sys.version_info < (3, 12):
        raise ValueError("Pinned WorkBench requires Python >=3.12")
    if any(importlib.util.find_spec(m) is None for m in ("pandas", "numpy")):
        raise ValueError("Install flashrsi-lite[all]")
    _web_tools._check_target(agent, "WorkBench")

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("WorkBench requires a loaded benchmark Task")
        if any(
            hashlib.sha256((benchmark._data_root / name).read_bytes()).hexdigest()
            != benchmark._file_checksums[name]
            for name in DATABASE_FILES
        ):
            raise ValueError("WorkBench native database changed after load")
        records = []
        with TemporaryDirectory(prefix="flashrsi-workbench-host-") as host:
            session = await asyncio.to_thread(
                _Session, benchmark._upstream_root, benchmark._data_root, Path(host)
            )
            result = None
            scores = {}

            def respond(raw):
                value = {}
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or set(value) != {
                        "name",
                        "arguments",
                    }:
                        raise ValueError("Only native tool calls are accepted")
                    response = session.request({"operation": "call", **value})
                except Exception as exc:
                    response = {
                        "error": f"WorkBench tool failed ({type(exc).__name__})"
                    }
                records.append(
                    {"type": "WorkBenchTool", "request": value, "response": response}
                )
                return response

            try:
                prompt = (
                    "Today's date is Thursday, 2023-11-30 and the current time is 00:00:00.\n"
                    + task.question
                    + "\n\nWorkBench tools (use your shell tool):\n"
                    + _web_tools._command(120)
                    + " call TOOL_NAME 'JSON_OBJECT_OF_STRING_KEYWORD_ARGUMENTS'\n\n"
                    + session.description
                )
                result = await _web_tools._run_with_relay(target, prompt, respond)
            finally:
                # No target requests can race the final host-only scorer.
                try:
                    if result is not None and result.status == "finished":
                        try:
                            scores = await asyncio.to_thread(
                                session.request,
                                {
                                    "operation": "grade",
                                    "reference": ast.literal_eval(
                                        benchmark._rows_by_id[task.task_id]["outcome"]
                                    ),
                                },
                            )
                        except Exception as exc:
                            scores = {
                                "error": f"Native scorer failed ({type(exc).__name__})"
                            }
                finally:
                    session.close()
            messages = tuple(
                m
                for m in result.messages
                if m.get("type") not in ("WorkBenchVerifier", "WorkBenchTool")
            ) + tuple(records)
            if result.status == "finished":
                if "error" in scores:
                    result = replace(
                        result,
                        status="error",
                        error_message="WorkBench native scoring failed: "
                        + scores["error"],
                    )
                else:
                    messages += (
                        {
                            "type": "WorkBenchVerifier",
                            "task_id": task.task_id,
                            "task_checksum": task.metadata["task_checksum"],
                            "data_checksum": benchmark._data_checksum,
                            "snapshot_id": target.snapshot.id,
                            "revision": REVISION,
                            "ground_truth_version": "v2",
                            "actions": scores["actions"],
                            "metrics": scores["metrics"],
                        },
                    )
            return replace(result, messages=messages)

    agent._env_runner = run
