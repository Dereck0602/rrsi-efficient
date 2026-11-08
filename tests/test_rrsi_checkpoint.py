"""Interrupt at real persistence boundaries and compare resumed decisions."""

import asyncio
import json
import shutil
from dataclasses import replace

import pytest
from test_rrsi import change, records, saved, setup

from flashrsi.algorithms.rrsi._checkpoint import (
    CheckpointResume,
    Checkpoints,
    atomic,
    read,
)


def edit(prompt, directory):
    context = records(prompt, directory)[0]
    marker = "GOOD" if context["step"] == 0 else "BEST"
    (directory / "skills/main.md").write_text(marker)
    return [change()]


def build(monkeypatch, path, **options):
    return setup(monkeypatch, path, edit, T=3, k=2, **options)


def interrupted_rollout(algorithm, monkeypatch, *, complete, error_trial=False):
    original = algorithm.target.rollout
    attempts = []

    async def rollout(tasks, *, snapshot=None):
        attempts.append((snapshot.id, tasks[0].task_id))
        if len(attempts) > complete:
            raise RuntimeError("injected interruption")
        rows = await original(tasks, snapshot=snapshot)
        if error_trial and len(attempts) == complete:
            rows = (
                replace(
                    rows[0],
                    status="error",
                    output="0",
                    error_message="terminal failure",
                ),
            )
        return rows

    monkeypatch.setattr(algorithm.target, "rollout", rollout)
    return attempts


@pytest.mark.parametrize("complete", [0, 1, 3, 4, 5, 7, 8, 11])
def test_trial_resume_matches_uninterrupted_and_reuses_completed_calls(
    monkeypatch, tmp_path, complete
):
    reference, _ = build(monkeypatch, tmp_path / "reference", m=2)
    expected = asyncio.run(reference.evolve())
    old, _ = build(monkeypatch, tmp_path / "old", m=2)
    interrupted_rollout(old, monkeypatch, complete=complete)
    with pytest.raises(RuntimeError, match="injected interruption"):
        asyncio.run(old.evolve())
    before = {
        str(p.relative_to(old._run_dir)): p.read_bytes()
        for p in old._run_dir.rglob("*")
        if p.is_file()
    }
    current, calls = build(monkeypatch, tmp_path / "new", m=2)
    resume = CheckpointResume(old._run_dir)
    resume.bind(current)
    final = asyncio.run(current.evolve())
    assert final.id == expected.id
    assert saved(current, "round-summary") == saved(reference, "round-summary")
    assert saved(current, "edit-history") == saved(reference, "edit-history")
    expected_calls = sum(
        len(row["trajectories"])
        for phase in ("baseline", "candidate")
        for row in saved(reference, phase)
    )
    assert len(calls) == expected_calls - complete
    assert len(old.worker.prompts) + len(current.worker.prompts) == len(
        reference.worker.prompts
    )
    assert before == {
        str(p.relative_to(old._run_dir)): p.read_bytes()
        for p in old._run_dir.rglob("*")
        if p.is_file()
    }
    # Raw trial persistence never introduces duplicate graded repetitions.
    for phase in ("baseline", "candidate"):
        assert len(saved(current, phase)) == len(saved(reference, phase))


@pytest.mark.parametrize("round_number", [1, 2, 3])
def test_any_complete_round_restores_history_floor_and_timing(
    monkeypatch, tmp_path, round_number
):
    old, _ = build(monkeypatch, tmp_path / "old")
    original = Checkpoints.boundary

    def stop(checkpoint, state):
        original(checkpoint, state)
        if state["start_step"] == round_number:
            raise RuntimeError("boundary interruption")

    with monkeypatch.context() as local:
        local.setattr(Checkpoints, "boundary", stop)
        with pytest.raises(RuntimeError, match="boundary interruption"):
            asyncio.run(old.evolve())
    current, calls = build(monkeypatch, tmp_path / "new")
    CheckpointResume(old._run_dir, mode="round").bind(current)
    asyncio.run(current.evolve())
    assert [row["round"] for row in saved(current, "round-summary")] == [1, 2, 3]
    assert all(marker != "SEED" for _, marker, _ in calls)
    assert len(saved(current, "baseline")) == 2
    assert (
        len(current.worker.prompts) == 0
        if round_number == 3
        else len(current.worker.prompts) > 0
    )


