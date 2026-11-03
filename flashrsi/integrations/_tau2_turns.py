"""Keep bounded tau simulations in a child process and Target execution in the parent."""

import asyncio
import json
import os
import signal

from flashrsi._records import _redact, _trajectory_record
from flashrsi.core import Trajectory


def request_turn(prompt, source, sink):
    sink.write(json.dumps({"prompt": prompt}) + "\n")
    sink.flush()
    line = source.readline()
    if not line:
        raise RuntimeError("Tau Target turn channel closed")
    reply = json.loads(line)
    if "error" in reply:
        raise RuntimeError(reply["error"])
    return Trajectory(**reply["trajectory"])


async def run_bounded(command, run_turn, timeout):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=os.name == "posix",
        limit=16 * 1024 * 1024,
    )

    async def exchange():
        while raw := await process.stdout.readline():
            prompt = json.loads(raw)["prompt"]
            if not isinstance(prompt, str):
                raise ValueError("Tau Target turn requires a text prompt")
            try:
                result = await run_turn(prompt)
                reply = {"trajectory": _redact(_trajectory_record(result), [])}
            except Exception as exc:
                reply = {"error": f"Tau Target turn failed ({type(exc).__name__})"}
            process.stdin.write(json.dumps(reply).encode() + b"\n")
            await process.stdin.drain()
        await process.wait()

    timed_out = False
    completed = False
    try:
        async with asyncio.timeout(timeout):
            await exchange()
        completed = True
    except TimeoutError:
        timed_out = True
    finally:
        if not completed:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                elif process.returncode is None:
                    process.kill()
            except ProcessLookupError:
                pass
        await process.communicate()
    return process.returncode, timed_out
