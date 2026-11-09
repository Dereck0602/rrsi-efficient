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
    resume.legacy_rows = rows
    resume.legacy_measurements = measurements
    resume.legacy_revised = revised
    current = _validate_history(resume, previous["algorithm"].get("k", 2))
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
    old["component_classifier"] = previous["algorithm"].get(
        "component_classifier", "legacy"
    )
    new = dict(algorithm.config)
    # Legacy imports restore only a complete round, with no in-flight phases or
    # trials. These scheduling switches affect future work, not saved decisions.
    # Native checkpoints still require an identical scheduling configuration.
    scheduling = (
        "shared_candidate_queue",
        "proposal_concurrency",
        "pipeline_evaluation",
    )
    changes = {
        key: {"before": old[key], "after": new[key]}
        for key in scheduling
        if old[key] != new[key]
    }
    for values in (old, new):
        values.pop("worker", None)
        values.pop("name", None)
        for key in scheduling:
            values.pop(key)
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
    resume.legacy_scheduling_changes = changes


def _validate_history(resume, repeats):
    """Match each accepted variant to its own measurement, even with equal IDs."""
    baseline = resume.legacy_revised[resume.legacy_measurements[0][0]]
    incumbent = baseline
    current_id, current_score = baseline["snapshot_id"], baseline["score"]
    best_id, best_score = current_id, current_score
    history, round_edits, step, context = [], [], 0, None
    groups, batch = [], []
    for name, row in resume.legacy_rows:
        if name.endswith("-candidate.json") and context is not None:
            batch.append(row)
            if len(batch) == repeats:
                groups.append({"records": batch, "measurement": None})
                batch = []
        elif name.endswith("-measurement.json") and context is not None:
            if not groups or groups[-1]["measurement"] is not None or batch:
                raise ValueError(
                    "Legacy candidate measurement has no complete trial group"
                )
            groups[-1]["measurement"] = resume.legacy_revised[name]
        elif name.endswith("-rrsi-context.json"):
            if (
                row["step"] != step
                or row["incumbent_id"] != current_id
                or row["score"] != current_score
                or row["best_score"] != best_score
                or row["history"] != history
            ):
                raise ValueError("Legacy round context/history mismatch")
            context = row
            groups, batch = [], []
        elif name.endswith("-edit-history.json"):
            if context is None or row["t"] != step or row["parent_id"] != current_id:
                raise ValueError("Legacy edit history parent/round mismatch")
            round_edits.append(row)
        elif name.endswith("-round-summary.json"):
            candidates = {}
            for edit in round_edits:
                if edit["candidate_id"] is not None:
                    if (
                        candidates.setdefault(edit["variant"], edit["candidate_id"])
                        != edit["candidate_id"]
                    ):
                        raise ValueError(
                            "Legacy variant identifies different Snapshots"
                        )
            if batch or len(groups) != len(candidates):
                raise ValueError("Legacy candidate trial groups/history mismatch")
            measurements = {}
            for (variant, identity), group in zip(
                sorted(candidates.items()), groups, strict=True
            ):
                if any(
                    record["snapshot_id"] != identity for record in group["records"]
                ):
                    raise ValueError("Legacy candidate trial group Snapshot mismatch")
                measurements[variant] = group["measurement"]
            selected = [r for r in round_edits if r["accepted"]]
            variants = {r["variant"] for r in selected}
            winners = {r["candidate_id"] for r in selected}
            expected = next(iter(winners)) if winners else current_id
            if (
                context is None
                or len(variants) > 1
                or len(winners) > 1
                or row["round"] != step + 1
                or row["current_snapshot_id"] != expected
                or row["accepted_candidate"] != (expected != current_id)
                or row["delta"] != context["delta"]
            ):
                raise ValueError("Legacy accepted candidate/summary mismatch")
            if variants:
                incumbent = measurements[next(iter(variants))]
            if incumbent is None or incumbent["score"] != row["current_score"]:
                raise ValueError("Legacy round measurement/score mismatch")
            current_id, current_score = expected, incumbent["score"]
            if current_score > best_score:
                best_id = current_id
            best_score = max(best_score, current_score)
            if (
                row["best_score"] != best_score
                or row["best_snapshot_id"] != best_id
                or row["baseline_score"] != baseline["score"]
            ):
                raise ValueError("Legacy historical best mismatch")
            history.extend(round_edits)
            round_edits, context = [], None
            step += 1
    return incumbent
