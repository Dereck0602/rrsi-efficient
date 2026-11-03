"""Native rows, answer protocols, evaluation-pool isolation and launcher wiring."""

import asyncio
import json
from dataclasses import replace

import pytest

from flashrsi import _launch
from flashrsi.benchmarks import BBEH, SuperGPQA
from flashrsi.config import load_config
from flashrsi.core import Agent, Task, Trajectory
from flashrsi.runner import _BENCHMARKS


def bbeh_row(i=0):
    return {"task": "counting", "input": f"Count item {i}.", "target": "SECRET_LABEL"}


def super_row(i=0):
    return {
        "uuid": f"question-{i}",
        "question": f"Choose item {i}.",
        "options": [f"option-{j}" for j in range(10)],
        "answer": "option-8",
        "answer_letter": "I",
        "discipline": "Science",
        "difficulty": "hard",
        "explanation": "SECRET_EXPLANATION",
    }


CASES = [(BBEH, bbeh_row), (SuperGPQA, super_row)]


@pytest.mark.parametrize("cls,row", CASES)
def test_partition_grading_and_reference_integrity(tmp_path, cls, row):
    path = tmp_path / "test.json"
    path.write_text(json.dumps([row(i) for i in range(8)]), encoding="utf-8")
    cfg = {
        "data_path": str(path),
        "split_counts": {"train": 4, "dev": 2, "test": 2},
        "seed": 42,
    }
    bench = cls(cfg)
    splits = bench.load()
    assert splits == cls(cfg).load()
    assert {k: len(v) for k, v in splits.items()} == {"train": 4, "dev": 2, "test": 2}
    assert len({t.task_id for group in splits.values() for t in group}) == 8
    task = bench.test[0]
    good, bad, failed = bench.grade(
        (
            Trajectory(task=task, output=task.ground_truth),
            Trajectory(task=task, output="INVALID"),
            Trajectory(
                task=task,
                output=task.ground_truth,
                status="error",
                error_message="fixture failure",
            ),
        )
    )
    assert good.metrics == {"accuracy": 1.0}
    assert bad.metrics == failed.metrics == {"accuracy": 0.0}
    assert good.output == task.ground_truth
    # A trajectory's attached reference cannot change the loaded answer key.
    forged = replace(task, ground_truth="INVALID")
    assert bench.grade((Trajectory(task=forged, output="INVALID"),))[0].metrics == {
        "accuracy": 0.0
    }
    with pytest.raises(ValueError, match="unknown"):
        bench.grade((Trajectory(task=replace(task, task_id="unknown")),))
    with pytest.raises(ValueError, match="requires a Task"):
        bench.grade((Trajectory(output="x"),))


@pytest.mark.parametrize("cls,row", CASES)
def test_native_download_pin_and_no_implicit_training(tmp_path, monkeypatch, cls, row):
    filename = "bbeh_data.json" if cls is BBEH else "SuperGPQA-all.jsonl"
    path = tmp_path / filename
    path.write_text(
        json.dumps([row()]) if cls is BBEH else json.dumps(row()) + "\n",
        encoding="utf-8",
    )
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return str(path)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", download)
    bench = cls({})
    splits = bench.load()
    assert splits["train"] == splits["dev"] == ()
    assert len(splits["test"]) == 1
    assert bench.load() is splits
    assert calls == [
        {
            "repo_id": cls.metadata["source"],
            "revision": cls.metadata["revision"],
            "repo_type": "dataset",
            "filename": filename,
        }
    ]
    if cls is BBEH:
        assert bench.metadata["scorer_revision"] == cls._scorer_revision


