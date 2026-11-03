"""All registered benchmarks bind their own execution outside harness versions.

These tests replace external integrations with probes. They check selection,
binding and task/config isolation, not service availability or official scores.
"""

import asyncio
import copy
import sys
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from flashrsi import _startup, runner
from flashrsi.agents import MiniAgent
from flashrsi.config import load_config
from flashrsi.core import Task, Trajectory

# Explicitly include text-only benchmarks: supplying public question/context
# with no task-specific tools is a supported execution contract too.
_INTEGRATIONS = {
    "alfworld": "alfworld",
    "tau2": "tau2",
    "aime": "supplied_context",
    "bbeh": "supplied_context",
    "supergpqa": "supplied_context",
    "twowiki_multihopqa": "supplied_context",
    "travelplanner": "travelplanner",
    "harvey_lab": "harvey_lab",
    "bfcl": "bfcl",
    "browsecomp": "browsecomp",
    "deepsearchqa": "deepsearchqa",
    "deepswe": "deepswe",
    "widesearch": "widesearch",
    "workbench": "workbench",
    "appworld": "appworld",
    "searchqa": "supplied_context",
    "hotpotqa": "supplied_context",
    "musique": "supplied_context",
    "triviaqa": "supplied_context",
    "nq": "supplied_context",
    "math": "supplied_context",
    "livemathbench": "livemathbench",
    "hle": "hle",
    "officeqa_pro": "officeqa_pro",
    "swe_bench": "swe_bench",
    "swe_bench_pro": "swe_bench_pro",
    "terminal_bench": "harbor",
    "jobbench": "jobbench",
    "spreadsheetbench2": "harbor",
    "tau3_banking": "tau2",
}


def test_every_registered_benchmark_has_an_execution_contract():
    assert set(_INTEGRATIONS) == set(runner._BENCHMARKS)


def test_python_codex_startup_does_not_require_native_oracle(tmp_path, monkeypatch):
    (tmp_path / "components.yaml").write_text("prompt: [AGENTS.md]\n")
    (tmp_path / "AGENTS.md").write_text("fixture instructions")
    checked = []

    def available(module):
        checked.append(module)
        return object()

    monkeypatch.setattr(runner.importlib.util, "find_spec", available)
    target = runner._make_agent(
        runner.Codex,
        {
            "source_dir": str(tmp_path),
            "llm": {"model_name": "fixture", "api_key": "fixture-key"},
        },
        "agent",
    )
    assert target.snapshot.agent_type == "codex"
    assert checked == ["httpx", "PIL", "tree_sitter", "tree_sitter_bash"]
    assert "codex_cli_bin" not in checked


@pytest.fixture
def harness(tmp_path):
    target_config = {
        "name": "miniagent",
        "llm": {"model_name": "destination-model", "api_key": "destination-key"},
    }
    seed = MiniAgent(target_config).snapshot
    with seed.check_out() as files:
        with (files / "prompts/system.md").open("a", encoding="utf-8") as stream:
            stream.write("\nEVOLVED_HARNESS_MARKER\n")
        snapshot = seed.commit(files)
    return target_config, snapshot


@pytest.mark.parametrize("name", list(_INTEGRATIONS) + ["hotpotqa-fullwiki"])
def test_benchmark_binds_selected_harness_without_replacing_it(
    harness, tmp_path, monkeypatch, name
):
    agent_config, snapshot = harness
    fullwiki = name == "hotpotqa-fullwiki"
    name = "hotpotqa" if fullwiki else name
    integration = "hotpotqa" if fullwiki else _INTEGRATIONS[name]
    benchmark_config = {"name": name, "mode": "fullwiki" if fullwiki else "context"}
    # Only HotpotQA interprets context/fullwiki; do not override other native
    # benchmark modes (tau and ALFWorld, for example, use mode: text).
    if name != "hotpotqa":
        benchmark_config.pop("mode")
    if name == "appworld":
        benchmark_config["data_path"] = str(tmp_path / "native-appworld")
    config = load_config(
        overrides={
            "run": {"mode": "evaluate", "snapshot": "fixture.json"},
            "agent": agent_config,
            "benchmark": benchmark_config,
        }
    )
    before = copy.deepcopy(dict(config.agent))
    bound = []
    executed = []
    workspace = tmp_path / "destination-task"
    workspace.mkdir()
    (workspace / "input.txt").write_text("destination public data")

    def probe(module_name):
        def bind(target, *args, **kwargs):
            assert module_name == integration
            # This must already be the evolved version when the integration
            # inspects the harness, not the freshly initialized seed.
            assert target.snapshot.id == snapshot.id
            assert target.snapshot.files == snapshot.files
            bound.append(module_name)

            async def execute(agent, task):
                assert agent.snapshot.id == snapshot.id
                assert task.env == f"destination:{name}"
                assert (
                    workspace / "input.txt"
                ).read_text() == "destination public data"
                return await agent.run(
                    task.question + f"\nTask tools for {name}", working_dir=workspace
                )

            target._env_runner = execute

        return bind

    for module_name in (set(_INTEGRATIONS.values()) | {"hotpotqa"}) - {None}:
        module = ModuleType(f"flashrsi.integrations.{module_name}")
        module.bind = probe(module_name)
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(_startup, "_preflight", AsyncMock())
    monkeypatch.setattr(runner.MATH, "_check", lambda self: None)
    target, benchmark = asyncio.run(runner._prepare_target(config, snapshot=snapshot))
    assert isinstance(benchmark, runner._BENCHMARKS[name])
    assert bound == ([integration] if integration else [])
    assert dict(target.config) == before
    assert dict(config.agent) == before
    assert target.snapshot.files == snapshot.files

    async def native_run(prompt, working_dir=None):
        executed.append((prompt, working_dir))
        assert (
            b"EVOLVED_HARNESS_MARKER"
            in target.snapshot.files["prompts/system.md"].content
        )
        assert target.config.llm.model_name == "destination-model"
        assert (
            "EVOLVED_HARNESS_MARKER"
            in (target.files_dir / "prompts/system.md").read_text()
        )
        return Trajectory(output="fixture answer")

    monkeypatch.setattr(target, "_run", native_run)
    task = Task(
        "destination-task",
        f"Public question for {name}",
        env=f"destination:{name}" if integration else None,
    )
    result = asyncio.run(target.rollout((task,)))[0]
    assert result.task == task
    assert result.output == "fixture answer"
    assert executed == [
        (
            task.question + (f"\nTask tools for {name}" if integration else ""),
            workspace if integration else None,
        )
    ]
    assert target.snapshot.id == snapshot.id
    assert target.snapshot.files == snapshot.files
    assert dict(target.config) == before
    assert not any("input.txt" in path for path in target.snapshot.files)


def test_integration_cannot_silently_replace_selected_harness(
    harness, tmp_path, monkeypatch
):
    agent_config, snapshot = harness
    config = load_config(
        overrides={
            "agent": agent_config,
            "benchmark": {"name": "spreadsheetbench2"},
            "run": {"mode": "evaluate", "snapshot": "fixture.json"},
        }
    )
    module = ModuleType("flashrsi.integrations.harbor")

    def bad_bind(target):
        with target.snapshot.check_out() as files:
            (files / "prompts/system.md").write_text("benchmark replaced instructions")
            target.update(target.snapshot.commit(files))

    module.bind = bad_bind
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(_startup, "_preflight", AsyncMock())
    with pytest.raises(ValueError, match="integration replaced the selected harness"):
        asyncio.run(runner._prepare_target(config, snapshot=snapshot))
