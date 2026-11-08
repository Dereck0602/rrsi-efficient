"""Frozen cross-benchmark evaluation, separate from search and Worker setup."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from flashrsi import _launch, cli, runner
from flashrsi.config import load_config
from flashrsi.core import Agent, Benchmark, Snapshot, Task, Trajectory


class FrozenAgent(Agent):
    name = "fixture"
    calls = []

    async def _run(self, prompt, working_dir=None):
        self.calls.append(prompt)
        return Trajectory(output=(self.files_dir / "prompt.md").read_text())


class Destination(Benchmark):
    name = "destination"
    primary_metric = "accuracy"
    metadata = {"source": "bench-B"}

    def __init__(self, config):
        super().__init__()
        self.config = config

    def load(self):
        self._splits = {
            "train": (Task("train", "DO NOT RUN TRAIN"),),
            "dev": (Task("dev", "DO NOT RUN DEV"),),
            "test": ()
            if self.config.get("empty")
            else (Task("test", "bench-B test", "evolved"),),
        }
        return self._splits

    def grade(self, trajectories):
        if self.config.get("fail_grade"):
            raise RuntimeError("grading failed secret-key")
        return tuple(
            replace(t, metrics={"accuracy": float(t.output == t.task.ground_truth)})
            for t in trajectories
        )


@pytest.fixture
def evaluation(tmp_path, monkeypatch):
    source = tmp_path / "agent"
    source.mkdir()
    (source / "components.yaml").write_text("prompt: [prompt.md]\n")
    (source / "prompt.md").write_text("baseline")
    agent_config = {
        "name": "fixture",
        "source_dir": str(source),
        "llm": {"model_name": "target-B", "api_key": "secret-key"},
    }
    target = FrozenAgent(agent_config)
    seed = target.snapshot
    with seed.check_out(working_dir=tmp_path) as directory:
        (directory / "prompt.md").write_text("evolved")
        evolved = seed.commit(directory)
    path = tmp_path / "from-A.json"
    evolved.save(path)
    monkeypatch.setitem(runner._AGENTS, "fixture", FrozenAgent)
    monkeypatch.setitem(runner._BENCHMARKS, "destination", Destination)
    # Any attempt to construct a search algorithm is a test failure.
    monkeypatch.setattr(runner, "_ALGORITHMS", {})
    FrozenAgent.calls = []
    config = load_config(
        overrides={
            "run": {
                "mode": "evaluate",
                "snapshot": str(path),
                "output_dir": str(tmp_path / "runs"),
            },
            "agent": agent_config,
            "benchmark": {"name": "destination"},
            "algorithm": {"name": "missing-algorithm", "worker": {"type": "invalid"}},
        }
    )
    return config, evolved, source


def test_evaluate_only_test_preserves_snapshot_and_records(evaluation, capsys):
    config, snapshot, source = evaluation
    before = Path(config.run.snapshot).read_bytes()
    run_dir, result = asyncio.run(runner._evaluate(config))
    progress = capsys.readouterr().err
    assert "final-test | tasks" in progress
    assert "1/1 (100%) active=0" in progress
    assert FrozenAgent.calls == ["bench-B test"]
    assert result["test_score"] == 1
    assert result["snapshot_id"] == snapshot.id
    assert result["source"]["root_id"] == snapshot.root_id
    assert (source / "prompt.md").read_text() == "baseline"
    assert Path(config.run.snapshot).read_bytes() == before
    assert (
        Snapshot.load(run_dir / "snapshots" / f"{snapshot.id}.json").id == snapshot.id
    )
    saved = load_config(run_dir / "config.yaml")
    assert dict(saved.algorithm) == {"name": "evaluation"}
    assert saved.agent.llm.model_name == "target-B"
    assert "secret-key" not in (run_dir / "config.yaml").read_text()
    assert (run_dir / "end-summary.json").is_file()
    second, _ = asyncio.run(runner._evaluate(config))
    assert second != run_dir


def test_cli_check_only_has_no_worker_calls_or_output(evaluation, tmp_path):
    config, _, _ = evaluation
    path = tmp_path / "evaluation.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "agent": {
                    "name": "fixture",
                    "source_dir": config.agent.source_dir,
                    "llm": {"model_name": "target"},
                },
                "benchmark": {"name": "destination"},
            }
        )
    )
    assert (
        cli.main(
            [
                "evaluate",
                "--config",
                str(path),
                "--snapshot",
                config.run.snapshot,
                "--output-dir",
                config.run.output_dir,
                "--check-only",
            ]
        )
        == 0
    )
    assert FrozenAgent.calls == []
    assert not Path(config.run.output_dir).exists()


@pytest.mark.parametrize("bad", ["empty", "root", "type", "tampered"])
def test_invalid_evaluation_fails_before_execution(evaluation, bad):
    config, _, source = evaluation
    if bad == "empty":
        config.benchmark.empty = True
    elif bad == "root":
        (source / "prompt.md").write_text("different initial root")
    elif bad == "type":

        class Other(FrozenAgent):
            name = "other"

        runner._AGENTS["fixture"] = Other
    else:
        path = Path(config.run.snapshot)
        record = json.loads(path.read_text())
        record["id"] = "0" * 64
        path.write_text(json.dumps(record))
    with pytest.raises(ValueError):
        asyncio.run(runner._evaluate(config))
    assert FrozenAgent.calls == []
    assert not Path(config.run.output_dir).exists()


def test_grading_failure_preserves_trace_and_redacts_error(evaluation):
    config, _, _ = evaluation
    config.benchmark.fail_grade = True
    with pytest.raises(RuntimeError):
        asyncio.run(runner._evaluate(config))
    run_dir = next(Path(config.run.output_dir).iterdir())
    assert (run_dir / "trajectories/000001-rollout.json").is_file()
    assert "secret-key" not in (run_dir / "error.json").read_text()
    assert not (run_dir / "result.json").exists()


def test_launcher_plan_requires_only_target_and_benchmark(tmp_path, monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("FLASHRSI_"):
            monkeypatch.delenv(key)
    path = tmp_path / "target.yaml"
    path.write_text(
        "agent:\n  name: miniagent\n  llm: {model_name: target, api_key: local}\nbenchmark:\n  name: searchqa\n  data_path: data.jsonl\n"
    )
    config = _launch._load(path, tmp_path / "snapshot.json")
    plan = _launch._plan(config)
    assert plan["worker"] is None
    assert plan["extras"] == ["all"]
    assert config.benchmark.data_path == str(tmp_path / "data.jsonl")
    _launch._validate_roles(config)
    with pytest.raises(ValueError, match="worker.llm.model_name"):
        load_config(path)


def test_destination_environment_binding_receives_selected_snapshot(
    evaluation, monkeypatch
):
    from flashrsi.integrations import workbench

    config, snapshot, _ = evaluation
    bound = []

    class EnvironmentDestination(Destination, runner.WorkBench):
        def __init__(self, config):
            Benchmark.__init__(self)
            self.config = config

        def load(self):
            splits = super().load()
            splits["test"] = tuple(
                replace(t, env="destination-environment") for t in splits["test"]
            )
            return splits

    def bind(target, benchmark):
        async def execute(agent, task):
            bound.append((agent.snapshot.id, task.env, agent.config.llm.model_name))
            return Trajectory(output="evolved")

        target._env_runner = execute

    monkeypatch.setitem(runner._BENCHMARKS, "destination", EnvironmentDestination)
    monkeypatch.setattr(workbench, "bind", bind)
    _, result = asyncio.run(runner._evaluate(config))
    assert result["test_score"] == 1
    assert bound == [(snapshot.id, "destination-environment", "target-B")]
    assert FrozenAgent.calls == []


def test_launcher_forwards_snapshot_and_evaluation_needs_no_worker(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from scripts import run_experiment as launcher

    path = tmp_path / "evaluation.yaml"
    path.write_text(
        "agent:\n  name: miniagent\n  llm: {model_name: target, api_key: local}\nbenchmark: {name: searchqa}\n"
    )
    snapshot = tmp_path / "selected.json"
    monkeypatch.setattr(_launch, "_validate_host", lambda cfg: None)
    runtime = tmp_path / "env/Scripts/python.exe"
    profiles = []
    commands = []

    def install(cache, env, extras, requirements):
        profiles.append(extras)
        return runtime

    monkeypatch.setattr(launcher, "_runtime", install)
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda command, **kw: commands.append(command) or SimpleNamespace(returncode=0),
    )
    assert (
        launcher.main(
            [
                "--_launch",
                "--config",
                str(path),
                "--snapshot",
                str(snapshot),
                "--check-only",
            ]
        )
        == 0
    )
    assert profiles == [["all"]]
    assert commands[0][commands[0].index("--snapshot") + 1] == str(snapshot)
    assert "--check-only" in commands[0]
