"""Baseline restoration reuses evidence only for the exact saved execution."""

import asyncio
import json

import pytest
import yaml
from test_rrsi import change, setup

from flashrsi._records import _redact_config
from flashrsi.algorithms.rrsi._resume import BaselineResume
from flashrsi.config import Config
from flashrsi.core import Snapshot
from flashrsi.runner import _run


@pytest.fixture
def baseline(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD")
        return [change()]

    original, _ = setup(monkeypatch, tmp_path / "old", edit, k=2)
    original.worker.config.llm.model_name = "offline-worker"
    original.worker.config.name = original.worker.name
    original.worker._snapshot = Snapshot._capture(
        tmp_path / "old/source",
        agent_type=original.worker.name,
        component_rules=original.target.component_rules,
    )
    original.worker.snapshot.save(original._run_dir / "snapshots/worker.json")
    config = Config(
        {
            "run": {"output_dir": str(tmp_path / "resumed"), "progress": "off"},
            "benchmark": {"name": "rrsi-fixture", "seed": 42},
            "agent": original.target.config,
            "algorithm": {
                **original.config,
                "name": "rrsi",
                "worker": original.worker.config,
            },
        }
    )
    (original._run_dir / "config.yaml").write_text(
        yaml.safe_dump(_redact_config(config))
    )

    async def measure():
        for _ in range(2):
            await original._evaluate(
                original.target.snapshot, original.benchmark.train, label="baseline"
            )

    asyncio.run(measure())
    current, calls = setup(
        monkeypatch, tmp_path / "new", edit, k=2, invalid_missing_frac=0.25
    )
    current.worker.config.llm.model_name = "offline-worker"
    current.worker.config.name = current.worker.name
    current.config.name = "rrsi"
    current.config.worker = current.worker.config
    # Simulate local runtime edits made after the baseline was measured.
    (tmp_path / "new/source/tools/runtime.py").write_text("value = 999\n")
    current.target._snapshot = Snapshot._capture(
        tmp_path / "new/source",
        agent_type=current.target.name,
        component_rules=current.target.component_rules,
    )._with_scope(current.target.snapshot._scope)
    current.worker._snapshot = Snapshot._capture(
        tmp_path / "new/source",
        agent_type=current.worker.name,
        component_rules=current.target.component_rules,
    )
    config.algorithm.invalid_missing_frac = 0.25
    return original, current, config, calls


def test_runner_continues_with_saved_roots_without_rerunning_baseline(baseline):
    old, current, config, calls = baseline
    config.algorithm.digest_concurrency = 8
    current.config.digest_concurrency = 8
    config.algorithm.task_order = "longest_first"
    current.config.task_order = "longest_first"
    before = {str(p): p.read_bytes() for p in old._run_dir.rglob("*") if p.is_file()}
    assert current.target.snapshot.id != old.target.snapshot.id
    resume = BaselineResume(old._run_dir, config)
    resume.bind(current)
    assert current.target.snapshot.id == old.target.snapshot.id
    assert current.worker.snapshot.id == old.worker.snapshot.id
    run_dir, result = asyncio.run(_run(config, algorithm=current))
    # 2 tasks * 2 candidate repeats + 1 heldout; zero additional baseline calls.
    assert len(calls) == 5
    assert all(marker == "GOOD" for _, marker, _ in calls)
    assert calls[-1][2] == "test-private"
    assert result["status"] == "finished"
    metadata = json.loads((run_dir / "resume.json").read_text())
    assert metadata["reused_baseline_trials"] == 4
    assert metadata["invalid_missing_frac_before"] == 0.15
    assert metadata["invalid_missing_frac_after"] == 0.25
    assert metadata["digest_concurrency_before"] == 1
    assert metadata["digest_concurrency_after"] == 8
    assert metadata["task_order_before"] == "fifo"
    assert metadata["task_order_after"] == "longest_first"
    assert metadata["source_sha256"]
    records = list((run_dir / "trajectories").glob("*-baseline.json"))
    assert len(records) == 2
    assert all(
        json.loads(p.read_text())["reused_from"] == str(old._run_dir) for p in records
    )
    measurement = json.loads(
        sorted((run_dir / "trajectories").glob("*-measurement.json"))[-1].read_text()
    )
    assert measurement["scheduling"]["policy"] == "longest_first"
    assert measurement["scheduling"]["known_tasks"] == 2
    summary = json.loads((run_dir / "end-summary.json").read_text())
    assert summary["resume"]["reused_baseline_trials"] == 4
    assert summary["usage"]["roles"]["target"]["finished"]["records"] == 9
    assert before == {
        str(p): p.read_bytes() for p in old._run_dir.rglob("*") if p.is_file()
    }


@pytest.mark.parametrize(
    "change_kind",
    [
        "model",
        "budget",
        "k",
        "benchmark",
        "extra_record",
        "incomplete",
        "completed",
        "task",
        "snapshot",
        "metric",
    ],
)
def test_incompatible_resume_fails_before_any_new_inference(baseline, change_kind):
    old, current, config, calls = baseline
    records = sorted((old._run_dir / "trajectories").glob("*.json"))
    if change_kind == "model":
        config.agent.llm.model_name = "different-model"
    elif change_kind == "budget":
        config.agent.llm.max_tokens = 32768
    elif change_kind == "k":
        config.algorithm.k = 3
    elif change_kind == "benchmark":
        config.benchmark.seed = 100
    elif change_kind == "extra_record":
        (old._run_dir / "trajectories/00003-worker.json").write_text("{}")
    elif change_kind == "incomplete":
        records[-1].unlink()
    elif change_kind == "completed":
        (old._run_dir / "result.json").write_text("{}")
    else:
        row = json.loads(records[0].read_text())
        if change_kind == "task":
            row["trajectories"][0]["task"]["question"] = "changed task"
        elif change_kind == "snapshot":
            for message in row["trajectories"][0]["messages"]:
                if message.get("type") == "RunConfig":
                    message["snapshot_id"] = "wrong"
        else:
            row["trajectories"][0]["metrics"] = {}
        records[0].write_text(json.dumps(row))
    initial = current.target.snapshot.id
    with pytest.raises((ValueError, KeyError)):
        BaselineResume(old._run_dir, config).bind(current)
    assert calls == []
    assert current.target.snapshot.id == initial
