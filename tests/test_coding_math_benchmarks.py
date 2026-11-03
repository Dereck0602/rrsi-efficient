"""Pinned datasets, non-executing scoring, reference integrity and launcher paths."""

import asyncio
import json
from dataclasses import replace

import pytest

from flashrsi import _launch
from flashrsi.benchmarks import AIME
from flashrsi.config import load_config
from flashrsi.core import Agent, Trajectory
from flashrsi.runner import _BENCHMARKS


def aime_row(i=0):
    return {"id": i, "problem": f"Solve problem {i}.", "answer": 70}


CASES = [(AIME, aime_row)]


@pytest.mark.parametrize("cls,row", CASES)
def test_partition_grading_and_prompt_isolation(tmp_path, cls, row):
    path = tmp_path / "test.json"
    path.write_text(json.dumps([row(i) for i in range(8)]), encoding="utf-8")
    config = {
        "data_path": str(path),
        "split_counts": {"train": 4, "dev": 2, "test": 2},
        "seed": 42,
    }
    bench = cls(config)
    splits = bench.load()
    assert splits == cls(config).load()
    assert {k: len(v) for k, v in splits.items()} == {"train": 4, "dev": 2, "test": 2}
    assert len({t.task_id for group in splits.values() for t in group}) == 8
    task = bench.test[0]

    class Recorder(Agent):
        name = "prompt-recorder"

        async def _run(self, prompt, working_dir=None):
            assert prompt == task.question
            assert "SECRET_LABEL" not in prompt
            return Trajectory(output=task.ground_truth)

    with pytest.raises(NotImplementedError, match="bind an execution integration"):
        asyncio.run(Recorder().rollout((task,)))
    good = bench.grade((Trajectory(task=task, output=task.ground_truth),))[0]
    assert good.metrics == {"accuracy": 1.0}
    assert task.env == "supplied_context"
    forged = replace(task, ground_truth="999", metadata={"input": "other"})
    bad, failed = bench.grade(
        (
            Trajectory(task=forged, output="999"),
            Trajectory(
                task=task,
                output=task.ground_truth,
                status="error",
                error_message="fixture",
            ),
        )
    )
    assert bad.metrics == failed.metrics == {"accuracy": 0.0}
    with pytest.raises(ValueError, match="unknown"):
        bench.grade((Trajectory(task=replace(task, task_id="unknown")),))
    with pytest.raises(ValueError, match="requires a Task"):
        bench.grade((Trajectory(),))


@pytest.mark.parametrize(
    "cls,config,row,repo,revision,filename",
    [
        (
            AIME,
            {"year": 2025},
            aime_row,
            "math-ai/aime25",
            "563bb8404243c5f09de6ec262f2db674fe5bce9b",
            "test.jsonl",
        ),
        (
            AIME,
            {},
            aime_row,
            "math-ai/aime26",
            "79037aebdb6580008fb960d17cb21fd3099083e3",
            "aime2026.jsonl",
        ),
    ],
)
def test_download_pins_and_no_implicit_training(
    tmp_path, monkeypatch, cls, config, row, repo, revision, filename
):
    path = tmp_path / filename
    path.write_text(json.dumps(row()) + "\n", encoding="utf-8")
    calls = []

    def download(**kwargs):
        calls.append(kwargs)
        return str(path)

    monkeypatch.setattr("huggingface_hub.hf_hub_download", download)
    bench = cls(config)
    splits = bench.load()
    assert splits["train"] == splits["dev"] == ()
    assert len(splits["test"]) == 1
    assert bench.load() is splits
    assert calls == [
        {
            "repo_id": repo,
            "repo_type": "dataset",
            "revision": revision,
            "filename": filename,
        }
    ]


@pytest.mark.parametrize("cls,row", CASES)
def test_explicit_local_roles_and_duplicate_ids(tmp_path, cls, row):
    path = tmp_path / "pool.json"
    path.write_text(json.dumps([row()]), encoding="utf-8")
    with pytest.raises(ValueError, match="No experiment role"):
        cls({"data_path": str(path)}).load()
    bench = cls({"data_path": {"test": str(path)}})
    bench.load()
    assert len(bench.test) == 1
    assert not bench.train and not bench.dev
    with pytest.raises(ValueError, match="duplicate"):
        cls({"data_path": {"train": str(path), "test": str(path)}}).load()


@pytest.mark.parametrize("value", [2024, 2027, "2026", 2026.0, True, None])
def test_aime_rejects_unsupported_year(value):
    with pytest.raises(ValueError, match="year"):
        AIME({"year": value})


@pytest.mark.parametrize("answer", ["070", 70])
def test_aime_year_ids_and_answer_normalization(answer):
    tasks = [
        AIME({"year": y})._task_from_row({**aime_row(), "answer": answer}, "test")
        for y in (2025, 2026)
    ]
    assert tasks[0].task_id != tasks[1].task_id
    assert all(t.ground_truth == "70" for t in tasks)


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", None),
        ("id", True),
        ("problem", ""),
        ("answer", True),
        ("answer", -1),
        ("answer", 1000),
        ("answer", 70.0),
        ("answer", "7+63"),
    ],
)
def test_aime_invalid_schema(field, value):
    with pytest.raises(ValueError):
        AIME({})._task_from_row({**aime_row(), field: value}, "test")


@pytest.mark.parametrize(
    "prediction,score",
    [
        ("70", 1),
        ("Reasoning\n070", 1),
        (r"Answer: \boxed{070}.", 1),
        (r"\boxed{12} then \boxed{70}", 1),
        (r"\boxed{70} then \boxed{12}", 0),
        (r"\boxed{70} then \boxed{", 0),
        ("70\n12", 0),
        ("70.0", 0),
        ("The answer may be 70 or 12", 0),
        (r"\boxed{7+63}", 0),
        ("", 0),
    ],
)
def test_aime_final_integer_protocol(prediction, score):
    bench = AIME({})
    task = bench._task_from_row(aime_row(), "test")
    assert bench._score(prediction, task) == {"accuracy": score}


@pytest.mark.parametrize("cls,row", CASES)
def test_launcher_and_unsupported_modes(cls, row):
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
        cls({"scoring": "official"})
    with pytest.raises(ValueError, match="mode"):
        cls({"mode": "input"})
    with pytest.raises(RuntimeError, match="call load"):
        cls({}).grade(())
