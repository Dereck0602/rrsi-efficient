"""Validate and import complete round boundaries from pre-checkpoint RRSI logs."""

import copy
import json
from statistics import mean

import yaml

from flashrsi._records import _config_record
from flashrsi.algorithms.rrsi._resume import _role_config
from flashrsi.algorithms.rrsi._round_resume import RoundOneResume
from flashrsi.config import _ConfigLoader


def legacy_checkpoint(resume, config):
    previous = yaml.load(resume.read_file("config.yaml"), Loader=_ConfigLoader)
    if previous["algorithm"]["name"] != "rrsi":
        raise ValueError("Legacy checkpoint requires RRSI")
    paths = sorted((resume.source / "trajectories").glob("*.json"))
    boundaries = [p for p in paths if p.name.endswith("-round-summary.json")]
    if not boundaries:
        raise ValueError(
            "No complete round checkpoint; use the legacy baseline resume for baseline-only logs"
        )
    boundary = boundaries[-1]
    rows = [
        (p.name, json.loads(resume.read_file(str(p.relative_to(resume.source)))))
        for p in paths
        if p.name <= boundary.name
    ]
    measurements = [(n, r) for n, r in rows if n.endswith("-measurement.json")]
    if not measurements:
        raise ValueError("Legacy checkpoint lacks baseline measurement")
    baseline = measurements[0][1]
    summaries = [r for n, r in rows if n.endswith("-round-summary.json")]
    if [r["round"] for r in summaries] != list(range(1, len(summaries) + 1)):
        raise ValueError("Legacy round boundaries are not contiguous")
    latest = summaries[-1]
    contexts = [r for n, r in rows if n.endswith("-rrsi-context.json")]
    if not contexts or contexts[-1]["step"] != latest["round"] - 1:
        raise ValueError("Legacy checkpoint lacks the final round context")
    history = [r for n, r in rows if n.endswith("-edit-history.json")]
    if any(r["t"] >= latest["round"] for r in history):
        raise ValueError("Legacy checkpoint contains later edit history")
    revised = {n: copy.deepcopy(r) for n, r in measurements}
    for name, row in rows:
        if name.endswith("-cost-reestimate.json"):
            source = row["source_measurement"]
            if source not in revised:
                raise ValueError(
                    "Legacy cost reestimate references missing measurement"
                )
            revised[source].update(
                cost=row["cost"], cost_accounting=row["cost_accounting"]
            )
    current = next(
        (
            r
            for r in reversed(list(revised.values()))
            if r["snapshot_id"] == latest["current_snapshot_id"]
        ),
        None,
    )
    if current is None or current["score"] != latest["current_score"]:
        raise ValueError("Legacy summary does not match its incumbent measurement")
    estimates, pending = {}, {}
    # Reconstruct the same per-round timing freeze as the original scheduler.
    for name, row in rows:
        if name.endswith("-rrsi-context.json"):
            estimates.update({task: mean(values) for task, values in pending.items()})
            pending = {}
        if name.endswith("-measurement.json"):
            for trial in row["evidence"]:
                if trial.get("execution_seconds") is not None:
                    pending.setdefault(trial["task_id"], []).append(
                        trial["execution_seconds"]
                    )
    resume.read_file("snapshots/worker.json")
    from flashrsi.core import Snapshot

    worker = Snapshot.load(resume.source / "snapshots/worker.json")
    identity = {
        "seed": baseline["snapshot_id"],
        "worker": worker.id,
        "legacy_config": previous,
    }
    state = {
        "current": current["snapshot_id"],
        "incumbent": current,
        "delta": latest["delta"],
        "baseline_score": baseline["score"],
        "best_score": latest["best_score"],
        "best_snapshot_id": latest["best_snapshot_id"],
        "scores": [baseline["score"], *[r["current_score"] for r in summaries]],
        "history": history,
        "analysis": contexts[-1]["analysis"],
        "start_step": latest["round"],
        "task_duration_estimates": estimates,
        "pending_task_durations": pending,
    }
    records = {name: resume.hashes[f"trajectories/{name}"] for name, _ in rows}
    resume.legacy_rows = rows
    resume.legacy_measurements = measurements
    return (
        identity,
        {
            "state": state,
            "records": records,
            "sequence": int(boundary.name.split("-")[0]),
        },
        True,
    )


def validate_legacy(resume, algorithm):
    from flashrsi.algorithms.rrsi import RRSI

    previous = resume.identity["legacy_config"]
    old = {**RRSI._defaults, **previous["algorithm"]}
    new = dict(algorithm.config)
    # Efficient scheduling does not change completed decisions, but strict
    # continuation keeps it fixed too; omitted new fields take their defaults.
    for values in (old, new):
        values.pop("worker", None)
        values.pop("name", None)
    if _config_record(old) != _config_record(new):
        raise ValueError("Legacy RRSI algorithm configuration changed")
    for saved, agent in (
        (previous["agent"], algorithm.target),
        (previous["algorithm"]["worker"], algorithm.worker),
    ):
        if _role_config(saved) != _role_config(agent.config):
            raise ValueError("Legacy RRSI execution configuration changed")
    benchmark = getattr(algorithm, "_benchmark_config", previous["benchmark"])
    if _config_record(benchmark) != _config_record(previous["benchmark"]):
        raise ValueError("Legacy RRSI benchmark configuration changed")
    validator = object.__new__(RoundOneResume)
    validator.rows = resume.legacy_rows
    for index, (name, row) in enumerate(resume.legacy_measurements):
        label = "baseline" if index == 0 else row.get("label", "candidate")
        RoundOneResume.validate_measurement(validator, algorithm, name, row, label)
    state = resume.boundary["state"]
    if state["start_step"] > algorithm.config.T:
        raise ValueError("Legacy checkpoint exceeds configured round count")
    if state["best_score"] != max(state["scores"]):
        raise ValueError("Legacy checkpoint best score differs from history")
