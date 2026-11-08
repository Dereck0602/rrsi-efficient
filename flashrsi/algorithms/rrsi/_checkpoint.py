"""Durable RRSI boundaries, completed proposal phases and individual trials.

Only RRSI uses these hooks; Agent execution and standalone evaluation are
unchanged. A resume always imports into a new run, never edits its source.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from collections import Counter
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from tempfile import NamedTemporaryFile

import yaml

from flashrsi._progress import Progress, progress_session
from flashrsi._records import _config_record, _redact, _trajectory_record
from flashrsi.algorithms.rrsi._resume import _role_config
from flashrsi.core import Snapshot, Trajectory
from flashrsi.core.agent import _Execution

PHASE = ContextVar("rrsi_checkpoint_phase", default=None)
VARIANT = ContextVar("rrsi_checkpoint_variant", default=None)


def _encode(item):
    # Match ordinary trajectory records for safe YAML values in task metadata.
    if isinstance(item, Mapping):
        return dict(item)
    if isinstance(item, Path):
        return str(item)
    return {"type": "yaml", "value": yaml.safe_dump(item)}


def _detach(value):
    return json.loads(json.dumps(value, default=_encode, ensure_ascii=False))


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=_encode,
        ).encode()
    ).hexdigest()


def read(path):
    envelope = json.loads(path.read_text())
    if envelope.get("version") != 1 or digest(envelope["data"]) != envelope.get(
        "sha256"
    ):
        raise ValueError(f"Invalid RRSI checkpoint: {path}")
    return envelope["data"]


def atomic(path, data):
    """A killed writer leaves either the old file or the entire new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {"version": 1, "sha256": digest(data), "data": data}
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", dir=path.parent, prefix=".pending-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(envelope, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def task_record(task):
    return _detach(_trajectory_record(Trajectory(task=task))["task"])


def signature(algorithm):
    # Credentials/output locations may change; all inference, selection and
    # scheduling settings remain fixed, including T (which affects annealing).
    return _config_record(
        {
            "algorithm": {
                k: v for k, v in algorithm.config.items() if k not in {"worker", "name"}
            },
            "target": _role_config(algorithm.target.config),
            "worker": _role_config(algorithm.worker.config),
            "benchmark": getattr(
                algorithm, "_benchmark_config", {"name": algorithm.benchmark.name}
            ),
            "metric": algorithm.benchmark.primary_metric,
            "tasks": {
                split: [task_record(t) for t in getattr(algorithm.benchmark, split)]
                for split in ("train", "dev", "test")
            },
        }
    )


class Checkpoints:
    def __init__(self, algorithm):
        self.algorithm = algorithm
        self.root = algorithm._run_dir / "checkpoints"
        self.step = -1
        self.calibration = 0
        self.execution = digest(_role_config(algorithm.target.config))

    def save(self, relative, data):
        # Same credential filtering as ordinary records, before checksumming.
        atomic(
            self.root / relative,
            _redact(_detach(data), self.algorithm._runtime_secrets()),
        )

    def start(self):
        algorithm = self.algorithm
        algorithm._save_snapshot(algorithm.target.snapshot)
        worker = algorithm.worker.snapshot if algorithm.worker.component_rules else None
        if worker is not None:
            path = algorithm._run_dir / "snapshots/worker.json"
            if path.exists():
                if Snapshot.load(path).id != worker.id:
                    raise ValueError("Existing Worker checkpoint identity mismatch")
            else:
                worker.save(path)
        self.save(
            "identity.json",
            {
                "signature": signature(algorithm),
                "seed": algorithm.target.snapshot.id,
                "worker": worker.id if worker is not None else None,
            },
        )

    def boundary(self, state):
        algorithm = self.algorithm
        algorithm._save_snapshot(state["current"])
        state = {
            **state,
            "current": state["current"].id,
            "task_duration_estimates": algorithm._task_duration_estimates,
            "pending_task_durations": algorithm._pending_task_durations,
        }
        records = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (algorithm._run_dir / "trajectories").glob("*.json")
        }
        self.save(
            "round.json",
            {"state": state, "records": records, "sequence": algorithm._sequence},
        )

    def recorded(self, label):
        phase = PHASE.get()
        if phase is not None:
            phase["records"].append(f"{self.algorithm._sequence:05d}-{label}.json")

    async def phase(self, key, operation):
        key = {"step": self.step, **key}
        path = self.root / "phases" / f"{digest(key)}.json"
        if path.exists():
            saved = read(path)
            if saved["key"] != key:
                raise ValueError("RRSI phase identity mismatch")
            value = saved["result"]
            if key["phase"] == "proposal":
                snapshot = value[0]
                value = (self.snapshot(snapshot) if snapshot else None, *value[1:])
            return value
        phase = {"key": key, "records": []}
        token = PHASE.set(phase)
        try:
            result = await operation()
            value = result
            if key["phase"] == "proposal":
                snapshot = result[0]
                if snapshot is not None:
                    self.algorithm._save_snapshot(snapshot)
                value = [snapshot.id if snapshot else None, *result[1:]]
            records = {
                name: hashlib.sha256(
                    (self.algorithm._run_dir / "trajectories" / name).read_bytes()
                ).hexdigest()
                for name in phase["records"]
            }
            self.save(
                str(path.relative_to(self.root)),
                {"key": key, "result": value, "records": records},
            )
            return result
        finally:
            PHASE.reset(token)

    def snapshot(self, identity):
        path = self.algorithm._run_dir / "snapshots" / f"{identity}.json"
        snapshot = Snapshot.load(path)
        if snapshot.id != identity:
            raise ValueError("RRSI checkpoint Snapshot identity mismatch")
        self.algorithm.target._validate_snapshot(snapshot)
        return snapshot._with_scope(self.algorithm.target.snapshot._scope)

    def trial_key(self, snapshot, task, index, *, phase, variant=None, step=None):
        return {
            "step": self.step if step is None else step,
            "phase": phase,
            "variant": variant,
            "snapshot": snapshot.id,
            "index": index,
            "execution": self.execution,
            "task": task_record(task),
        }

    @staticmethod
    def validate(key, task, row):
        if row.task != task or task_record(task) != key["task"]:
            raise ValueError("RRSI trial task/checksum mismatch")
        configs = [m for m in row.messages if m.get("type") == "RunConfig"]
        if not configs or any(m.get("snapshot_id") != key["snapshot"] for m in configs):
            raise ValueError("RRSI trial Snapshot identity mismatch")
        for event in row.messages:
            if event.get("type") == "HarborVerifier" and (
                event.get("snapshot_id") != key["snapshot"]
                or event.get("task_id") != task.task_id
                or (
                    task.metadata.get("task_checksum") is not None
                    and event.get("task_checksum") != task.metadata["task_checksum"]
                )
            ):
                raise ValueError("RRSI trial verifier identity mismatch")

    def load_trial(self, key, task):
        path = self.root / "trials" / f"{digest(key)}.json"
        if not path.exists():
            return None
        saved = read(path)
        if saved["key"] != key or saved["trajectory"]["task"] != key["task"]:
            raise ValueError("RRSI trial identity mismatch")
        row = Trajectory(**{**saved["trajectory"], "task": task})
        self.validate(key, task, row)
        return row

    def save_trial(self, key, task, row):
        self.validate(key, task, row)
        self.save(
            f"trials/{digest(key)}.json",
            {"key": key, "trajectory": _trajectory_record(row)},
        )

    async def rollout(self, snapshot, tasks, *, phase, indexes=None, variant=None):
        target = self.algorithm.target
        config = copy.deepcopy(target.config)
        semaphore = asyncio.Semaphore(config.get("concurrency", 1))
        indexes = tuple(range(len(tasks))) if indexes is None else indexes

        counts, occurrences, labels = Counter(t.task_id for t in tasks), Counter(), []
        for task in tasks:
            occurrences[task.task_id] += 1
            label = task.task_id
            if counts[task.task_id] > 1:
                label += f" [repeat={occurrences[task.task_id]}/{counts[task.task_id]}]"
            labels.append(label)

        async def execute(task, index, label):
            key = self.trial_key(snapshot, task, index, phase=phase, variant=variant)
            row = self.load_trial(key, task)
            if row is not None:
                progress.start(label)
                progress.finish(label, row.status)
                return row
            async with semaphore:
                progress.start(label)
                status = "error"
                token = target._execution.set(
                    _Execution(snapshot, None, copy.deepcopy(config))
                )
                try:
                    with progress_session("off"):
                        rows = await target.rollout((task,), snapshot=snapshot)
                    if len(rows) != 1:
                        raise ValueError("RRSI rollout must return exactly one trial")
                    # rollout includes native cleanup. Persist before releasing
                    # the slot or reporting completion to a batch/queue.
                    self.save_trial(key, task, rows[0])
                    status = rows[0].status
                    return rows[0]
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                finally:
                    progress.finish(label, status)
                    target._execution.reset(token)

        async with Progress(len(tasks)) as progress:
            pending = [
                asyncio.create_task(execute(t, i, label))
                for t, i, label in zip(tasks, indexes, labels, strict=True)
            ]
            try:
                return tuple(await asyncio.gather(*pending))
            finally:
                for task in pending:
                    if not task.done() and not task.cancelling():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)


