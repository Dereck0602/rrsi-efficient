"""Task durations inform dispatch order only, never evaluation membership."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from statistics import mean


def _seconds(value):
    if type(value) in (int, float) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def execution_seconds(trajectory):
    """Prefer slot occupancy; older Harbor archives contain native trial times."""
    for message in reversed(trajectory.messages):
        if message.get("type") == "TaskExecutionTiming":
            value = _seconds(message.get("elapsed_seconds"))
            if value is not None:
                return value
    for message in reversed(trajectory.messages):
        if message.get("type") != "HarborVerifier":
            continue
        artifacts = message.get("artifacts")
        directory = (
            artifacts.get("directory") if isinstance(artifacts, Mapping) else None
        ) or message.get("trial_directory")
        if not directory or trajectory.task is None:
            continue
        try:
            record = json.loads((Path(directory) / "result.json").read_text())
            if record.get("task_name") != trajectory.task.task_id:
                continue
            checksum = message.get("task_checksum")
            if checksum and record.get("task_checksum") != checksum:
                continue
            elapsed = (
                datetime.fromisoformat(record["finished_at"])
                - datetime.fromisoformat(record["started_at"])
            ).total_seconds()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            continue  # Missing timing must not invalidate a task or its score.
        value = _seconds(elapsed)
        if value is not None:
            return value
    return None


def task_schedule(tasks, repeats, estimates, policy):
    """Stable LPT over all repeats; indices identify the canonical trial slots."""
    known = {
        task.task_id: duration
        for task in tasks
        if (duration := _seconds(estimates.get(task.task_id))) is not None
    }
    fallback = mean(known.values()) if known else None
    jobs = [
        {
            "index": trial * len(tasks) + index,
            "task_id": task.task_id,
            "trial": trial,
            "estimated_seconds": known.get(task.task_id, fallback),
        }
        for trial in range(repeats)
        for index, task in enumerate(tasks)
    ]
    if policy == "longest_first" and known:
        jobs.sort(key=lambda job: -job["estimated_seconds"])
    elif policy not in {"fifo", "longest_first"}:
        raise ValueError("task_order must be fifo or longest_first")
    return {
        "policy": policy,
        "known_tasks": len(known),
        "total_tasks": len(tasks),
        "unknown_task_estimate_seconds": fallback,
        "jobs": jobs,
    }
