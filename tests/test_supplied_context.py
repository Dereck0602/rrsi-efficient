"""Text-task routing and the host/container payload boundary; no model calls."""

import asyncio
import json
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from flashrsi import _launch, _startup, runner
from flashrsi.agents import MiniAgent
from flashrsi.config import load_config
from flashrsi.core import Trajectory
from flashrsi.integrations import supplied_context

ROWS = {
    "searchqa": {
        "id": "q",
        "question": "Public question?",
        "context": "Public context",
        "answer": "PRIVATE_REFERENCE",
    },
    "hotpotqa": {
        "id": "q",
        "question": "Public question?",
        "answer": "PRIVATE_REFERENCE",
        "context": [["Page", ["Public context"]]],
    },
    "musique": {
        "id": "q",
        "question": "Public question?",
        "answer": "PRIVATE_REFERENCE",
        "answerable": True,
        "answer_aliases": [],
        "paragraphs": [{"title": "Page", "paragraph_text": "Public context"}],
    },
    "triviaqa": {
        "question_id": "q",
        "question": "Public question?",
        "answer": {
            "value": "PRIVATE_REFERENCE",
            "aliases": [],
            "normalized_aliases": ["private_reference"],
        },
        "entity_pages": {"title": ["Page"], "wiki_context": ["Public context"]},
    },
    "nq": {
        "id": "q",
        "question": {"text": "Public question?"},
        "document": {
            "tokens": {"token": ["Public", "context"], "is_html": [False, False]}
        },
        "annotations": {
            "short_answers": [{"start_token": [], "end_token": []}],
            "yes_no_answer": [1],
        },
    },
    "twowiki_multihopqa": {
        "_id": "q",
        "question": "Public question?",
        "answer": "PRIVATE_REFERENCE",
        "context": [["Page", ["Public context"]]],
    },
    "math": {
        "unique_id": "q",
        "problem": "Public problem",
        "solution": "PRIVATE_REFERENCE",
    },
    "aime": {"id": "q", "problem": "Public problem", "answer": "731"},
    "bbeh": {
        "task": "counting",
        "input": "Public problem",
        "target": "PRIVATE_REFERENCE",
    },
    "supergpqa": {
        "uuid": "q",
        "question": "Public question?",
        "options": ["One", "Two"],
        "answer": "One",
        "answer_letter": "A",
        "explanation": "PRIVATE_REFERENCE",
    },
}


@pytest.mark.parametrize("name", ROWS)
@pytest.mark.parametrize(
    "frozen", [False, True], ids=["evolution", "frozen-evaluation"]
)
def test_loaded_tasks_require_isolation_and_stage_only_public_input(
    tmp_path, monkeypatch, name, frozen
):
    dataset = tmp_path / "labeled.json"
    dataset.write_text(json.dumps([ROWS[name]]), encoding="utf-8")
    (tmp_path / "other-experiment.json").write_text("PRIVATE_OTHER_EXPERIMENT")
    config = load_config(
        overrides={
            "algorithm": {
                "worker": {
                    "type": "llm",
                    "llm": {"model_name": "fixture", "api_key": "fixture"},
                }
            },
            "agent": {
                "name": "miniagent",
                "llm": {"model_name": "fixture", "api_key": "fixture"},
            },
            "benchmark": {
                "name": name,
                "data_path": str(dataset),
                "split_counts": {"train": 0, "dev": 0, "test": 1},
            },
        }
    )
    assert (
        "Docker CLI and a running Linux daemon"
        in _launch._plan(config)["prerequisites"]
    )
    with monkeypatch.context() as missing_docker:
        missing_docker.setattr("shutil.which", lambda _: None)
        with pytest.raises(ValueError, match="Docker"):
            asyncio.run(_startup._preflight(config))

    monkeypatch.setattr(_startup, "_preflight", AsyncMock())
    monkeypatch.setattr(runner.MATH, "_check", lambda self: None)
    monkeypatch.setattr(supplied_context.shutil, "which", lambda name: name)
    seed = MiniAgent(config.agent).snapshot
    with seed.check_out() as checkout:
        with (checkout / "prompts/system.md").open("a", encoding="utf-8") as stream:
            stream.write("\nSELECTED_VERSION")
        selected = seed.commit(checkout)
    target, benchmark = asyncio.run(
        runner._prepare_target(config, snapshot=selected if frozen else None)
    )
    if not frozen:
        target.update(selected)
    benchmark.load()
    task = benchmark.test[0]
    assert task.env == "supplied_context"
    assert "PRIVATE_REFERENCE" not in task.question
    # Even extra host-only annotations must not cross the execution boundary.
    task = replace(
        task,
        ground_truth="PRIVATE_REFERENCE",
        metadata={
            **task.metadata,
            "annotation": "PRIVATE_ANNOTATION",
        },
    )
    with pytest.raises(NotImplementedError, match="bind an execution integration"):
        asyncio.run(MiniAgent(config.agent).rollout((task,)))

    staged = []

    async def setup(docker, name, network, payload, config, timeout):
        request = json.loads((payload / "request.json").read_text())
        snapshot = json.loads((payload / "snapshot.json").read_text())
        assert set(request) == {"agent_class", "config_yaml", "snapshot_path", "prompt"}
        assert request["prompt"] == task.question
        assert "PRIVATE_" not in json.dumps([request, snapshot])
        assert snapshot["id"] == selected.id
        assert {p.name for p in payload.iterdir()} == {
            "package",
            "snapshot.json",
            "request.json",
            "requirements.txt",
        }
        staged.append(name)

    monkeypatch.setattr(supplied_context, "_setup", setup)
    monkeypatch.setattr(
        supplied_context,
        "_run",
        AsyncMock(return_value=Trajectory(output="Public result")),
    )
    cleanup = AsyncMock()
    monkeypatch.setattr(supplied_context, "_cleanup", cleanup)
    host_run = AsyncMock(side_effect=AssertionError("Target ran on host"))
    monkeypatch.setattr(target, "_run", host_run)
    result = asyncio.run(target.rollout((task,)))[0]
    assert result.task == task and result.output == "Public result"
    assert target.snapshot.id == selected.id
    assert len(staged) == 1
    host_run.assert_not_called()
    cleanup.assert_awaited_once()
