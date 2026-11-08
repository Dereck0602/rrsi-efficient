"""A development timeout floor must not shorten long benchmark tasks."""

import asyncio
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")

from flashrsi.core import Task
from flashrsi.integrations import harbor
from flashrsi.integrations.harbor import _agent_timeout


@pytest.mark.parametrize(
    "native,expected", [(600, 3600), (3600, 3600), (7200, 7200), (12000, 12000)]
)
def test_floor_preserves_larger_native_timeouts(native, expected):
    assert _agent_timeout({"harbor": {"agent_min_timeout": 3600}}, native) == expected


def test_existing_native_and_explicit_timeout_behavior():
    assert _agent_timeout({}, 12000) == 12000
    assert _agent_timeout({"timeout": 60}, 12000) == 60
    with pytest.raises(ValueError, match="not both"):
        _agent_timeout({"timeout": 60, "harbor": {"agent_min_timeout": 3600}}, 12000)


@pytest.mark.parametrize("value", [0, -1, True, "3600", float("inf"), float("nan")])
def test_invalid_floor_is_rejected(value):
    with pytest.raises(ValueError, match="agent_min_timeout"):
        _agent_timeout({"harbor": {"agent_min_timeout": value}}, 900)


@pytest.mark.parametrize("native_timeout,effective", [(900, 3600), (12000, 12000)])
def test_native_trial_and_runtime_receive_the_same_floor(
    tmp_path, monkeypatch, native_timeout, effective
):
    (tmp_path / "environment").mkdir()
    (tmp_path / "environment/Dockerfile").write_text("FROM ubuntu:24.04\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test.sh").write_text("#!/bin/sh\ntrue\n")
    (tmp_path / "instruction.md").write_text("Fixture task")
    (tmp_path / "task.toml").write_text(
        f'version = "1.0"\n[agent]\ntimeout_sec = {native_timeout}\n'
    )
    agent = SimpleNamespace(
        config={
            "llm": {"model_name": "fixture"},
            "harbor": {"agent_min_timeout": 3600},
        },
        snapshot=SimpleNamespace(id="fixture-snapshot"),
        _check_container=lambda: None,
    )
    captured = {}

    def stage(directory, current, config, question):
        captured["runtime_timeout"] = config["timeout"]

    async def create(config):
        captured["trial_timeout"] = config.agent.override_timeout_sec
        captured["setup_timeout"] = config.agent.override_setup_timeout_sec
        raise RuntimeError("fixture stops before container creation")

    monkeypatch.setattr(harbor.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(harbor, "_stage", stage)
    monkeypatch.setattr(harbor.Trial, "create", create)
    result = asyncio.run(
        harbor._run(agent, Task("fixture", "Fixture task", env=str(tmp_path)))
    )
    assert result.status == "error"
    assert captured == {
        "runtime_timeout": effective,
        "trial_timeout": effective + 30,
        "setup_timeout": 600,
    }
    assert "timeout" not in agent.config
