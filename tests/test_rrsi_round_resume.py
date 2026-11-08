"""Replay real saved evidence and continue with the selected candidate parent."""

import asyncio
import hashlib
import json

import pytest
import yaml
from test_rrsi import change, saved, setup

from flashrsi._records import _redact_config
from flashrsi.agents import MiniAgent
from flashrsi.algorithms.rrsi._round_resume import RoundOneResume
from flashrsi.config import Config
from flashrsi.core import Snapshot, Trajectory
from flashrsi.runner import _run


def hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def archived(monkeypatch, tmp_path, request):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD")
        return [change()]

    old, _ = setup(
        monkeypatch,
        tmp_path / "old",
        edit,
        T=2,
        k=2,
        delta=0.1,
        shared_candidate_queue=getattr(request, "param", False),
    )
    old.worker.config.llm.model_name = "offline-worker"
    old.worker.config.name = old.worker.name
    old.worker._snapshot = Snapshot._capture(
        tmp_path / "old/source",
        agent_type=old.worker.name,
        component_rules=old.target.component_rules,
    )
    old.worker.snapshot.save(old._run_dir / "snapshots/worker.json")
    counts = {}

    async def run(self, prompt, working_dir=None):
        marker = (self.files_dir / "skills/main.md").read_text()
        key = (marker, prompt)
        counts[key] = counts.get(key, 0) + 1
        missing = marker == "GOOD" and prompt == "train-0" and counts[key] == 2
        return Trajectory(
            output="0.55" if marker == "GOOD" else "0.5",
            usage={} if missing else {"total_tokens": 100},
        )

    monkeypatch.setattr(MiniAgent, "_run", run)
    analyze = old._analyze
    analyzed = 0

    async def stop_after_round_one(*args):
        nonlocal analyzed
        if analyzed:
            raise RuntimeError("user stopped round two")
        analyzed += 1
        return await analyze(*args)

    monkeypatch.setattr(old, "_analyze", stop_after_round_one)
    config = Config(
        {
            "run": {"output_dir": str(tmp_path / "resumed"), "progress": "off"},
            "benchmark": {"name": "rrsi-fixture", "seed": 42},
            "agent": old.target.config,
            "algorithm": {**old.config, "name": "rrsi", "worker": old.worker.config},
        }
    )
    (old._run_dir / "config.yaml").write_text(yaml.safe_dump(_redact_config(config)))
    with pytest.raises(RuntimeError, match="user stopped"):
        asyncio.run(old.evolve())
    assert saved(old, "edit-history")[0]["accepted"] is False
    # Stale later work must never enter the restored history or usage totals.
    old._record(
        "worker",
        {
            "prompt": "stale round two",
            "trajectory": {"usage": {"total_tokens": 999999}},
        },
    )
    next_contexts = []

    def next_edit(prompt, directory):
        assert (directory / "skills/main.md").read_text() == "GOOD"
        reference = prompt.split("`")[1]
        next_contexts.append(json.loads((directory / reference).read_text()))
        (directory / "skills/main.md").write_text("NEXT")
        return [change()]

    current, calls = setup(
        monkeypatch,
        tmp_path / "new",
        next_edit,
        T=2,
        k=2,
        delta=0.1,
        cost_policy="estimate",
        outcomes={"NEXT": (0.65, 100)},
    )
    current.worker.config.llm.model_name = "offline-worker"
    current.worker.config.name = current.worker.name
    current.worker._snapshot = Snapshot._capture(
        tmp_path / "new/source",
        agent_type=current.worker.name,
        component_rules=current.target.component_rules,
    )
    current.config.name = "rrsi"
    current.config.worker = current.worker.config
    config.algorithm.cost_policy = "estimate"
    return old, current, config, calls, next_contexts


@pytest.mark.parametrize("archived", [False, True], indirect=True)
def test_reselection_resumes_second_round_without_new_baseline_or_first_round_trials(
    archived,
):
    old, current, config, calls, contexts = archived
    config.algorithm.shared_candidate_queue = True
    current.config.shared_candidate_queue = True
    before = hashes(old._run_dir)
    resume = RoundOneResume(old._run_dir, config)
    resume.bind(current)
    selected = saved(old, "measurement")[-1]["snapshot_id"]
    assert resume.state["current"].id == selected
    assert (
        resume.metadata()["shared_candidate_queue_before"]
        is old.config.shared_candidate_queue
    )
    assert resume.metadata()["shared_candidate_queue_after"] is True
    assert resume.decisions[0]["admissible"]
    assert resume.decisions[0]["cost_comparison"] == "estimated_conservative"
    assert resume.revised[1]["cost_accounting"]["complete"] is False
    run_dir, result = asyncio.run(_run(config, algorithm=current))
    assert result["status"] == "finished"
    assert len(calls) == 5  # four round-two trials and one final heldout
    assert all(marker == "NEXT" for _, marker, _ in calls)
    assert len(contexts) == 1 and contexts[0]["step"] == 1
    assert contexts[0]["incumbent_id"] == selected
    assert contexts[0]["history"][0]["accepted"]
    summaries = [
        json.loads(p.read_text())
        for p in sorted((run_dir / "trajectories").glob("*-round-summary.json"))
    ]
    assert [row["round"] for row in summaries] == [1, 2]
    assert summaries[0]["current_score"] == 0.55
    assert not any(
        "stale round two" in p.read_text()
        for p in (run_dir / "trajectories").glob("*-worker.json")
    )
    assert (
        json.loads((run_dir / "resume.json").read_text())["reused_candidate_trials"]
        == 4
    )
    assert hashes(old._run_dir) == before


@pytest.mark.parametrize(
    "kind",
    ["model", "k", "metric", "snapshot", "task", "missing_repeat", "error_threshold"],
)
def test_invalid_replay_rejected_before_inference(archived, kind):
    old, current, config, calls, _ = archived
    if kind == "model":
        config.agent.llm.model_name = "different"
    elif kind == "k":
        config.algorithm.k = 3
    elif kind == "error_threshold":
        # Alter only the cached aggregate: raw data validation must catch it.
        path = sorted((old._run_dir / "trajectories").glob("*-measurement.json"))[0]
        value = json.loads(path.read_text())
        value["error_trials"] = 4
        path.write_text(json.dumps(value))
    else:
        path = sorted((old._run_dir / "trajectories").glob("*-candidate.json"))[0]
        if kind == "missing_repeat":
            path.unlink()
        else:
            value = json.loads(path.read_text())
            row = value["trajectories"][0]
            if kind == "metric":
                row["metrics"]["accuracy"] = 0.9
            elif kind == "task":
                row["task"]["question"] = "changed"
            else:
                for message in row["messages"]:
                    if message.get("type") == "RunConfig":
                        message["snapshot_id"] = "wrong"
            path.write_text(json.dumps(value))
    initial = current.target.snapshot.id
    with pytest.raises(ValueError):
        RoundOneResume(old._run_dir, config).bind(current)
    assert calls == [] and current.target.snapshot.id == initial


def test_changed_source_after_validation_cannot_be_imported(archived):
    old, current, config, calls, _ = archived
    resume = RoundOneResume(old._run_dir, config)
    resume.bind(current)
    path = old._run_dir / "config.yaml"
    path.write_text(path.read_text() + "\n# changed after validation\n")
    with pytest.raises(ValueError, match="source changed"):
        asyncio.run(_run(config, algorithm=current))
    assert calls == []
