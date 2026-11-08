import asyncio
import json
from dataclasses import replace

import pytest

from flashrsi.core import Agent, Algorithm, Benchmark, Snapshot, Task, Trajectory


class MemoryBenchmark(Benchmark):
    name = "memory"
    primary_metric = "accuracy"

    def __init__(self, *, env=None):
        super().__init__()
        self.loads = 0
        self.env = env

    def load(self):
        self.loads += 1
        self._splits = {
            "train": (),
            "dev": (),
            "test": (Task("test", "question", env=self.env),),
        }
        return self._splits

    def grade(self, trajectories):
        return trajectories


class Target(Agent):
    name = "test_target"

    async def _run(self, prompt, working_dir=None):
        raise AssertionError("initialization must not execute tasks")


class Worker(Agent):
    name = "test_worker"

    async def _run(self, prompt, working_dir=None):
        return Trajectory(output="unused")


class Example(Algorithm):
    evolve_scope = {"skill"}
    observation_scope = {"skill", "prompt"}
    _defaults = {"rounds": 2, "options": {"enabled": True}, "max_evidence_chars": 24000}

    def _check(self):
        assert self.benchmark.loads == 1
        assert self.config.rounds > 0
        with self.target.snapshot.check_out() as directory:
            assert (directory / "prompts/main.md").is_file()

    async def evolve(self):
        return self.target.snapshot


@pytest.fixture
def target(tmp_path):
    for component in ("skills", "prompts", "code"):
        (tmp_path / component).mkdir()
        (tmp_path / component / "main.md").write_text(component, newline="\n")
    (tmp_path / "components.yaml").write_text(
        "skill: ['skills/**']\nprompt: ['prompts/**']\ncode: ['code/**']\n",
        newline="\n",
    )
    return Target({"source_dir": tmp_path})


def test_base_initializes_scope_data_and_defaults_before_algorithm_hook(target):
    benchmark = MemoryBenchmark()
    config = {"rounds": 3}
    algo = Example(config, target=target, worker=Worker(), benchmark=benchmark)
    assert algo.config.rounds == 3
    assert config == {"rounds": 3}
    algo.config.options.enabled = False
    assert Example._defaults["options"]["enabled"] is True

    baseline = target.snapshot
    with baseline.check_out() as directory:
        assert {
            path.relative_to(directory).as_posix() for path in directory.rglob("*.md")
        } == {
            "skills/main.md",
            "prompts/main.md",
        }
        (directory / "skills/main.md").write_text("evolved", newline="\n")
        candidate = baseline.commit(directory)
    assert candidate.files["skills/main.md"].content == b"evolved"
    assert candidate.files["code/main.md"] == baseline.files["code/main.md"]

    with baseline.check_out() as directory:
        prompt = directory / "prompts/main.md"
        prompt.chmod(0o644)
        prompt.write_text("changed observation", newline="\n")
        with pytest.raises(ValueError, match="outside allowed components"):
            baseline.commit(directory)

    assert asyncio.run(algo.evolve()) is target.snapshot
    assert benchmark.loads == 1


def test_unknown_scope_is_rejected_by_agent_at_initialization(target):
    class Unknown(Algorithm):
        observation_scope = {"missing"}

    with pytest.raises(ValueError, match="unsupported components"):
        Unknown({}, target=target, worker=Worker(), benchmark=MemoryBenchmark())


def test_base_checks_environment_support_including_final_test(target):
    with pytest.raises(ValueError, match="environment-aware"):
        Algorithm(
            {}, target=target, worker=Worker(), benchmark=MemoryBenchmark(env="image")
        )

    class EnvironmentTarget(Target):
        async def rollout(self, tasks, *, snapshot=None):
            raise AssertionError("initialization must not execute tasks")

    adapted = EnvironmentTarget(target.config)
    algo = Algorithm(
        {}, target=adapted, worker=Worker(), benchmark=MemoryBenchmark(env="image")
    )
    assert algo.benchmark.test[0].env == "image"


def test_base_accepts_bound_environment_runner_and_text_worker(target):
    async def run_environment(agent, task):
        raise AssertionError("initialization must not execute tasks")

    target._env_runner = run_environment
    algo = Algorithm(
        {}, target=target, worker=Worker(), benchmark=MemoryBenchmark(env="image")
    )
    assert algo.benchmark.test[0].env == "image"
    assert type(target).rollout is Agent.rollout


