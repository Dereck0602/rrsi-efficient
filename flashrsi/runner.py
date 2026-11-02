"""Instantiate components, run evolution, and save the final held-out result."""

from __future__ import annotations

import copy
import importlib.util
import logging
from pathlib import Path
from statistics import mean
from types import MappingProxyType

from flashrsi._records import _trajectory_record, _write_record
from flashrsi._run_summary import _finish_summary
from flashrsi.agents import (
    LLM,
    ClaudeCode,
    Codex,
    Hermes,
    MiniAgent,
    OpenClaw,
    OpenCode,
    Pi,
)
from flashrsi.algorithms import (
    ACE,
    ADAS,
    AHE,
    GEPA,
    OPRO,
    RRSI,
    Arbor,
    DarwinX,
    EvoHarness,
    EvoPrompt,
    MetaHarness,
    ModularRSI,
    SkillOpt,
)
from flashrsi.benchmarks import (
    AIME,
    BBEH,
    BFCL,
    HLE,
    MATH,
    NQ,
    ALFWorld,
    AppWorldBenchmark,
    BrowseComp,
    DeepSearchQA,
    DeepSWE,
    HarveyLAB,
    HotpotQA,
    JobBench,
    LiveMathBench,
    MuSiQue,
    OfficeQAPro,
    SearchQA,
    SpreadsheetBench2,
    SuperGPQA,
    SWEBench,
    SWEBenchPro,
    Tau2Benchmark,
    Tau3Banking,
    TerminalBench,
    TravelPlanner,
    TriviaQA,
    TwoWikiMultiHopQA,
    WideSearch,
    WorkBench,
)
from flashrsi.config import prepare_run_dir
from flashrsi.core import Agent, Snapshot

_AGENTS = {
    "miniagent": MiniAgent,
    "claude_code": ClaudeCode,
    "codex": Codex,
    "pi": Pi,
    "hermes": Hermes,
    "openclaw": OpenClaw,
    "opencode": OpenCode,
}
_BENCHMARKS = {
    "alfworld": ALFWorld,
    "tau2": Tau2Benchmark,
    "aime": AIME,
    "bbeh": BBEH,
    "supergpqa": SuperGPQA,
    "twowiki_multihopqa": TwoWikiMultiHopQA,
    "travelplanner": TravelPlanner,
    "harvey_lab": HarveyLAB,
    "bfcl": BFCL,
    "browsecomp": BrowseComp,
    "deepsearchqa": DeepSearchQA,
    "deepswe": DeepSWE,
    "widesearch": WideSearch,
    "workbench": WorkBench,
    "appworld": AppWorldBenchmark,
    "searchqa": SearchQA,
    "hotpotqa": HotpotQA,
    "musique": MuSiQue,
    "triviaqa": TriviaQA,
    "nq": NQ,
    "math": MATH,
    "livemathbench": LiveMathBench,
    "hle": HLE,
    "officeqa_pro": OfficeQAPro,
    "swe_bench": SWEBench,
    "swe_bench_pro": SWEBenchPro,
    "terminal_bench": TerminalBench,
    "jobbench": JobBench,
    "spreadsheetbench2": SpreadsheetBench2,
    "tau3_banking": Tau3Banking,
}
_ALGORITHMS = {
    "arbor": Arbor,
    "adas": ADAS,
    "darwinx": DarwinX,
    "evo_harness": EvoHarness,
    "meta_harness": MetaHarness,
    "modular_rsi": ModularRSI,
    "rrsi": RRSI,
    "ace": ACE,
    "ahe": AHE,
    "skillopt": SkillOpt,
    "opro": OPRO,
    "gepa": GEPA,
    "evoprompt": EvoPrompt,
}
_LOGGER = logging.getLogger(__name__)


def _make_agent(cls, section, label):
    from flashrsi._startup import _role

    dependencies = {
        MiniAgent: (("openai", "requests"), "all"),
        ClaudeCode: (("claude_agent_sdk",), "all"),
        Codex: (("httpx", "PIL", "tree_sitter", "tree_sitter_bash"), "all"),
        Hermes: (("httpx",), "all"),
        OpenClaw: (("httpx",), "all"),
        OpenCode: (("httpx",), "all"),
        LLM: (("openai",), "all"),
    }
    if cls is Pi:
        from flashrsi.agents.pi_agent import _executable

        _executable(section)
    for module in dependencies.get(cls, ((), ""))[0]:
        if importlib.util.find_spec(module) is None:
            raise ValueError(
                f"Missing {module}; install flashrsi-lite[{dependencies[cls][1]}]"
            )
    if cls in (MiniAgent, ClaudeCode, Codex, Hermes, OpenClaw, OpenCode, LLM, Pi):
        _role(section, label, cls.name)
    return cls(section)


