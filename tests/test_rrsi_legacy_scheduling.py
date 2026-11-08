"""Switch legacy scheduling only after validating a complete round boundary."""

import asyncio
import hashlib
import shutil

import pytest
import yaml
from test_rrsi import saved
from test_rrsi_checkpoint import build

from flashrsi._records import _redact_config
from flashrsi.algorithms.rrsi._checkpoint import CheckpointResume, Checkpoints
from flashrsi.core import Snapshot


def capture_worker(algorithm, source):
    algorithm.worker._snapshot = Snapshot._capture(
        source,
        agent_type=algorithm.worker.name,
        component_rules=algorithm.target.component_rules,
    )


@pytest.fixture
def legacy(monkeypatch, tmp_path):
    old, _ = build(monkeypatch, tmp_path / "old", m=2)
    capture_worker(old, tmp_path / "old/source")
    original = Checkpoints.boundary

    def stop(checkpoint, state):
        original(checkpoint, state)
        if state["start_step"] == 1:
            raise RuntimeError("complete round interruption")

    with monkeypatch.context() as local:
        local.setattr(Checkpoints, "boundary", stop)
        with pytest.raises(RuntimeError, match="complete round interruption"):
            asyncio.run(old.evolve())
    config = {
        "agent": old.target.config,
        "benchmark": {"name": old.benchmark.name},
        "algorithm": {**old.config, "name": "rrsi", "worker": old.worker.config},
    }
    # Real old logs predate these fields; their missing values mean defaults.
    for key in (
        "shared_candidate_queue",
        "proposal_concurrency",
        "pipeline_evaluation",
    ):
        config["algorithm"].pop(key)
    (old._run_dir / "config.yaml").write_text(yaml.safe_dump(_redact_config(config)))
    shutil.rmtree(old._run_dir / "checkpoints")
    return old


@pytest.mark.parametrize(
    "scheduling",
    [
        {"shared_candidate_queue": True},
        {"proposal_concurrency": 2},
        {
            "shared_candidate_queue": True,
            "proposal_concurrency": 2,
            "pipeline_evaluation": True,
        },
    ],
)
def test_legacy_switch_preserves_history_and_only_runs_remaining_rounds(
    monkeypatch, tmp_path, legacy, scheduling
):
    reference, _ = build(monkeypatch, tmp_path / "reference", m=2)
    expected = asyncio.run(reference.evolve())
    before = {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in legacy._run_dir.rglob("*")
        if p.is_file()
    }
    current, calls = build(monkeypatch, tmp_path / "new", m=2, **scheduling)
    capture_worker(current, tmp_path / "new/source")
    resume = CheckpointResume(legacy._run_dir)
    resume.bind(current)
    assert resume.metadata()["next_round"] == 2
    assert resume.metadata()["legacy_scheduling_changes"] == {
        key: {"before": legacy.config[key], "after": value}
        for key, value in scheduling.items()
    }
    assert asyncio.run(current.evolve()).id == expected.id
    assert saved(current, "round-summary") == saved(reference, "round-summary")
    assert saved(current, "edit-history") == saved(reference, "edit-history")
    # Round two measures both candidates; round three proposes unchanged files.
    assert len(calls) == 8
    assert all(marker == "BEST" for _, marker, _ in calls)
    assert before == {
        str(p): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in legacy._run_dir.rglob("*")
        if p.is_file()
    }


@pytest.mark.parametrize(
    "mutation", ["T", "k", "cost_policy", "task_order", "budget", "concurrency"]
)
def test_legacy_switch_does_not_allow_protocol_or_execution_changes(
    monkeypatch, tmp_path, legacy, mutation
):
    current, calls = build(
        monkeypatch,
        tmp_path / "new",
        m=2,
        shared_candidate_queue=True,
        proposal_concurrency=2,
        pipeline_evaluation=True,
    )
    capture_worker(current, tmp_path / "new/source")
    if mutation in {"T", "k"}:
        current.config[mutation] += 1
    elif mutation == "cost_policy":
        current.config.cost_policy = "estimate"
    elif mutation == "task_order":
        current.config.task_order = "longest_first"
    elif mutation == "budget":
        current.worker.config.llm.max_tokens = 99
    else:
        current.target.config.concurrency = 99
    with pytest.raises(ValueError, match="configuration changed"):
        CheckpointResume(legacy._run_dir).bind(current)
    assert calls == []
    assert current.worker.prompts == []
