"""Bridge shell-capable agents to isolated ALFWorld text sessions."""

import asyncio
import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import replace
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi.benchmarks.alfworld import TEXTWORLD_VERSION, VERSION
from flashrsi.integrations import _web_tools


class _Session:
    def __init__(self, setup, directory):
        names = {"PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "HOME"}
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-X",
                "utf8",
                str(Path(__file__).with_name("_alfworld_process.py")),
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
        self.error = None
        try:
            self.initial = self.request(setup)
        except BaseException:
            self.close()
            raise

    def _read(self):
        for line in self.process.stdout:
            self.responses.put(line)
        self.responses.put(None)

    def request(self, value):
        try:
            self.process.stdin.write(json.dumps(value) + "\n")
            self.process.stdin.flush()
            line = self.responses.get(timeout=120)
            if line is None:
                raise RuntimeError("Native process stopped")
            return json.loads(line)
        except Exception as exc:
            self.error = f"ALFWorld process failed ({type(exc).__name__})"
            raise RuntimeError(self.error) from None

    def close(self):
        # EOF lets the child close the native environment before exiting.
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=2)
        self.process.stdout.close()


def _check_runtime():
    if os.name != "posix" or not (3, 11) <= sys.version_info < (3, 13):
        raise ValueError(
            "ALFWorld text requires Linux/WSL/macOS with Python 3.11 or 3.12"
        )
    for package, required in (("alfworld", VERSION), ("textworld", TEXTWORLD_VERSION)):
        try:
            installed = version(package)
        except PackageNotFoundError:
            installed = None
        if installed != required:
            raise ValueError(
                f"Install flashrsi-lite[all]: requires {package}=={required}"
            )


def bind(agent, benchmark):
    _check_runtime()
    _web_tools._check_target(agent, "ALFWorld")

    async def run(target, task):
        if benchmark._tasks_by_id.get(task.task_id) != task:
            raise ValueError("ALFWorld requires a loaded benchmark Task")
        game, checksum = benchmark._games[task.task_id]
        game_bytes = game.read_bytes()
        if hashlib.sha256(game_bytes).hexdigest() != checksum:
            raise ValueError("ALFWorld native game changed after load")
        records = []
        with TemporaryDirectory(prefix="flashrsi-alfworld-host-") as host:
            gamefile = Path(host) / "game.tw-pddl"
            gamefile.write_bytes(game_bytes)
            setup = {
                "gamefile": str(gamefile),
                "max_steps": benchmark.max_steps,
                "seed": benchmark.config.get("seed", 0),
                "admissible_commands": benchmark.admissible_commands,
            }
            session = await asyncio.to_thread(_Session, setup, host)
            result, score = None, {}

            def respond(raw):
                action = None
                try:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or set(value) != {"action"}:
                        raise ValueError("Only action is accepted")
                    action = value["action"]
                    if not isinstance(action, str):
                        raise ValueError("Action must be a string")
                    response = session.request({"operation": "step", "action": action})
                except Exception as exc:
                    response = {
                        "error": f"ALFWorld action failed ({type(exc).__name__})"
                    }
                records.append(
                    {"type": "ALFWorldStep", "action": action, "response": response}
                )
                return response

            try:
                prompt = (
                    task.question
                    + "\n\nInitial environment observation:\n"
                    + json.dumps(session.initial, ensure_ascii=False)
                    + "\n\nUse your shell tool to submit one environment action:\n"
                    + _web_tools._command(120)
                    + " step 'ACTION'\n"
                    + f"At most {benchmark.max_steps} environment steps. "
                    "Use the native text commands (e.g. look, inventory, help). "
                    "Stop when done is true. Your final text does not change the environment."
                )
                result = await _web_tools._run_with_relay(target, prompt, respond)
            finally:
                try:
                    if result is not None and result.status == "finished":
                        if session.error:
                            score = {"error": session.error}
                        else:
                            try:
                                score = await asyncio.to_thread(
                                    session.request, {"operation": "grade"}
                                )
                            except RuntimeError as exc:
                                score = {"error": str(exc)}
                finally:
                    session.close()
            messages = tuple(
                m
                for m in result.messages
                if m.get("type") not in ("ALFWorldVerifier", "ALFWorldStep")
            ) + tuple(records)
            if result.status == "finished":
                if "error" in score:
                    result = replace(
                        result, status="error", error_message=score["error"]
                    )
                else:
                    messages += (
                        {
                            "type": "ALFWorldVerifier",
                            "task_id": task.task_id,
                            "task_checksum": task.metadata["task_checksum"],
                            "game_sha256": checksum,
                            "version": VERSION,
                            "textworld_version": TEXTWORLD_VERSION,
                            "max_steps": benchmark.max_steps,
                            "seed": setup["seed"],
                            "admissible_commands": benchmark.admissible_commands,
                            **score,
                        },
                    )
            return replace(result, messages=messages)

    agent._env_runner = run
