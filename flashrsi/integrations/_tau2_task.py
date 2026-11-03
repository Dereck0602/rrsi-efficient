"""Private subprocess entry point for one bounded tau2 simulation."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from flashrsi._records import _redact, _secrets
from flashrsi.integrations._tau2_evidence import _Evidence
from flashrsi.integrations._tau2_turns import request_turn


def main(request_path: Path, response_path: Path) -> None:
    request = json.loads(request_path.read_text(encoding="utf-8"))
    source, sink = sys.stdin, sys.stdout

    def run_turn(prompt):
        return request_turn(prompt, source, sink)

    task = SimpleNamespace(
        task_id=request["task_id"],
        env=request["domain"],
        metadata=request["metadata"],
    )
    evidence = _Evidence(
        request.get("evidence_path"),
        sorted(set(_secrets(request) + _secrets(os.environ)), key=len, reverse=True),
    )
    # Scope the native harness's temporary logs to this isolated task. They are
    # still cleaned on completion, but survive SIGKILL until the parent reads
    # their partial evidence and removes the whole task directory.
    native = request_path.parent / "native"
    native.mkdir(mode=0o700)
    previous_tempdir = tempfile.tempdir
    tempfile.tempdir = str(native)
    try:
        with redirect_stdout(sys.stderr):
            from flashrsi.integrations.tau2 import _simulate, _simulation_result

            run = _simulate(
                run_turn,
                task,
                Path(request["domain"]),
                request["simulator"],
                request["config"],
                evidence,
                request.get("judge"),
            )
            result = _simulation_result(run, evidence)
    except Exception as exc:
        result = {**evidence.read(), "error": str(exc) or type(exc).__name__}
    finally:
        tempfile.tempdir = previous_tempdir
    response_path.write_text(
        json.dumps(_redact(result, evidence.secrets), ensure_ascii=False),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