async def _prepare_target(config, *, snapshot=None):
    """Share benchmark bindings between search and frozen-Snapshot evaluation."""
    from flashrsi._startup import _preflight

    await _preflight(config)
    target = _make_agent(
        _lookup(_AGENTS, config.agent.name, "agent"), config.agent, "agent"
    )
    if snapshot is not None:
        try:
            target.update(snapshot)
        except ValueError as exc:
            raise ValueError(
                f"Cannot evaluate this Snapshot: {exc}. Use the original Agent type and "
                "root source tree (agent.source_dir); evaluation does not rebase snapshots."
            ) from exc
    from flashrsi.benchmarks import _prepared

    prepared = _prepared.selected(config.benchmark)
    if prepared:
        config = copy.deepcopy(config)
        config.benchmark = _prepared.prepare(config.benchmark)
    # Bind the task environment to the selected harness, including any evolved
    # tool/config changes. A benchmark must not silently install another version.
    selected = target.snapshot.id
    benchmark = _lookup(_BENCHMARKS, config.benchmark.name, "benchmark")(
        config.benchmark
    )
    if isinstance(benchmark, ALFWorld):
        from flashrsi.integrations.alfworld import bind

        bind(target, benchmark)
    if isinstance(benchmark, WorkBench):
        from flashrsi.integrations.workbench import bind

        bind(target, benchmark)
    if isinstance(benchmark, TravelPlanner):
        from flashrsi.integrations.travelplanner import bind

        bind(target, benchmark)
    if isinstance(benchmark, HarveyLAB):
        from flashrsi.integrations.harvey_lab import bind

        bind(target, benchmark)
    if isinstance(benchmark, WideSearch):
        from flashrsi.integrations.widesearch import bind

        bind(target, benchmark)
    if isinstance(benchmark, DeepSearchQA):
        from flashrsi.integrations.deepsearchqa import bind

        bind(target, benchmark)
    if isinstance(benchmark, BrowseComp):
        from flashrsi.integrations.browsecomp import bind

        bind(target, benchmark)
    if isinstance(benchmark, HotpotQA) and benchmark.mode == "fullwiki":
        from flashrsi.integrations.hotpotqa import bind

        bind(target, benchmark)
    if isinstance(benchmark, MATH):
        benchmark._check()
    if isinstance(benchmark, LiveMathBench):
        from flashrsi.integrations.livemathbench import bind

        bind(target, benchmark)
    if isinstance(benchmark, HLE):
        from flashrsi.integrations.hle import bind

        bind(target, benchmark)
    if isinstance(benchmark, OfficeQAPro):
        from flashrsi.integrations.officeqa_pro import bind

        bind(target, benchmark)
    if isinstance(benchmark, SWEBench):
        from flashrsi.integrations.swe_bench import bind

        bind(target, benchmark)
    if isinstance(benchmark, BFCL):
        from flashrsi.integrations.bfcl import bind

        bind(target, config.benchmark)
    if isinstance(benchmark, AppWorldBenchmark):
        from flashrsi.integrations.appworld import bind

        bind(target, config.benchmark)
    if isinstance(benchmark, (TerminalBench, SpreadsheetBench2)):
        from flashrsi.integrations.harbor import bind

        bind(target)
    if isinstance(benchmark, DeepSWE):
        from flashrsi.integrations.deepswe import bind

        bind(target, benchmark)
    if isinstance(benchmark, JobBench):
        from flashrsi.integrations.jobbench import bind

        bind(target, benchmark)
    if isinstance(benchmark, SWEBenchPro):
        from flashrsi.integrations.swe_bench_pro import bind

        bind(target, benchmark)
    if isinstance(benchmark, (Tau2Benchmark, Tau3Banking)):
        from flashrsi.integrations.tau2 import bind

        bind(
            target,
            config.benchmark.get("user_simulator"),
            task_timeout=config.benchmark.get("task_timeout"),
            judge=config.benchmark.get("judge"),
        )
    if target.snapshot.id != selected:
        raise ValueError(
            f"{benchmark.name} integration replaced the selected harness Snapshot; "
            "task environments and tools must be provided outside Snapshot files"
        )
    if prepared:
        benchmark.load()
        benchmark.metadata = MappingProxyType(
            {**benchmark.metadata, "prepared": config.benchmark["_prepared_source"]}
        )
    return target, benchmark


async def _prepare(config):
    """Initialize search, including its independent Worker and algorithm checks."""
    algorithm_type = _lookup(_ALGORITHMS, config.algorithm.name, "algorithm")
    worker_config = config.algorithm.worker
    worker_type = (
        LLM
        if worker_config.type == "llm"
        else _lookup(_AGENTS, worker_config.name, "worker")
    )
    target, benchmark = await _prepare_target(config)
    worker = _make_agent(worker_type, worker_config, "algorithm.worker")
    algorithm = algorithm_type(
        config.algorithm, target=target, worker=worker, benchmark=benchmark
    )
    return algorithm