def test_base_rejects_unimplemented_execution_before_loading_data(target):
    class MissingWorker(Agent):
        name = "missing_worker"

    benchmark = MemoryBenchmark()
    with pytest.raises(ValueError, match="worker.*_run"):
        Algorithm({}, target=target, worker=MissingWorker(), benchmark=benchmark)
    assert benchmark.loads == 0


def recording_algorithm(target, tmp_path, *, worker=None, benchmark=None):
    algo = Example(
        {},
        target=target,
        worker=worker or Worker(),
        benchmark=benchmark or MemoryBenchmark(),
    )
    algo._run_dir = tmp_path / "run"
    for name in ("snapshots", "trajectories", "working"):
        (algo._run_dir / name).mkdir(parents=True)
    return algo


def test_shared_text_candidate_respects_scope_and_preserves_other_files(
    target, tmp_path
):
    # Add a binary resource before creating the next target version.
    (target.config.source_dir / "skills/image.bin").write_bytes(b"\xff\xfe")
    target = Target(target.config)
    algo = recording_algorithm(target, tmp_path)
    baseline = target.snapshot
    assert algo._read_text(baseline) == {
        "prompts/main.md": "prompts",
        "skills/main.md": "skills",
    }
    assert algo._candidate(baseline, algo._read_text(baseline)) is baseline
    candidate = algo._candidate(
        baseline, {"skills/main.md": "changed", "skills/new.md": "new"}
    )
    assert target.snapshot is baseline
    assert candidate.parent_id == baseline.id
    assert candidate.files["skills/main.md"].content == b"changed"
    for path in ("skills/image.bin", "prompts/main.md", "code/main.md"):
        assert candidate.files[path] == baseline.files[path]
    assert (
        Snapshot.load(algo._run_dir / "snapshots" / f"{candidate.id}.json") == candidate
    )
    assert not list((algo._run_dir / "working").iterdir())

    with pytest.raises(ValueError, match="non-text"):
        algo._candidate(baseline, {"skills/image.bin": "replace binary"})
    # The checkout is read-only; privileged users reach commit's scope check.
    with pytest.raises((PermissionError, ValueError)):
        algo._candidate(baseline, {"prompts/main.md": "change readonly"})
    with pytest.raises(ValueError, match="hidden component"):
        algo._candidate(baseline, {"code/new.md": "change hidden"})
    assert target.snapshot is baseline
    assert not list((algo._run_dir / "working").iterdir())


@pytest.mark.parametrize(
    "path", ["../outside.md", "/tmp/algorithm-outside.md", "skills/../../outside.md"]
)
def test_shared_candidate_validates_paths_before_writing(target, tmp_path, path):
    algo = recording_algorithm(target, tmp_path)
    with pytest.raises(ValueError, match="invalid snapshot file path"):
        algo._candidate(target.snapshot, {path: "escape"})
    assert not list((algo._run_dir / "working").iterdir())
    assert not list((algo._run_dir / "snapshots").iterdir())


@pytest.mark.parametrize(
    "kwargs,record_label",
    [({}, "evaluation"), ({"label": "candidate_dev"}, "candidate_dev")],
)
def test_shared_evaluation_records_scores_without_selecting_candidate(
    target, tmp_path, monkeypatch, kwargs, record_label
):
    class GradedBenchmark(MemoryBenchmark):
        def grade(self, trajectories):
            # The benchmark's own primary_metric; an evaluation is read through
            # it, so grading some other name would be the benchmark's bug.
            return tuple(replace(t, metrics={"accuracy": 1.0}) for t in trajectories)

    async def run(self, prompt, working_dir=None):
        return Trajectory(
            output=(self.files_dir / "skills/main.md").read_text(), usage={"tokens": 5}
        )

    monkeypatch.setattr(Target, "_run", run)
    algo = recording_algorithm(target, tmp_path, benchmark=GradedBenchmark())
    baseline = target.snapshot
    candidate = algo._candidate(baseline, {"skills/main.md": "candidate answer"})
    results = asyncio.run(
        algo._evaluate(candidate, iter(algo.benchmark.test), **kwargs)
    )
    assert results[0].output == "candidate answer"
    assert results[0].metrics["accuracy"] == 1.0
    assert results.score == 1.0
    assert results[0].task is algo.benchmark.test[0]
    assert target.snapshot is baseline
    (path,) = (algo._run_dir / "trajectories").iterdir()
    assert path.name == f"00001-{record_label}.json"
    recorded = json.loads(path.read_text())
    assert recorded["snapshot_id"] == candidate.id
    assert recorded["trajectories"][0]["usage"] == {"tokens": 5}
    # Timing metadata can precede RunConfig when the backend has no messages.
    (run_config,) = (
        message
        for message in recorded["trajectories"][0]["messages"]
        if message.get("type") == "RunConfig"
    )
    assert run_config["snapshot_id"] == candidate.id


