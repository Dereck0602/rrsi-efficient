"""Restore completed baseline repeats from an RRSI run that never began search."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

from flashrsi._records import _redact_config, _trajectory_record, _write_record
from flashrsi.config import _ConfigLoader
from flashrsi.core import Snapshot, Trajectory
from flashrsi.core.trajectory import Results


def _role_config(values):
    values = _redact_config(values)
    # The saved Snapshot determines the source files; credentials can rotate.
    values.pop("source_dir", None)
    for key in ("artifacts_dir", "trials_dir"):
        values.get("harbor", {}).pop(key, None)
    return values


class BaselineResume:
    """Strict, read-only import. This is not a mid-round checkpoint loader."""

    def __init__(self, source, config):
        from flashrsi.algorithms.rrsi import RRSI

        self.source = Path(source).expanduser().resolve()
        self.hashes = {}

        def read(relative):
            data = (self.source / relative).read_bytes()
            self.hashes[relative] = hashlib.sha256(data).hexdigest()
            return data

        previous = yaml.load(read("config.yaml"), Loader=_ConfigLoader)
        if previous["algorithm"]["name"] != "rrsi" or config.algorithm.name != "rrsi":
            raise ValueError("Baseline resume requires RRSI")
        if (self.source / "result.json").exists():
            raise ValueError("Cannot resume an already completed experiment")
        for role, old, new in (
            ("target", previous["agent"], config.agent),
            ("worker", previous["algorithm"]["worker"], config.algorithm.worker),
        ):
            if _role_config(old) != _role_config(new):
                raise ValueError(
                    f"Cannot reuse baseline: {role} execution config changed"
                )
        old_algorithm = {**RRSI._defaults, **previous["algorithm"]}
        new_algorithm = {**RRSI._defaults, **config.algorithm}
        for values in (old_algorithm, new_algorithm):
            values.pop("worker", None)
            values.pop("invalid_missing_frac", None)
            values.pop("digest_concurrency", None)
            values.pop("task_order", None)
            values.pop("shared_candidate_queue", None)
            values.pop("cost_policy", None)
            values.pop("cost_estimate_max_frac", None)
        if _redact_config(old_algorithm) != _redact_config(new_algorithm):
            raise ValueError(
                "Baseline resume permits changing only the error-rate threshold, digest concurrency, task scheduling and cost estimation policy"
            )
        old_benchmark = _redact_config(previous["benchmark"])
        new_benchmark = _redact_config(config.benchmark)
        for values in (old_benchmark, new_benchmark):
            if values.get("data_path"):
                values["data_path"] = str(Path(values["data_path"]).resolve())
        if old_benchmark != new_benchmark:
            raise ValueError("Cannot reuse baseline: benchmark configuration changed")

        paths = sorted((self.source / "trajectories").glob("*.json"))
        if len(paths) != config.algorithm.get("k", RRSI._defaults["k"]) or any(
            not re.fullmatch(r"\d+-baseline\.json", path.name) for path in paths
        ):
            raise ValueError(
                "Resume requires exactly k complete baseline records and no evolution records"
            )
        self.records = [
            json.loads(read(str(path.relative_to(self.source)))) for path in paths
        ]
        ids = {row["snapshot_id"] for row in self.records}
        if len(ids) != 1 or not re.fullmatch(r"[0-9a-f]{64}", next(iter(ids))):
            raise ValueError("Baseline repeats must identify one valid Snapshot")
        target_path = f"snapshots/{next(iter(ids))}.json"
        read(target_path)
        read("snapshots/worker.json")
        self.target = Snapshot.load(self.source / target_path)
        self.worker = Snapshot.load(self.source / "snapshots/worker.json")
        if self.target.parent_id is not None or self.worker.parent_id is not None:
            raise ValueError("Baseline resume requires the original root Snapshots")
        self.threshold_before = previous["algorithm"].get("invalid_missing_frac", 0.15)
        self.threshold_after = config.algorithm.get("invalid_missing_frac", 0.15)
        self.digest_concurrency_before = previous["algorithm"].get(
            "digest_concurrency", 1
        )
        self.digest_concurrency_after = config.algorithm.get("digest_concurrency", 1)
        self.task_order_before = previous["algorithm"].get("task_order", "fifo")
        self.task_order_after = config.algorithm.get("task_order", "fifo")
        self.shared_candidate_queue_before = previous["algorithm"].get(
            "shared_candidate_queue", False
        )
        self.shared_candidate_queue_after = config.algorithm.get(
            "shared_candidate_queue", False
        )
        self.batches = None

    def bind(self, algorithm):
        """Validate all tasks and metrics before restoring the archived roots."""
        tasks = tuple(algorithm.benchmark.train)
        expected = [_trajectory_record(Trajectory(task=task))["task"] for task in tasks]
        batches = []
        for record in self.records:
            rows = record["trajectories"]
            if [row["task"] for row in rows] != expected:
                raise ValueError(
                    "Saved baseline tasks/order/checksums differ from loaded train"
                )
            trajectories = tuple(
                Trajectory(**{**row, "task": task})
                for row, task in zip(rows, tasks, strict=True)
            )
            for trajectory in trajectories:
                if not any(
                    m.get("type") == "RunConfig"
                    and m.get("snapshot_id") == self.target.id
                    for m in trajectory.messages
                ):
                    raise ValueError(
                        "Baseline trajectory Snapshot identity is missing or wrong"
                    )
            batches.append(
                Results(trajectories, metric=algorithm.benchmark.primary_metric)
            )
        for agent, snapshot in (
            (algorithm.target, self.target),
            (algorithm.worker, self.worker),
        ):
            if (
                agent.name != snapshot.agent_type
                or agent.component_rules != snapshot.component_rules
            ):
                raise ValueError(
                    "Archived Snapshot is incompatible with this Agent adapter"
                )
        for agent, snapshot in (
            (algorithm.target, self.target),
            (algorithm.worker, self.worker),
        ):
            # Explicit restoration of a validated root, not rebasing old results
            # onto today's source tree. Preserve Algorithm's editing permissions.
            agent._snapshot = snapshot._with_scope(agent.snapshot._scope)
        self.batches = tuple(batches)
        algorithm._baseline_resume = self

    def metadata(self):
        return {
            "mode": "completed-baseline",
            "source_run": str(self.source),
            "source_sha256": self.hashes,
            "target_snapshot_id": self.target.id,
            "worker_snapshot_id": self.worker.id,
            "reused_baseline_trials": sum(
                len(row["trajectories"]) for row in self.records
            ),
            "invalid_missing_frac_before": self.threshold_before,
            "invalid_missing_frac_after": self.threshold_after,
            "digest_concurrency_before": self.digest_concurrency_before,
            "digest_concurrency_after": self.digest_concurrency_after,
            "task_order_before": self.task_order_before,
            "task_order_after": self.task_order_after,
            "shared_candidate_queue_before": self.shared_candidate_queue_before,
            "shared_candidate_queue_after": self.shared_candidate_queue_after,
            "usage_note": "Inherited baseline usage is included once; no new baseline inference.",
        }

    def record(self, run_dir, config):
        _write_record(run_dir / "resume.json", self.metadata(), config)