def test_finished_error_trial_is_kept_without_selective_retry(monkeypatch, tmp_path):
    old, _ = build(monkeypatch, tmp_path / "old", invalid_missing_frac=1)
    interrupted_rollout(old, monkeypatch, complete=5, error_trial=True)
    with pytest.raises(RuntimeError):
        asyncio.run(old.evolve())
    current, calls = build(monkeypatch, tmp_path / "new", invalid_missing_frac=1)
    CheckpointResume(old._run_dir).bind(current)
    asyncio.run(current.evolve())
    first = saved(current, "candidate")[0]["trajectories"]
    assert first[0]["status"] == "error"
    assert saved(current, "measurement")[1]["error_trials"] == 1
    assert len([call for call in calls if call[1] == "GOOD"]) == 3


@pytest.mark.parametrize(
    "mutation", ["model", "budget", "k", "task", "checksum", "truncated", "snapshot"]
)
def test_incompatible_or_corrupt_resume_fails_before_inference(
    monkeypatch, tmp_path, mutation
):
    old, _ = build(monkeypatch, tmp_path / "old")
    interrupted_rollout(old, monkeypatch, complete=1)
    with pytest.raises(RuntimeError):
        asyncio.run(old.evolve())
    current, calls = build(monkeypatch, tmp_path / "new")
    path = next((old._run_dir / "checkpoints/trials").glob("*.json"))
    if mutation == "model":
        current.target.config.llm.model_name = "different"
    elif mutation == "budget":
        current.worker.config.llm.max_tokens = 99
    elif mutation == "k":
        current.config.k = 99
    elif mutation == "task":
        current.benchmark._splits["train"] = (
            replace(current.benchmark.train[0], question="changed"),
            *current.benchmark.train[1:],
        )
    elif mutation == "truncated":
        path.write_text('{"version":')
    elif mutation == "checksum":
        value = json.loads(path.read_text())
        value["data"]["trajectory"]["output"] = "tampered"
        path.write_text(json.dumps(value))
    else:
        value = read(path)
        value["trajectory"]["messages"][0]["snapshot_id"] = "0" * 64
        atomic(path, value)
    with pytest.raises((ValueError, json.JSONDecodeError)):
        CheckpointResume(old._run_dir).bind(current)
    assert calls == []
    assert current.worker.prompts == []


def test_trial_resume_twice_and_final_test_trial_cache(monkeypatch, tmp_path):
    old, _ = build(monkeypatch, tmp_path / "old")
    interrupted_rollout(old, monkeypatch, complete=5)
    with pytest.raises(RuntimeError):
        asyncio.run(old.evolve())
    second, _ = build(monkeypatch, tmp_path / "second")
    CheckpointResume(old._run_dir).bind(second)
    interrupted_rollout(second, monkeypatch, complete=1)
    with pytest.raises(RuntimeError):
        asyncio.run(second.evolve())
    final, calls = build(monkeypatch, tmp_path / "final")
    CheckpointResume(second._run_dir).bind(final)
    snapshot = asyncio.run(final.evolve())
    asyncio.run(final._evaluate(snapshot, final.benchmark.test, label="test"))
    assert len(saved(final, "baseline")) == 2
    next_run, next_calls = build(monkeypatch, tmp_path / "last")
    CheckpointResume(final._run_dir).bind(next_run)
    snapshot = asyncio.run(next_run.evolve())
    asyncio.run(next_run._evaluate(snapshot, next_run.benchmark.test, label="test"))
    assert next_calls == []
    assert len(saved(next_run, "test")) == 1


def test_legacy_round_import(monkeypatch, tmp_path):
    import yaml

    from flashrsi._records import _redact_config
    from flashrsi.config import Config
    from flashrsi.core import Snapshot

    old, _ = build(monkeypatch, tmp_path / "old")
    old.worker._snapshot = Snapshot._capture(
        tmp_path / "old/source",
        agent_type=old.worker.name,
        component_rules=old.target.component_rules,
    )
    asyncio.run(old.evolve())
    config = Config(
        {
            "agent": old.target.config,
            "benchmark": {"name": old.benchmark.name},
            "algorithm": {**old.config, "name": "rrsi", "worker": old.worker.config},
        }
    )
    (old._run_dir / "config.yaml").write_text(yaml.safe_dump(_redact_config(config)))
    shutil.rmtree(old._run_dir / "checkpoints")
    current, calls = build(monkeypatch, tmp_path / "new")
    current.worker._snapshot = Snapshot._capture(
        tmp_path / "new/source",
        agent_type=current.worker.name,
        component_rules=current.target.component_rules,
    )
    resume = CheckpointResume(old._run_dir, config)
    resume.bind(current)
    result = asyncio.run(current.evolve())
    assert result.id == old.target.snapshot.id
    assert calls == []
    assert resume.metadata()["next_round"] == 4
    assert resume.mode == "round"