@pytest.mark.parametrize("cls,row", CASES)
def test_explicit_local_roles_and_duplicate_detection(tmp_path, cls, row):
    train = tmp_path / "part-a.json"
    test = tmp_path / "part-b.json"
    train.write_text(json.dumps([row(0)]), encoding="utf-8")
    test.write_text(json.dumps([row(1)]), encoding="utf-8")
    bench = cls({"data_path": {"train": str(train), "test": str(test)}})
    bench.load()
    assert len(bench.train) == len(bench.test) == 1
    assert not bench.dev
    # Standalone unassigned data must request a partition or explicit role.
    with pytest.raises(ValueError, match="No experiment role"):
        cls({"data_path": str(train)}).load()
    test.write_text(json.dumps([row(0)]), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        cls({"data_path": {"train": str(train), "test": str(test)}}).load()


def test_bbeh_explicit_duplicate_exclusion_removes_whole_groups(tmp_path, caplog):
    path = tmp_path / "test.json"
    rows = [
        bbeh_row(0),
        {**bbeh_row(0), "target": "CONTRADICTORY_LABEL"},
        bbeh_row(1),
        bbeh_row(1),
        bbeh_row(2),
        bbeh_row(3),
        bbeh_row(4),
    ]
    path.write_text(json.dumps(rows), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate_questions: exclude"):
        BBEH({"data_path": str(path)}).load()
    bench = BBEH(
        {
            "data_path": str(path),
            "duplicate_questions": "exclude",
            "split_counts": {"train": 1, "dev": 1, "test": 1},
        }
    )
    splits = bench.load()
    assert all(len(group) == 1 for group in splits.values())
    assert {t.question for group in splits.values() for t in group} == {
        f"Count item {i}." for i in (2, 3, 4)
    }
    assert bench.metadata["source_row_count"] == 7
    assert bench.metadata["excluded_duplicate_rows"] == 4
    assert len(bench.metadata["excluded_question_ids"]) == 2
    assert "filtered experiment pool" in caplog.text
    with pytest.raises(ValueError, match="duplicate_questions"):
        BBEH({"duplicate_questions": "guess"})


@pytest.mark.parametrize("cls,row", CASES)
def test_no_label_or_explanation_in_agent_prompt(cls, row):
    task = cls._task_from_row(row(), "test")
    assert "SECRET" not in task.question
    assert task.env == "supplied_context"

    class Recorder(Agent):
        name = "prompt-recorder"

        async def _run(self, prompt, working_dir=None):
            assert prompt == task.question
            assert "SECRET" not in prompt
            return Trajectory(output="A")

    with pytest.raises(NotImplementedError, match="bind an execution integration"):
        asyncio.run(Recorder().rollout((task,)))


@pytest.mark.parametrize("field,value", [("task", ""), ("input", None), ("target", 2)])
def test_bbeh_invalid_rows(field, value):
    with pytest.raises(ValueError, match=field):
        BBEH._task_from_row({**bbeh_row(), field: value}, "test")


@pytest.mark.parametrize(
    "field,value",
    [
        ("uuid", ""),
        ("question", None),
        ("options", ["x"]),
        ("options", ["x"] * 11),
        ("options", [None] * 10),
        ("answer_letter", "K"),
        ("answer_letter", "AB"),
        ("answer_letter", "A"),
        ("answer", "mismatched"),
    ],
)
def test_supergpqa_invalid_rows(field, value):
    with pytest.raises(ValueError):
        SuperGPQA._task_from_row({**super_row(), field: value}, "test")


@pytest.mark.parametrize("count", range(4, 11))
def test_supergpqa_native_variable_option_counts(count):
    row = super_row()
    row.update(options=row["options"][:count], answer_letter="A", answer="option-0")
    task = SuperGPQA._task_from_row(row, "test")
    assert f"(A-{chr(64 + count)})" in task.question
    assert SuperGPQA({})._score("A", task) == {"accuracy": 1.0}
    row["answer_letter"] = chr(65 + count)
    with pytest.raises(ValueError, match="answer_letter"):
        SuperGPQA._task_from_row(row, "test")


@pytest.mark.parametrize(
    "prediction,reference,score",
    [
        (r"Reasoning. The final answer is: \boxed{4}.", "4", 1),
        ("The answer is: (A)", "a", 1),
        ("The final answer is: 2, 3, 4", "2,3,4", 1),
        ("The answer is: **25**\nHere's why.", "25.0", 1),
        ("The answer is: no", "yes", 0),
        ("", "4", 0),
    ],
)
def test_bbeh_upstream_protocol(prediction, reference, score):
    task = Task("q", "question", reference)
    assert BBEH({})._score(prediction, task) == {"accuracy": score}


@pytest.mark.parametrize(
    "prediction,score",
    [
        ("I", 1),
        ("reasoning mentions A\n\ni\n", 1),
        ("Answer: I.", 1),
        ("A B C D E F G H I J", 0),
        ("I\nAnswer: A", 0),
        ("The answer might be I or J", 0),
        ("option-8", 0),
        ("", 0),
    ],
)
def test_supergpqa_final_letter_protocol(prediction, score):
    task = SuperGPQA._task_from_row(super_row(), "test")
    assert SuperGPQA({})._score(prediction, task) == {"accuracy": score}


@pytest.mark.parametrize("cls,row", CASES)
def test_registration_planning_and_unsupported_protocols(cls, row):
    assert _BENCHMARKS[cls.name] is cls
    cfg = load_config(
        overrides={
            "benchmark": {"name": cls.name},
            "agent": {"name": "miniagent", "llm": {"model_name": "test"}},
            "algorithm": {
                "name": "opro",
                "worker": {"type": "llm", "llm": {"model_name": "test"}},
            },
        }
    )
    plan = _launch._plan(cfg)
    assert plan["extras"] == ["all"]
    assert plan["prerequisites"] == ["Docker CLI and a running Linux daemon"]
    with pytest.raises(ValueError, match="scoring"):
        cls({"scoring": "legacy"})
    with pytest.raises(ValueError, match="mode"):
        cls({"mode": "fullwiki"})
    with pytest.raises(RuntimeError, match="call load"):
        cls({}).grade(())
    if cls is SuperGPQA:
        with pytest.raises(ValueError, match="scoring"):
            cls({"scoring": "official"})