async def _run(config):
    algorithm = await _prepare(config)
    worker, benchmark = algorithm.worker, algorithm.benchmark
    resolved = copy.deepcopy(config)
    resolved.algorithm = algorithm.config
    run_dir = prepare_run_dir(resolved)
    _artifact_paths(algorithm.target, benchmark, run_dir)
    algorithm._run_dir = run_dir
    _LOGGER.info("Experiment: %s", run_dir)
    try:
        if worker.component_rules:
            worker.snapshot.save(run_dir / "snapshots" / "worker.json")
        final = await algorithm.evolve()
        results = await algorithm._evaluate(final, benchmark.test, label="test")
        summary = {
            "status": "finished",
            "snapshot_id": final.id,
            "primary_metric": benchmark.primary_metric,
            "test_score": mean(
                item.metrics[benchmark.primary_metric] for item in results
            )
            if results
            else None,
            "test_tasks": len(results),
            "test_metrics": {
                name: mean(item.metrics[name] for item in results)
                for name in (results[0].metrics if results else ())
                if all(name in item.metrics for item in results)
            },
            "benchmark": dict(benchmark.metadata),
        }
        _write_record(run_dir / "result.json", summary, config)
    except BaseException as exc:
        _write_record(
            run_dir / "error.json",
            {"status": "error", "error": str(exc) or type(exc).__name__},
            config,
        )
        raise
    finally:
        _finish_summary(run_dir)
    return run_dir, summary


def _lookup(choices, name, role):
    try:
        return choices[name]
    except KeyError:
        raise ValueError(
            f"Unsupported {role}: {name}; available: {', '.join(choices)}"
        ) from None


def _artifact_paths(target, benchmark, run_dir):
    if isinstance(benchmark, HarveyLAB) and benchmark._artifacts_dir is None:
        benchmark._artifacts_dir = run_dir / "artifacts" / "harvey_lab"
    if isinstance(
        benchmark, (TerminalBench, SpreadsheetBench2, DeepSWE, JobBench, SWEBenchPro)
    ):
        target.config.setdefault("harbor", {}).setdefault(
            "artifacts_dir", str(run_dir / "artifacts" / "harbor")
        )


async def _prepare_evaluation(config):
    snapshot = Snapshot.load(Path(config.run.snapshot))
    target, benchmark = await _prepare_target(config, snapshot=snapshot)
    target.set_scope({name: "r" for name in target.component_rules})
    benchmark.load()
    if not benchmark.test:
        raise ValueError(
            "Evaluation requires a non-empty benchmark.test; configure explicit test roles"
        )
    if (
        type(target).rollout is Agent.rollout
        and target._env_runner is None
        and any(t.env is not None for t in benchmark.test)
    ):
        raise ValueError(
            "Evaluation target needs environment-aware rollout for these tasks"
        )
    return target, benchmark


async def _evaluate(config):
    """Evaluate a saved resource version without constructing a Worker or Algorithm."""
    target, benchmark = await _prepare_evaluation(config)
    snapshot = target.snapshot
    run_dir = prepare_run_dir(config)
    _artifact_paths(target, benchmark, run_dir)
    source = {
        "snapshot_path": str(Path(config.run.snapshot).resolve()),
        "snapshot_id": snapshot.id,
        "root_id": snapshot.root_id,
        "parent_id": snapshot.parent_id,
        "agent_type": snapshot.agent_type,
    }
    try:
        snapshot.save(run_dir / "snapshots" / f"{snapshot.id}.json")
        _write_record(
            run_dir / "evaluation.json",
            {"source": source, "benchmark": dict(benchmark.metadata), "split": "test"},
            config,
        )
        trajectories = await target.rollout(benchmark.test)
        # Preserve completed requests even when grading fails.
        _write_record(
            run_dir / "trajectories" / "000001-rollout.json",
            {
                "snapshot_id": snapshot.id,
                "trajectories": [_trajectory_record(t) for t in trajectories],
            },
            config,
        )
        results = benchmark.grade(trajectories)
        if tuple(t.task for t in results) != tuple(benchmark.test):
            raise ValueError(
                "rollout/grade must preserve the requested tasks and order"
            )
        record = {
            "snapshot_id": snapshot.id,
            "trajectories": [_trajectory_record(t) for t in results],
        }
        _write_record(run_dir / "trajectories" / "000002-test.json", record, config)
        # The scored record supersedes its raw copy for usage accounting.
        (run_dir / "trajectories" / "000001-rollout.json").unlink()
        summary = {
            "status": "finished",
            "mode": "evaluate",
            "source": source,
            "snapshot_id": snapshot.id,
            "primary_metric": benchmark.primary_metric,
            "test_score": mean(t.metrics[benchmark.primary_metric] for t in results),
            "test_tasks": len(results),
            "test_metrics": {
                name: mean(t.metrics[name] for t in results)
                for name in results[0].metrics
                if all(name in t.metrics for t in results)
            },
            "benchmark": dict(benchmark.metadata),
        }
        _write_record(run_dir / "result.json", summary, config)
    except BaseException as exc:
        _write_record(
            run_dir / "error.json",
            {
                "status": "error",
                "mode": "evaluate",
                "source": source,
                "error": str(exc) or type(exc).__name__,
            },
            config,
        )
        raise
    finally:
        _finish_summary(run_dir)
    return run_dir, summary