class CheckpointResume:
    """Read-only validation followed by import into a fresh output directory."""

    def __init__(self, source, config=None, *, mode="trial"):
        if mode not in {"round", "trial"}:
            raise ValueError("RRSI resume mode must be round or trial")
        self.source = Path(source).expanduser().resolve()
        if (self.source / "result.json").exists():
            raise ValueError("Cannot resume an already completed experiment")
        self.mode, self.files, self.hashes = mode, {}, {}
        self.state = None
        self.bound = False
        if not (self.source / "checkpoints/identity.json").exists():
            from flashrsi.algorithms.rrsi._legacy_resume import legacy_checkpoint

            self.identity, self.boundary, self.legacy = legacy_checkpoint(self, config)
            self.mode = "round"  # Legacy logs cannot certify unfinished trials.
        else:
            self.legacy = False
            self.identity = self.read_checkpoint("checkpoints/identity.json")
            path = self.source / "checkpoints/round.json"
            self.boundary = (
                self.read_checkpoint("checkpoints/round.json")
                if path.exists()
                else None
            )
        self.target = self.snapshot(self.identity["seed"])
        self.worker = None
        if self.identity["worker"] is not None:
            self.read_file("snapshots/worker.json")
            self.worker = Snapshot.load(self.source / "snapshots/worker.json")
            if self.worker.id != self.identity["worker"]:
                raise ValueError("Worker Snapshot identity mismatch")
        self._collect()

    def read_file(self, relative):
        data = (self.source / relative).read_bytes()
        self.files[relative] = data
        self.hashes[relative] = hashlib.sha256(data).hexdigest()
        return data

    def read_checkpoint(self, relative):
        self.read_file(relative)
        return read(self.source / relative)

    def snapshot(self, identity):
        if (
            not isinstance(identity, str)
            or len(identity) != 64
            or any(c not in "0123456789abcdef" for c in identity)
        ):
            raise ValueError("Invalid checkpoint Snapshot ID")
        relative = f"snapshots/{identity}.json"
        self.read_file(relative)
        snapshot = Snapshot.load(self.source / relative)
        if snapshot.id != identity:
            raise ValueError("Checkpoint Snapshot filename mismatch")
        return snapshot

    def _collect(self):
        records = dict(self.boundary["records"]) if self.boundary else {}
        start = self.boundary["state"]["start_step"] if self.boundary else -1
        self.trials = []
        if self.mode == "trial":
            for path in sorted((self.source / "checkpoints/phases").glob("*.json")):
                relative = str(path.relative_to(self.source))
                data = self.read_checkpoint(relative)
                if path.stem != digest(data["key"]):
                    raise ValueError("Checkpoint phase filename mismatch")
                if data["key"]["step"] < start:
                    self.files.pop(relative)
                    continue
                records.update(data["records"])
                if data["key"]["phase"] == "proposal" and data["result"][0]:
                    self.snapshot(data["result"][0])
            for path in sorted((self.source / "checkpoints/trials").glob("*.json")):
                relative = str(path.relative_to(self.source))
                data = self.read_checkpoint(relative)
                if path.stem != digest(data["key"]):
                    raise ValueError("Checkpoint trial filename mismatch")
                if data["key"]["step"] < start:
                    self.files.pop(relative)
                    continue
                self.trials.append(data)
                self.snapshot(data["key"]["snapshot"])
            # Completed Worker calls from an interrupted phase still cost
            # tokens. Keep them, even though that phase must be attempted anew.
            for path in (self.source / "trajectories").glob("*-worker.json"):
                try:
                    json.loads(path.read_text())
                except json.JSONDecodeError:
                    continue  # An uncommitted torn log is not a checkpoint.
                records.setdefault(
                    path.name, hashlib.sha256(path.read_bytes()).hexdigest()
                )
        for name, expected in records.items():
            if Path(name).name != name:
                raise ValueError("Invalid checkpoint record path")
            relative = f"trajectories/{name}"
            self.read_file(relative)
            if self.hashes[relative] != expected:
                raise ValueError(f"Checkpoint record changed: {name}")
        # Historical evidence/proposals refer to archived snapshots too.
        for relative in list(self.files):
            if relative.startswith("trajectories/"):
                row = json.loads(self.files[relative])
                for key in (
                    "snapshot_id",
                    "current_snapshot_id",
                    "best_snapshot_id",
                    "candidate_id",
                    "parent_id",
                    "incumbent_id",
                ):
                    if row.get(key):
                        self.snapshot(row[key])
        self.records = records
        if self.boundary:
            self.snapshot(self.boundary["state"]["current"])

    def bind(self, algorithm):
        if algorithm.name != "rrsi":
            raise ValueError("Checkpoint resume requires RRSI")
        if self.legacy:
            from flashrsi.algorithms.rrsi._legacy_resume import validate_legacy

            validate_legacy(self, algorithm)
        elif signature(algorithm) != self.identity["signature"]:
            raise ValueError("RRSI checkpoint configuration/tasks changed")
        for agent, snapshot in (
            (algorithm.target, self.target),
            (algorithm.worker, self.worker),
        ):
            if snapshot is not None and (
                agent.name != snapshot.agent_type
                or agent.component_rules != snapshot.component_rules
            ):
                raise ValueError("Checkpoint Snapshot incompatible with Agent adapter")
        tasks = {
            digest(task_record(t)): t
            for split in ("train", "dev", "test")
            for t in getattr(algorithm.benchmark, split)
        }
        execution = digest(_role_config(algorithm.target.config))
        for trial in self.trials:
            key = trial["key"]
            task = tasks.get(digest(key["task"]))
            if task is None or key["execution"] != execution:
                raise ValueError("RRSI checkpoint trial configuration/tasks changed")
            row = Trajectory(**{**trial["trajectory"], "task": task})
            if trial["trajectory"]["task"] != key["task"]:
                raise ValueError("RRSI checkpoint trial task mismatch")
            Checkpoints.validate(key, task, row)
        for agent, snapshot in (
            (algorithm.target, self.target),
            (algorithm.worker, self.worker),
        ):
            if snapshot is not None:
                agent._snapshot = snapshot._with_scope(agent.snapshot._scope)
        self.bound = True
        algorithm._checkpoint_resume = self

    def metadata(self):
        counts = {"baseline": 0, "candidate": 0}
        for relative, data in self.files.items():
            if relative.startswith("trajectories/"):
                for phase in counts:
                    if relative.endswith(f"-{phase}.json"):
                        counts[phase] += len(json.loads(data)["trajectories"])
        for trial in self.trials:
            if trial["key"]["phase"] in counts:
                counts[trial["key"]["phase"]] += 1
        return {
            "mode": f"checkpoint-{self.mode}",
            "legacy_round_import": self.legacy,
            "source_run": str(self.source),
            "source_sha256": self.hashes,
            "next_round": self.boundary["state"]["start_step"] + 1
            if self.boundary
            else 1,
            "reused_baseline_trials": counts["baseline"],
            "reused_candidate_trials": counts["candidate"],
            "usage_note": "Completed trials (including errors) are reused once. Interrupted executions restart; their unreported usage is unknown. Incomplete Worker phases restart; recorded Worker usage is retained.",
        }

    def restore(self, algorithm):
        if not self.bound:
            raise ValueError("Bind/validate the checkpoint before restoring")
        if algorithm._run_dir.resolve() == self.source:
            raise ValueError("Resume requires a new output directory")
        for relative, expected in self.hashes.items():
            if (
                hashlib.sha256((self.source / relative).read_bytes()).hexdigest()
                != expected
            ):
                raise ValueError(f"Resume source changed: {relative}")
        for relative, data in self.files.items():
            if relative.startswith(("trajectories/", "snapshots/", "checkpoints/")):
                destination = algorithm._run_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
        algorithm._sequence = max(
            (int(name.split("-")[0]) for name in self.records), default=0
        )
        from flashrsi._records import _write_record

        _write_record(algorithm._run_dir / "resume.json", self.metadata(), {})
        if self.boundary is None:
            return None
        state = copy.deepcopy(self.boundary["state"])
        state["current"] = self.snapshot(state["current"])._with_scope(
            algorithm.target.snapshot._scope
        )
        algorithm._task_duration_estimates = state.pop("task_duration_estimates")
        algorithm._pending_task_durations = state.pop("pending_task_durations")
        return state
