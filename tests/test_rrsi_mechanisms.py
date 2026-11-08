"""Upstream golden outputs and behavior that affects real RRSI search."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_rrsi import Tasks, Worker, change, records, saved, setup

from flashrsi._run_summary import _collect_usage
from flashrsi.agents import MiniAgent
from flashrsi.algorithms import RRSI
from flashrsi.algorithms.rrsi import (
    _attribution as attribution,
)
from flashrsi.algorithms.rrsi import (
    _calibrate as calibrate,
)
from flashrsi.algorithms.rrsi import (
    _edit_budget as edit_budget,
)
from flashrsi.algorithms.rrsi import (
    _judge as judge,
)
from flashrsi.algorithms.rrsi import (
    _normalize_component as normalize_component,
)
from flashrsi.algorithms.rrsi import (
    _quote_in_trace as quote_in_trace,
)
from flashrsi.algorithms.rrsi import (
    _select_traces as select_traces,
)
from flashrsi.config import Config
from flashrsi.core import Trajectory

GOLDEN = json.loads((Path(__file__).parent / "fixtures/rrsi_upstream.json").read_text())


@pytest.mark.parametrize("row", GOLDEN["selection"])
def test_selection_matches_pinned_upstream_outputs(row):
    config = Config({**RRSI._defaults, **row["config"]})
    result = judge(
        {"score": row["score"], "cost": row["cost"]},
        {"score": row["base"], "cost": row["basecost"]},
        row["best"],
        row["delta"],
        [{"component": row["component"]}],
        set(row["accepted"]),
        config,
    )
    assert {key: result[key] for key in row["expected"]} == row["expected"]


@pytest.mark.parametrize("row", GOLDEN["schedule"])
def test_schedule_matches_pinned_upstream_outputs(row):
    config = Config({**RRSI._defaults, "T": row["T"]})
    assert [edit_budget(t, config) for t in range(row["T"])] == row["budgets"]


@pytest.mark.parametrize("row", GOLDEN["tags"])
def test_tags_match_pinned_upstream_outputs(row):
    assert normalize_component(row["declared"], row["diff"], {}) == row["expected"]


@pytest.mark.parametrize("row", GOLDEN["calibration"])
def test_calibration_matches_pinned_upstream_outputs(row):
    evaluations = []
    for trial_set in row["evaluations"]:
        scores = {str(i): item["rewards"] for i, item in enumerate(trial_set)}
        weights = {str(i): item["weights"] for i, item in enumerate(trial_set)}
        numerator = sum(
            reward * weight
            for task in scores
            for reward, weight in zip(scores[task], weights[task])
        )
        evaluations.append(
            {
                "score": numerator / sum(map(sum, weights.values())),
                "per_task": scores,
                "weights": weights,
            }
        )
    assert calibrate(evaluations, 2, 7)["delta"] == row["expected_delta"]


def test_comment_cannot_earn_memory_novelty(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "tools/runtime.py").write_text(
            "value = 0\n# reusable hint\n", newline="\n"
        )
        return [change(component="memory", path="tools/runtime.py")]

    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        w_s=0,
        component_signals={},
    )
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    history = saved(algo, "edit-history")[0]
    assert (
        history["component"] == "prompt" and history["declared_component"] == "memory"
    )
    assert history["novelty"] == 0 and not history["accepted"]


def test_bundled_edits_are_tagged_from_their_own_files(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        (directory / "tools/runtime.py").write_text("value = 1\n", newline="\n")
        return [
            change(component="memory"),
            change(component="client_tool", path="tools/runtime.py"),
        ]

    algo, _ = setup(monkeypatch, tmp_path, edit, b_min=2)
    asyncio.run(algo.evolve())
    rows = saved(algo, "edit-history")
    assert [row["component"] for row in rows] == ["skill", "client_tool"]
    assert rows[0]["declared_component"] == "memory"
    assert all(row["novelty"] == 2 for row in rows)


def test_identical_siblings_still_have_only_one_winning_variant(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit, m=2)
    asyncio.run(algo.evolve())
    first, second = saved(algo, "edit-history")
    assert first["candidate_id"] == second["candidate_id"]
    assert (first["outcome"], second["outcome"]) == ("ACCEPTED", "LOST")


def test_upstream_generic_credential_precheck_runs_before_model_critic(
    monkeypatch, tmp_path
):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text(
            "invented key: sk-" + "x" * 25, newline="\n"
        )
        return [change()]

    algo, calls = setup(monkeypatch, tmp_path, edit)
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    assert len(calls) == 2 and not saved(algo, "critic")
    assert "credential pattern" in saved(algo, "repair")[0]["objection"]


def test_manifest_component_case_is_canonicalized_like_upstream(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change(component="SKILL")]

    algo, _ = setup(monkeypatch, tmp_path, edit)
    assert asyncio.run(algo.evolve()).files["skills/main.md"].content == b"GOOD"
    assert saved(algo, "edit-history")[0]["component"] == "skill"


@pytest.mark.parametrize("outside_scope", [False, True])
def test_default_semantic_tags_reuse_snapshot_permissions(
    monkeypatch, tmp_path, outside_scope
):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        if outside_scope:
            (directory / "outside.py").write_text("value = 1\n", newline="\n")
        return [change()]

    algo, calls = setup(monkeypatch, tmp_path, edit)
    seed = algo.target.snapshot
    result = asyncio.run(algo.evolve())
    assert (result.id != seed.id) == (not outside_scope)
    assert len(calls) == (2 if outside_scope else 4)
    assert "component_paths" not in algo.config


def test_reserved_slot_is_checked_after_retagging(monkeypatch, tmp_path):
    count = 0

    def edit(prompt, directory):
        nonlocal count
        count += 1
        if count == 1:
            (directory / "tools/runtime.py").write_text(
                "value = 0\n# first hint\n", newline="\n"
            )
            return [change(component="client_tool", path="tools/runtime.py")]
        (directory / "tools/runtime.py").write_text(
            "value = 0\n# new hint\n", newline="\n"
        )
        return [change(component="client_tool", path="tools/runtime.py")]

    algo, calls = setup(monkeypatch, tmp_path, edit, T=2, w=1, component_signals={})
    asyncio.run(algo.evolve())
    assert len(calls) == 4
    assert saved(algo, "edit-history")[-1]["component"] == "prompt"
    assert "judged by the diff" in saved(algo, "repair")[-1]["objection"]


def test_siblings_do_not_receive_each_others_proposals_or_repairs(
    monkeypatch, tmp_path
):
    calls = 0

    def edit(prompt, directory):
        nonlocal calls
        calls += 1
        text = json.dumps(records(prompt, directory))
        assert "FIRST_PRIVATE_HYPOTHESIS" not in text
        assert "FIRST_PRIVATE_OBJECTION" not in text
        if calls == 2:
            assert "FIRST_PRIVATE_OBJECTION" in prompt
        if calls == 3:
            assert "FIRST_PRIVATE_OBJECTION" not in prompt
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [
            change(
                hypothesis="FIRST_PRIVATE_HYPOTHESIS"
                if calls < 3
                else "Independent mechanism"
            )
        ]

    verdicts = iter(
        [
            {"verdict": "reject", "reasons": ["FIRST_PRIVATE_OBJECTION"]},
            {"verdict": "accept"},
            {"verdict": "accept"},
        ]
    )
    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        m=2,
        repair_rounds=1,
        critic=lambda prompt: next(verdicts),
    )
    asyncio.run(algo.evolve())
    assert calls == 3


def test_trace_selection_uses_worst_failure_and_best_success_trial():
    measurement = {
        "per_task": {"bad": [1, 0, 0.5], "good": [0.5, 1, 1]},
        "evidence": [
            {"task_id": task, "trial": trial, "output": f"{task}{trial}"}
            for task in ("bad", "good")
            for trial in range(3)
        ],
    }
    traces = select_traces(measurement, 1, 1)
    assert (
        traces["bad"]["evidence"]["output"] == "bad1"
        and traces["bad"]["lens"] == "failure"
    )
    assert (
        traces["good"]["evidence"]["output"] == "good1"
        and traces["good"]["lens"] == "success"
    )
    assert len(select_traces(measurement, 1, 0)) == 1


def test_round_evidence_retains_digests_and_history_without_individual_logs(
    monkeypatch, tmp_path
):
    rounds = []

    def edit(prompt, directory):
        rows = records(prompt, directory)
        context = next(row for row in rows if "history" in row)
        rounds.append(context)
        assert context["analysis"]["digests"][0]["task_id"] == "train-0"
        assert "TAIL_EVIDENCE" in json.dumps(rows)
        assert "target-private-secret" not in json.dumps(rows)
        if context["step"] == 1:
            assert len(context["history"]) == 2
            assert all(row["attribution"] for row in context["history"])
            assert {row["outcome"] for row in context["history"]} == {
                "ACCEPTED",
                "LOST",
            }
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit, T=2, m=2)
    record = algo._record

    def archive(label, payload):
        record(label, payload)
        if label in {
            "baseline",
            "candidate",
            "edit-history",
        }:
            for path in (algo._run_dir / "trajectories").glob(f"*-{label}.json"):
                path.unlink()

    monkeypatch.setattr(algo, "_record", archive)
    result = asyncio.run(algo.evolve())
    assert result.files["skills/main.md"].content == b"GOOD"
    assert rounds[0] == rounds[1] and rounds[2] == rounds[3]


def test_trial_evidence_does_not_count_as_extra_worker_usage(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, calls = setup(monkeypatch, tmp_path, edit)
    asyncio.run(algo.evolve())
    usage = _collect_usage(algo._run_dir)
    assert usage["roles"]["target"]["finished"]["records"] == len(calls) == 4
    assert usage["roles"]["worker"]["finished"]["records"] == 5


@pytest.mark.parametrize("failure", [ValueError, RuntimeError])
def test_critic_worker_failure_propagates_and_cleans_up_direct_run(
    monkeypatch, tmp_path, failure
):
    directories = []

    def critic(prompt):
        directories.append(algo._run_dir)
        raise failure("worker unavailable")

    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit, critic=critic, repair_rounds=2)
    seed = algo.target.snapshot
    algo._run_dir = None
    with pytest.raises(failure, match="worker unavailable"):
        asyncio.run(algo.evolve())
    assert len(directories) == 1 and not directories[0].exists()
    assert algo._run_dir is None and algo.target.snapshot.id == seed.id


@pytest.mark.parametrize(
    "problem", ["quote", "metric_quote", "write", "json", "identity", "oversize"]
)
def test_digest_repairs_protocol_without_mutating_the_incumbent(
    monkeypatch, tmp_path, problem
):
    original = Worker._run
    attempts = 0

    async def run(self, prompt, working_dir=None):
        nonlocal attempts
        result = await original(self, prompt, working_dir)
        if prompt.startswith("[RRSI: digest]"):
            attempts += 1
            if attempts == 1:
                digest = json.loads(result.output)
                if problem == "quote":
                    digest["evidence"][0]["quote"] = "FABRICATED_EVIDENCE"
                elif problem == "metric_quote":
                    digest["evidence"].append(
                        {"where": "reward", "quote": "reward = 0.0"}
                    )
                elif problem == "write":
                    (working_dir / "trace.json").write_text("{}", newline="\n")
                elif problem == "json":
                    return Trajectory(output="not JSON")
                elif problem == "identity":
                    digest["task_id"] = "test-private"
                else:
                    digest["narrative"] = "x" * 7000
                return replace(result, output=json.dumps(digest))
            if problem in {"quote", "metric_quote"}:
                index = 0 if problem == "quote" else 1
                assert f"evidence[{index}].quote" in prompt
                assert "one decoded trace value" in prompt
        return result

    monkeypatch.setattr(Worker, "_run", run)

    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit)
    result = asyncio.run(algo.evolve())
    assert attempts == 2
    assert result.files["skills/main.md"].content == b"GOOD"
    assert not any(
        "ANALYST-TAMPER" in path or "trace.json" == path for path in result.files
    )
    assert not list((algo._run_dir / "working").iterdir())


def test_digest_long_context_keeps_short_evidence_readable_by_line(
    monkeypatch, tmp_path
):
    evidence = {
        "task_id": "train-0",
        "trajectory": {
            "messages": [{"text": "long context " * 20000}],
            "output": "TAIL_EVIDENCE",
        },
    }
    original = Worker._run
    inspected = []

    async def run(self, prompt, working_dir=None):
        if prompt.startswith("[RRSI: digest]"):
            text = (working_dir / "trace.json").read_text(encoding="utf-8")
            assert json.loads(text) == evidence
            output_lines = [line for line in text.splitlines() if '"output"' in line]
            assert len(output_lines) == 1 and len(output_lines[0]) < 100
            inspected.append(True)
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [])
    result = asyncio.run(
        algo._digest({"task_id": "train-0", "evidence": evidence}, "failure", [])
    )
    assert inspected == [True]
    assert result["evidence"][0]["quote"] == "TAIL_EVIDENCE"
    assert not list((algo._run_dir / "working").iterdir())


def test_digest_quotes_support_decoded_multiline_unicode():
    assert quote_in_trace(
        "第一行\n第二行", {"messages": [{"text": "第一行\n第二行\n第三行"}]}
    )
    assert not quote_in_trace("missing quote", {"output": "real evidence"})


def test_bad_digest_stops_after_bounded_protocol_retries(monkeypatch, tmp_path):
    original = Worker._run
    calls = 0

    async def run(self, prompt, working_dir=None):
        nonlocal calls
        if prompt.startswith("[RRSI: digest]"):
            calls += 1
            return Trajectory(output="not JSON")
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)
    algo, _ = setup(monkeypatch, tmp_path, lambda prompt, directory: [change()])
    with pytest.raises(ValueError, match="RRSI digest invalid"):
        asyncio.run(algo.evolve())
    assert calls == 3
    assert not saved(algo, "rrsi-context")
    assert not list((algo._run_dir / "working").iterdir())


@pytest.mark.parametrize("digest_concurrency", [1, 8])
def test_analyst_can_dispatch_all_lenses_and_follow_up(
    monkeypatch, tmp_path, digest_concurrency
):
    original = Worker._run
    rounds = 0

    async def run(self, prompt, working_dir=None):
        nonlocal rounds
        if prompt.startswith("[RRSI: analyze]"):
            rounds += 1
            if rounds <= 2:
                requests = (
                    [
                        {"task_id": "train-0", "lens": "failure"},
                        {"task_id": "train-1", "lens": "success"},
                    ]
                    if rounds == 1
                    else [
                        {
                            "task_id": "train-0",
                            "lens": "capability_gap",
                            "questions": ["Was the environment the blocker?"],
                        }
                    ]
                )
                return Trajectory(
                    output=json.dumps({"action": "digest_many", "requests": requests})
                )
            assert "Was the environment the blocker?" in prompt
            assert '"lens": "success"' in prompt
            assert '"lens": "capability_gap"' in prompt
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)

    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        n_fail_traces=1,
        n_success_traces=1,
        digest_concurrency=digest_concurrency,
    )
    asyncio.run(algo.evolve())
    report = saved(algo, "rrsi-context")[0]["analysis"]
    assert len(report["digests"]) == 3
    assert {row["lens"] for row in report["digests"]} == {
        "failure",
        "success",
        "capability_gap",
    }


def test_analyst_must_investigate_before_returning_a_report(monkeypatch, tmp_path):
    original = Worker._run
    rounds = 0

    async def run(self, prompt, working_dir=None):
        nonlocal rounds
        if prompt.startswith("[RRSI: analyze]"):
            rounds += 1
            if rounds == 1:
                return Trajectory(
                    output=json.dumps(
                        {
                            "action": "report",
                            "failure_modes": [],
                            "capability_gaps": [],
                            "success_habits": [],
                        }
                    )
                )
            if rounds == 2:
                assert "investigate at least one trace" in prompt
                return Trajectory(
                    output=json.dumps(
                        {"action": "digest_many", "requests": [{"task_id": "train-0"}]}
                    )
                )
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)

    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit)
    asyncio.run(algo.evolve())
    assert rounds == 3


def test_analyst_stops_after_30_actions_without_a_grounded_report(
    monkeypatch, tmp_path
):
    original = Worker._run
    calls = 0

    async def run(self, prompt, working_dir=None):
        nonlocal calls
        if prompt.startswith("[RRSI: analyze]"):
            calls += 1
            return Trajectory(output='{"action": "report"}')
        return await original(self, prompt, working_dir)

    monkeypatch.setattr(Worker, "_run", run)
    algo, _ = setup(monkeypatch, tmp_path, lambda prompt, directory: [change()])
    seed = algo.target.snapshot
    with pytest.raises(ValueError, match="30 turns without a report"):
        asyncio.run(algo.evolve())
    assert calls == 30 and algo.target.snapshot.id == seed.id
    assert not saved(algo, "rrsi-context")
    assert not list((algo._run_dir / "working").iterdir())


@pytest.mark.parametrize("repairable", [True, False])
def test_critic_protocol_retries_never_default_to_accept(
    monkeypatch, tmp_path, repairable
):
    calls = 0

    def critic(prompt):
        nonlocal calls
        calls += 1
        return (
            {"verdict": "accept"}
            if repairable and calls == 3
            else {"unparseable": True}
        )

    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, evaluations = setup(monkeypatch, tmp_path, edit, critic=critic)
    seed = algo.target.snapshot
    result = asyncio.run(algo.evolve())
    assert calls == 3
    assert (result.id != seed.id) == repairable
    assert len(evaluations) == (4 if repairable else 2)


@pytest.mark.parametrize(
    "missing", ["id", "targets_mode", "predicted_affected", "retroactive_check"]
)
def test_candidate_requires_a_falsifiable_edit_manifest(monkeypatch, tmp_path, missing):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        item = change()
        del item[missing]
        return [item]

    algo, calls = setup(monkeypatch, tmp_path, edit)
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    assert len(calls) == 2
    assert missing in saved(algo, "repair")[0]["objection"]


def test_predictions_are_scored_and_supplied_to_the_next_round(monkeypatch, tmp_path):
    count = 0

    def edit(prompt, directory):
        nonlocal count
        count += 1
        if count == 2:
            context = next(
                row for row in records(prompt, directory) if "history" in row
            )
            row = context["history"][0]["attribution"]
            assert row["predicted_hit"] == ["train-0"]
            assert row["unpredicted_regressions"] == ["train-1"]
            assert row["hit_rate"] == 1
        (directory / "skills/main.md").write_text(f"GOOD-{count}", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit, T=2, k=2)

    async def run(self, prompt, working_dir=None):
        seed = (self.files_dir / "skills/main.md").read_text() == "SEED"
        score = (
            (0 if prompt == "train-0" else 0.5)
            if seed
            else (1 if prompt == "train-0" else 0)
        )
        return Trajectory(output=str(score), usage={"total_tokens": 100})

    monkeypatch.setattr(MiniAgent, "_run", run)
    asyncio.run(algo.evolve())
    assert count == 2 and len(saved(algo, "edit-history")) == 2
    assert all(row["attribution"] for row in saved(algo, "edit-history"))


def test_unknown_prediction_is_filtered_like_upstream():
    baseline = {"per_task": {"a": [0]}}
    candidate = {"per_task": {"a": [1]}}
    row = attribution([change(predicted_affected=["unknown"])], baseline, candidate, 1)[
        0
    ]
    assert row["n_predicted"] == 0 and row["hit_rate"] is None


def _preserve_extra_metrics(results):
    return tuple(
        replace(row, metrics={**row.metrics, "accuracy": float(row.output or 0)})
        for row in results
    )


def test_weighted_benchmark_metric_prevents_reversed_selection(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit, weight_metric="criteria")
    monkeypatch.setattr(
        Tasks, "grade", lambda self, results: _preserve_extra_metrics(results)
    )

    async def run(self, prompt, working_dir=None):
        seed = (self.files_dir / "skills/main.md").read_text() == "SEED"
        score = (
            (0 if prompt == "train-0" else 1)
            if seed
            else (1 if prompt == "train-0" else 0.9)
        )
        return Trajectory(
            output=str(score),
            usage={"total_tokens": 100},
            metrics={"criteria": 1 if prompt == "train-0" else 100},
        )

    monkeypatch.setattr(MiniAgent, "_run", run)
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    baseline, candidate = saved(algo, "measurement")
    assert baseline["score"] == pytest.approx(100 / 101)
    assert candidate["score"] == pytest.approx(91 / 101)


def test_domain_guard_rejects_a_higher_scoring_candidate(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit, guards={"valid": {"max_drop": 0.1}})
    monkeypatch.setattr(
        Tasks, "grade", lambda self, results: _preserve_extra_metrics(results)
    )

    async def run(self, prompt, working_dir=None):
        seed = (self.files_dir / "skills/main.md").read_text() == "SEED"
        return Trajectory(
            output="0.5" if seed else "1",
            usage={"total_tokens": 100},
            metrics={"valid": 1 if seed else 0.5},
        )

    monkeypatch.setattr(MiniAgent, "_run", run)
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    row = saved(algo, "edit-history")[0]
    assert row["delta_S"] == 0.5 and "domain guard violated" in row["reason"]


def test_missing_guard_metric_cannot_silently_disable_a_guard(monkeypatch, tmp_path):
    algo, _ = setup(
        monkeypatch,
        tmp_path,
        lambda prompt, directory: [change()],
        guards={"valid": {"max_drop": 0.1}},
    )
    with pytest.raises(ValueError, match="missing guard metric"):
        asyncio.run(algo.evolve())


def test_smoke_failure_is_not_a_measured_component(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit, smoke_n=1)
    calls = []

    async def run(self, prompt, working_dir=None):
        seed = (self.files_dir / "skills/main.md").read_text() == "SEED"
        calls.append(seed)
        return (
            Trajectory(output="0.5", usage={"total_tokens": 100})
            if seed
            else Trajectory(
                status="error",
                error_message="constructor failed",
                output="0",
                usage={"total_tokens": 1},
            )
        )

    monkeypatch.setattr(MiniAgent, "_run", run)
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    assert calls == [True, True, False]
    assert len(saved(algo, "measurement")) == 1
    history = saved(algo, "edit-history")[0]
    assert history["delta_S"] is None and history["reason"] == "smoke failed"


def test_repeated_baseline_calibration_supports_single_trial_evaluations(
    monkeypatch, tmp_path
):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit, delta=None, calibration_evals=2)
    calls = 0

    async def run(self, prompt, working_dir=None):
        nonlocal calls
        calls += 1
        return Trajectory(
            output="0" if calls <= 2 else "1", usage={"total_tokens": 100}
        )

    monkeypatch.setattr(MiniAgent, "_run", run)
    asyncio.run(algo.evolve())
    row = saved(algo, "calibration")[0]
    assert row["source"] == "repeated base evaluations" and row["n_evals"] == 2
    assert row["delta"] == 2


@pytest.mark.parametrize(
    "options",
    [
        {"component_signals": {"not-a-component": ["x"]}},
        {"component_signals": {"memory": ["["]}},
        {"guards": {"valid": {"max_drop": -1}}},
        {"guards": {"valid": {"max_drop": True}}},
        {"weight_metric": 7},
        {"n_fail_traces": 0, "n_success_traces": 0},
    ],
)
def test_invalid_mechanism_configuration_fails_at_startup(
    monkeypatch, tmp_path, options
):
    with pytest.raises(ValueError):
        setup(monkeypatch, tmp_path, lambda prompt, directory: [change()], **options)


def test_removed_configuration_is_rejected_instead_of_silently_ignored(
    monkeypatch, tmp_path
):
    with pytest.raises(ValueError, match="unknown RRSI options.*component_paths"):
        setup(
            monkeypatch,
            tmp_path,
            lambda prompt, directory: [change()],
            component_paths={"skill": ["skills/**"]},
        )
