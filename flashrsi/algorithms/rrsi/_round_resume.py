"""Read-only replay of round one selection, then resume from round two.

This explicit migration never reuses later proposals drafted from the old
incumbent. Raw measurements and the old run remain immutable.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from statistics import mean

import yaml

from flashrsi._records import _redact_config, _trajectory_record, _write_record
from flashrsi.algorithms.rrsi._cost import estimate_cost
from flashrsi.algorithms.rrsi._resume import _role_config
from flashrsi.config import _ConfigLoader
from flashrsi.core import Snapshot, Trajectory


class RoundOneResume:
    def __init__(self, source, config):
        from flashrsi.algorithms.rrsi import RRSI

        self.source = Path(source).expanduser().resolve()
        self.hashes, self.files = {}, {}
        previous = yaml.load(self.read("config.yaml"), Loader=_ConfigLoader)
        if previous["algorithm"]["name"] != "rrsi" or config.algorithm.name != "rrsi":
            raise ValueError("Round replay requires RRSI")
        if (self.source / "result.json").exists():
            raise ValueError("Cannot resume a completed experiment")
        for role, old, new in (
            ("target", previous["agent"], config.agent),
            ("worker", previous["algorithm"]["worker"], config.algorithm.worker),
        ):
            if _role_config(old) != _role_config(new):
                raise ValueError(f"Cannot replay: {role} execution config changed")
        old_config = {**RRSI._defaults, **previous["algorithm"]}
        new_config = {**RRSI._defaults, **config.algorithm}
        allowed = {
            "worker",
            "digest_concurrency",
            "task_order",
            "invalid_missing_frac",
            "cost_policy",
            "cost_estimate_max_frac",
        }
        if _redact_config(
            {k: v for k, v in old_config.items() if k not in allowed}
        ) != _redact_config({k: v for k, v in new_config.items() if k not in allowed}):
            raise ValueError(
                "Round replay permits only cost policy, error threshold, digest concurrency and task order changes"
            )
        for value in (previous["benchmark"], config.benchmark):
            if value.get("data_path"):
                value["data_path"] = str(Path(value["data_path"]).resolve())
        if _redact_config(previous["benchmark"]) != _redact_config(config.benchmark):
            raise ValueError("Cannot replay: benchmark configuration changed")
        if new_config["T"] < 2 or new_config["calibration_evals"] != 1:
            raise ValueError(
                "Round-one replay requires T >= 2 and one baseline calibration group"
            )
        paths = sorted((self.source / "trajectories").glob("*.json"))
        self.rows = []
        for path in paths:
            relative = str(path.relative_to(self.source))
            row = json.loads(self.read(relative))
            self.rows.append((path.name, row))
            if path.name.endswith("-round-summary.json"):
                if row["round"] != 1:
                    raise ValueError("Missing first-round boundary")
                self.boundary = int(path.name.split("-")[0])
                self.original_summary = row
                break
        else:
            raise ValueError("Round one must have a complete round-summary record")
        self.excluded_records = len(paths) - len(self.rows)
        self.measurements = [
            (name, row) for name, row in self.rows if name.endswith("-measurement.json")
        ]
        if len(self.measurements) != new_config["m"] + 1:
            raise ValueError(
                "Round replay requires a completed baseline and every first-round candidate measurement"
            )
        self.target = self.snapshot(self.measurements[0][1]["snapshot_id"])
        self.read("snapshots/worker.json")
        self.worker = Snapshot.load(self.source / "snapshots/worker.json")
        if self.target.parent_id is not None or self.worker.parent_id is not None:
            raise ValueError("Replay requires the original root Snapshots")
        self.snapshots = {self.target.id: self.target}
        for _, row in self.measurements[1:]:
            snapshot = self.snapshot(row["snapshot_id"])
            if (
                snapshot.root_id != self.target.id
                or snapshot.parent_id != self.target.id
            ):
                raise ValueError(
                    "First-round candidates must descend directly from the baseline"
                )
            self.snapshots[snapshot.id] = snapshot
        self.calibration = next(
            row for name, row in self.rows if name.endswith("-calibration.json")
        )
        self.context = next(
            row for name, row in self.rows if name.endswith("-rrsi-context.json")
        )
        if self.context["step"] != 0 or self.context["history"]:
            raise ValueError("Expected initial round-one context")
        self.history_before = [
            row for name, row in self.rows if name.endswith("-edit-history.json")
        ]
        self.cost_policy_before = old_config["cost_policy"]
        self.config = new_config
        self.state = None

    def read(self, relative):
        data = (self.source / relative).read_bytes()
        self.hashes[relative] = hashlib.sha256(data).hexdigest()
        self.files[relative] = data
        return data

    def snapshot(self, identity):
        if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Invalid Snapshot ID")
        relative = f"snapshots/{identity}.json"
        self.read(relative)
        return Snapshot.load(self.source / relative)

    def validate_measurement(self, algorithm, name, measurement, label):
        from flashrsi.algorithms.rrsi import _token_accounting, _weighted_score

        tasks = tuple(algorithm.benchmark.train)
        expected = [_trajectory_record(Trajectory(task=task))["task"] for task in tasks]
        records = [
            row
            for filename, row in self.rows
            if filename < name
            and filename.endswith(f"-{label}.json")
            and row["snapshot_id"] == measurement["snapshot_id"]
        ][-algorithm.config.k :]
        if (
            len(records) != algorithm.config.k
            or len(measurement["evidence"]) != len(tasks) * algorithm.config.k
        ):
            raise ValueError("Incomplete archived measurement repeats")
        trials = []
        for repeat, record in enumerate(records):
            if [row["task"] for row in record["trajectories"]] != expected:
                raise ValueError(
                    "Archived tasks/order/checksums differ from loaded train"
                )
            for task, raw in zip(tasks, record["trajectories"], strict=True):
                if not any(
                    m.get("type") == "RunConfig"
                    and m.get("snapshot_id") == measurement["snapshot_id"]
                    for m in raw["messages"]
                ):
                    raise ValueError("Archived trajectory Snapshot identity mismatch")
                trajectory = Trajectory(**{**raw, "task": task})
                evidence = measurement["evidence"][len(trials)]
                observed, complete = _token_accounting(trajectory.usage)
                reward = (
                    0.0
                    if trajectory.status == "error"
                    else trajectory.metrics[algorithm.benchmark.primary_metric]
                )
                if (
                    evidence["task_id"] != task.task_id
                    or evidence["trial"] != repeat
                    or evidence["trajectory"]
                    != algorithm._trajectory_evidence(trajectory)
                    or evidence["observed_tokens"] != observed
                    or evidence["usage_complete"] != complete
                    or evidence["tokens"] != (observed if complete else None)
                    or evidence["reward"] != reward
                ):
                    raise ValueError(
                        "Measurement does not match archived raw trajectory"
                    )
                weight = (
                    trajectory.metrics.get(algorithm.config.weight_metric)
                    if algorithm.config.weight_metric
                    else 1.0
                )
                if evidence["weight"] != weight:
                    raise ValueError("Archived measurement weight mismatch")
                trials.append(evidence)
        score = _weighted_score(
            [row["reward"] for row in trials], [row["weight"] for row in trials]
        )
        errors = sum(row["trajectory"]["status"] == "error" for row in trials)
        per_task = {
            task.task_id: [
                row["reward"] for row in trials if row["task_id"] == task.task_id
            ]
            for task in tasks
        }
        if (
            score != measurement["score"]
            or errors != measurement["error_trials"]
            or per_task != measurement["per_task"]
        ):
            raise ValueError("Archived aggregate score/error count mismatch")
        costs = [row["tokens"] for row in trials]
        cost = mean(costs) if None not in costs else None
        cost = cost if cost is not None and cost > 0 else None
        accounting = measurement["cost_accounting"]
        complete = sum(row["usage_complete"] for row in trials)
        missing = sum(row["observed_tokens"] is None for row in trials)
        expected_accounting = {
            "complete": cost is not None,
            "trials_total": len(trials),
            "trials_complete": complete,
            "trials_missing": missing,
            "trials_partial": len(trials) - complete - missing,
            "observed_tokens": sum(row["observed_tokens"] or 0 for row in trials),
        }
        if measurement["cost"] != cost or any(
            accounting.get(key) != value for key, value in expected_accounting.items()
        ):
            raise ValueError("Archived token accounting mismatch")
        for metric in algorithm.config.guards:
            if measurement["guard_metrics"][metric] != mean(
                row["trajectory"]["metrics"][metric] for row in trials
            ):
                raise ValueError("Archived guard metric mismatch")

    def bind(self, algorithm):
        from flashrsi.algorithms.rrsi import _calibrate, _judge

        revised = []
        for index, (name, row) in enumerate(self.measurements):
            self.validate_measurement(
                algorithm, name, row, "baseline" if index == 0 else "candidate"
            )
            measurement = copy.deepcopy(row)
            estimate_cost(measurement, algorithm.config)
            revised.append(measurement)
        baseline = revised[0]
        if baseline["error_trials"] > algorithm.config.invalid_missing_frac * len(
            baseline["evidence"]
        ):
            raise ValueError("Reused baseline exceeds the execution error threshold")
        delta = algorithm.config.delta
        if delta is None:
            delta = _calibrate(
                [baseline], algorithm.config.delta_z, algorithm.config.seed
            )["delta"]
        if (
            delta != self.calibration["delta"]
            or self.context["incumbent_id"] != self.target.id
        ):
            raise ValueError("Archived calibration/context mismatch")
        history, decisions = [], []
        winner, incumbent = None, baseline
        for variant, candidate in enumerate(revised[1:]):
            edits = [
                copy.deepcopy(row)
                for row in self.history_before
                if row["variant"] == variant
            ]
            if not edits or any(
                row["t"] != 0
                or row["parent_id"] != self.target.id
                or row["candidate_id"] != candidate["snapshot_id"]
                for row in edits
            ):
                raise ValueError(
                    "Missing or inconsistent first-round candidate edit history"
                )
            decision = _judge(
                candidate,
                baseline,
                baseline["score"],
                delta,
                edits,
                set(),
                algorithm.config,
            )
            if candidate["error_trials"] > algorithm.config.invalid_missing_frac * len(
                candidate["evidence"]
            ):
                decision.update(admissible=False, reason="too many failed trials")
            decisions.append(
                {
                    "variant": variant,
                    "snapshot_id": candidate["snapshot_id"],
                    **decision,
                }
            )
            history.extend({**row, **decision} for row in edits)
            if decision["admissible"] and (
                winner is None or candidate["score"] > incumbent["score"]
            ):
                winner, incumbent = variant, candidate
        for row in history:
            selected = row["variant"] == winner
            row.update(
                accepted=selected,
                outcome="ACCEPTED"
                if selected
                else "LOST"
                if row["admissible"]
                else "REJECTED",
            )
        current = self.snapshots[incumbent["snapshot_id"]]
        for agent, snapshot in (
            (algorithm.target, self.target),
            (algorithm.worker, self.worker),
        ):
            if (
                agent.name != snapshot.agent_type
                or agent.component_rules != snapshot.component_rules
            ):
                raise ValueError("Archived Snapshot incompatible with Agent adapter")
        # All evidence/config validation precedes modifying the new algorithm.
        for agent, snapshot in (
            (algorithm.target, self.target),
            (algorithm.worker, self.worker),
        ):
            agent._snapshot = snapshot._with_scope(agent.snapshot._scope)
        current = current._with_scope(algorithm.target.snapshot._scope)
        self.revised, self.decisions = revised, decisions
        self.state = {
            "current": current,
            "incumbent": incumbent,
            "delta": delta,
            "baseline_score": baseline["score"],
            "best_score": max(baseline["score"], incumbent["score"]),
            "best_snapshot_id": current.id
            if incumbent["score"] > baseline["score"]
            else self.target.id,
            "scores": [baseline["score"], incumbent["score"]],
            "history": history,
            "analysis": copy.deepcopy(self.context["analysis"]),
            "start_step": 1,
        }
        algorithm._round_resume = self

    def metadata(self):
        return {
            "mode": "reselect-round-one",
            "source_run": str(self.source),
            "source_sha256": self.hashes,
            "source_boundary": self.boundary,
            "reused_baseline_trials": len(self.measurements[0][1]["evidence"]),
            "reused_candidate_trials": sum(
                len(row["evidence"]) for _, row in self.measurements[1:]
            ),
            "excluded_later_records": self.excluded_records,
            "next_round": 2,
            "cost_policy_before": self.cost_policy_before,
            "cost_policy_after": self.config["cost_policy"],
            "original_round_summary": self.original_summary,
            "selected_snapshot_id": self.state["current"].id if self.state else None,
            "decisions": getattr(self, "decisions", []),
            "usage_note": "Inherited round-one usage is included once; no baseline/candidate inference replay. Estimates are separate from reported provider tokens.",
        }

    def restore(self, algorithm):
        if self.state is None:
            raise ValueError("Bind/validate round replay before restoring")
        # Prevent silently replaying a source modified after validation.
        for relative, expected in self.hashes.items():
            if (
                hashlib.sha256((self.source / relative).read_bytes()).hexdigest()
                != expected
            ):
                raise ValueError(f"Resume source changed: {relative}")
        for relative, data in self.files.items():
            if relative.startswith("trajectories/"):
                # Keep old decisions in the source; emit the revised history as
                # the sole active history for this branch.
                if relative.endswith(("-edit-history.json", "-round-summary.json")):
                    continue
                (algorithm._run_dir / relative).write_bytes(data)
            elif relative.startswith("snapshots/") and not relative.endswith(
                "/worker.json"
            ):
                (algorithm._run_dir / relative).write_bytes(data)
        algorithm._sequence = self.boundary
        for original, revised in zip(self.measurements, self.revised, strict=True):
            algorithm._record(
                "cost-reestimate",
                {
                    "source_measurement": original[0],
                    "snapshot_id": revised["snapshot_id"],
                    "cost": revised["cost"],
                    "cost_accounting": revised["cost_accounting"],
                },
            )
        for row in self.state["history"]:
            algorithm._record("edit-history", row)
        state = self.state
        algorithm._record(
            "round-summary",
            {
                "round": 1,
                "rounds": algorithm.config.T,
                "reselected": True,
                "current_score": state["incumbent"]["score"],
                "best_score": state["best_score"],
                "baseline_score": state["baseline_score"],
                "gain_from_baseline": state["best_score"] - state["baseline_score"],
                "current_snapshot_id": state["current"].id,
                "best_snapshot_id": state["best_snapshot_id"],
                "accepted_candidate": state["current"].id != self.target.id,
                "current_error_trials": state["incumbent"]["error_trials"],
                "trials_total": len(state["incumbent"]["evidence"]),
                "delta": state["delta"],
            },
        )
        for measurement in self.revised[1:]:
            for trial in measurement["evidence"]:
                if trial.get("execution_seconds") is not None:
                    algorithm._pending_task_durations.setdefault(
                        trial["task_id"], []
                    ).append(trial["execution_seconds"])
        _write_record(
            algorithm._run_dir / "resume.json",
            self.metadata(),
            {"target": algorithm.target.config, "worker": algorithm.worker.config},
        )
        return state