def test_worker_questions_are_recorded_and_redacted_against_both_roles(
    target, tmp_path, monkeypatch
):
    """An algorithm arranges none of this; asking is enough. See Agent.ask."""

    class FailedWorker(Worker):
        async def _run(self, prompt, working_dir=None):
            return Trajectory(
                status="error",
                error_message="target-secret worker-secret environment-secret",
                messages=({"type": "Partial", "text": "worker-secret"},),
                usage={"tokens": 3},
            )

    target.config.llm = {"api_key": "target-secret"}
    worker = FailedWorker({"llm": {"api_key": "worker-secret"}})
    monkeypatch.setenv("TEST_API_KEY", "environment-secret")
    algo = recording_algorithm(target, tmp_path, worker=worker)
    with pytest.raises(RuntimeError) as error:
        asyncio.run(algo.worker.ask("prompt target-secret"))
    # A single Worker holds only its own credentials, so the error is redacted
    # against the target's and the environment's as well.
    assert str(error.value) == "test_worker failed: [REDACTED] [REDACTED] [REDACTED]"
    (path,) = (algo._run_dir / "trajectories").iterdir()
    assert path.name == "00001-worker.json"
    text = path.read_text()
    assert all(
        secret not in text
        for secret in ("target-secret", "worker-secret", "environment-secret")
    )
    assert json.loads(text)["trajectory"]["usage"] == {"tokens": 3}
    with pytest.raises(ValueError, match="credential"):
        algo._candidate(target.snapshot, {"skills/main.md": "target-secret"})
    assert not list((algo._run_dir / "working").iterdir())


def test_an_unobserved_agent_still_answers_and_raises(target):
    """A Worker used directly, outside an Algorithm, needs no observer."""

    class Failing(Worker):
        async def _run(self, prompt, working_dir=None):
            return Trajectory(status="error", error_message="no model configured")

    assert asyncio.run(Worker().ask("question")) == "unused"
    with pytest.raises(RuntimeError, match="no model configured"):
        asyncio.run(Failing().ask("question"))


# -- shared parameter checking, scoring and promotion ----------------------


def test_declared_ranges_are_checked_before_any_collaborator_is_touched(target):
    """``_ranges`` replaces the per-algorithm type tests each _check used to run."""

    class Ranged(Algorithm):
        evolve_scope = {"skill"}
        _defaults = {
            "steps": 4,
            "budget": None,
            "threshold": 0.5,
            "strategy": "pareto",
            "merge": False,
            "seed": 1,
        }
        _ranges = {
            "steps": (int, 1),
            "budget": (int | None, 1),
            "threshold": float,
            "strategy": ("pareto", "current_best"),
            "merge": bool,
            "seed": int,
        }

    def build(**config):
        benchmark = MemoryBenchmark()
        Ranged(config, target=target, worker=Worker(), benchmark=benchmark)
        return benchmark

    # Defaults, an unset optional, and every accepted value pass.
    assert build().loads == 1
    assert build(budget=10, steps=1, threshold=0, strategy="current_best").loads == 1

    for config, message in (
        ({"steps": 0}, "steps must be an integer >= 1"),
        ({"steps": 1.5}, "steps must be an integer"),
        ({"steps": True}, "steps must be an integer"),
        # Only a key declared ``| None`` may be unset.
        ({"steps": None}, "steps must be an integer"),
        ({"budget": 0}, "budget must be an integer >= 1 or null"),
        ({"threshold": float("nan")}, "threshold must be finite"),
        ({"threshold": "0.5"}, "threshold must be a number"),
        ({"strategy": "greedy"}, "strategy must be one of: pareto, current_best"),
        ({"merge": "yes"}, "merge must be a boolean"),
        ({"seed": "1"}, "seed must be an integer"),
    ):
        benchmark = MemoryBenchmark()
        with pytest.raises(ValueError, match=message):
            Ranged(config, target=target, worker=Worker(), benchmark=benchmark)
        # Rejected before the benchmark is loaded or the scope applied.
        assert benchmark.loads == 0


def test_an_accepted_spelling_is_canonicalized_before_it_is_checked(target):
    """An upstream CLI name reaches ``_ranges`` already rewritten."""

    class Aliased(Algorithm):
        evolve_scope = {"skill"}
        _defaults = {"selection": "wheel"}
        _ranges = {"selection": ("wheel", "tournament")}

        def _normalize(self):
            if self.config.selection == "tour":
                self.config.selection = "tournament"

    algo = Aliased(
        {"selection": "tour"},
        target=target,
        worker=Worker(),
        benchmark=MemoryBenchmark(),
    )
    # Canonicalized, so the loop compares against one name and the saved
    # configuration records what actually ran.
    assert algo.config.selection == "tournament"


def test_shared_promotion_selects_once_and_records_the_pool(target, tmp_path):
    algo = recording_algorithm(target, tmp_path)
    current = target.snapshot
    candidate = algo._candidate(current, {"skills/main.md": "evolved"})

    final = algo._promote(current, candidate, score=1.0, pool=["a", "b"])

    assert final is target.snapshot and final.id == candidate.id
    (record,) = (algo._run_dir / "trajectories").glob("*-decision.json")
    decision = json.loads(record.read_text())
    assert decision["kind"] == "promote" and decision["accepted"] is True
    assert decision["parent_id"] == current.id
    assert decision["candidate_id"] == candidate.id
    # Whatever the algorithm's own search state is, recorded beside the decision.
    assert decision["score"] == 1.0 and decision["pool"] == ["a", "b"]


def test_shared_promotion_of_the_incumbent_records_it_as_not_accepted(target, tmp_path):
    algo = recording_algorithm(target, tmp_path)
    current = target.snapshot

    final = algo._promote(current, current, score=0.0)

    assert final is current
    (record,) = (algo._run_dir / "trajectories").glob("*-decision.json")
    decision = json.loads(record.read_text())
    assert decision["accepted"] is False
    assert decision["parent_id"] == decision["candidate_id"] == current.id


def test_shared_evidence_is_json_and_marks_a_truncating_cut(target, tmp_path):
    algo = recording_algorithm(target, tmp_path)
    rows = [{"b": 2, "a": 1}]

    assert algo._evidence(rows) == '[{"a": 1, "b": 2}]'
    algo.config.max_evidence_chars = 8
    # The cut lands mid-JSON, so it is marked rather than passed off as whole.
    assert algo._evidence(rows) == '[{"a": 1…'


def test_shared_trajectory_evidence_drops_the_runtime_configuration(target, tmp_path):
    algo = recording_algorithm(target, tmp_path)
    trajectory = replace(
        Trajectory(output="answer"),
        messages=({"type": "RunConfig", "config": {}}, {"role": "assistant"}),
    )

    row = algo._trajectory_evidence(trajectory)

    # The saved record keeps RunConfig; a Worker prompt never shows it.
    assert row["messages"] == [{"role": "assistant"}]
    assert row["output"] == "answer"


def test_a_wildcard_scope_resolves_to_every_component_the_target_declares(
    target, tmp_path
):
    """A method that evolves the harness as a directory cannot name them."""

    class Whole(Example):
        evolve_scope = {"*"}
        observation_scope = frozenset()

        def _check(self):
            pass

    algo = Whole({}, target=target, worker=Worker(), benchmark=MemoryBenchmark())
    assert algo._evolve == frozenset({"skill", "prompt", "code"})
    baseline = target.snapshot
    # Every component is editable, including the one an ordinary scope hides.
    with baseline.check_out() as directory:
        (directory / "code/main.md").write_text("evolved", newline="\n")
        (directory / "code/added.md").write_text("new", newline="\n")
        candidate = baseline.commit(directory)
    assert candidate.files["code/main.md"].content == b"evolved"
    assert candidate.files["code/added.md"].component == "code"
    assert algo._read_text(baseline)["code/main.md"] == "code"
