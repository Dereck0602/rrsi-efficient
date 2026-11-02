"""Dependency selection and resource preparation for the one-command launcher."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from flashrsi import algorithms
from flashrsi._hf import selected as _hf_prepared
from flashrsi._records import _redact, _secrets
from flashrsi._startup import _CONTAINERS, _HARBOR, _check_docker, _role
from flashrsi.config import load_config

# Every built-in benchmark has an entry, including those needing only base deps.
# The table remains the source of benchmark-specific validation and preparation
# metadata; its values are component labels, not pip extras. Package
# installation uses the public ``all`` extra below.
_BENCHMARK_EXTRAS = {
    "alfworld": ("alfworld",),
    "tau2": ("tau2",),
    "aime": (),
    "bbeh": (),
    "supergpqa": (),
    "twowiki_multihopqa": (),
    "searchqa": (),
    "hotpotqa": (),
    "musique": (),
    "triviaqa": (),
    "nq": (),
    "math": ("math",),
    "hle": ("hle",),
    "officeqa_pro": (),
    "livemathbench": ("livemathbench",),
    "travelplanner": ("travelplanner",),
    "harvey_lab": ("harvey-lab",),
    "browsecomp": ("browsecomp",),
    "deepsearchqa": ("deepsearchqa",),
    "widesearch": ("widesearch",),
    "workbench": ("workbench",),
    "swe_bench": ("swe-bench",),
    "swe_bench_pro": ("harbor",),
    "terminal_bench": ("harbor",),
    "deepswe": ("harbor",),
    "jobbench": ("harbor",),
    "spreadsheetbench2": ("harbor",),
    "appworld": ("appworld",),
    "bfcl": ("bfcl",),
    "tau3_banking": ("tau2",),
}
_AGENT_EXTRAS = {
    "miniagent": "miniagent",
    "claude_code": "claude-code",
    "codex": "codex",
    "pi": "pi",
    "hermes": "hermes",
    "openclaw": "openclaw",
    "opencode": "opencode",
    "llm": "llm",
}
_EDITING_ALGORITHMS = {
    "ahe",
    "adas",
    "darwinx",
    "evo_harness",
    "meta_harness",
    "modular_rsi",
    "rrsi",
}
_TAU_REQUIREMENT = "tau2 @ git+https://github.com/sierra-research/tau2-bench.git@fc0055dc4e0a316c3f83133267fbd6faaa770992"


def _load(path, snapshot=None):
    overrides = {}
    if snapshot is not None:
        overrides["run"] = {
            "mode": "evaluate",
            "snapshot": str(Path(snapshot).expanduser().resolve()),
        }
    for role, keys in (
        ("AGENT", ("agent", "llm")),
        ("WORKER", ("algorithm", "worker", "llm")),
        ("JUDGE", ("benchmark", "judge", "llm")),
        ("USER_SIMULATOR", ("benchmark", "user_simulator", "llm")),
        ("POSTPROCESS", ("benchmark", "postprocess", "llm")),
        ("JUDGE_ANTHROPIC", ("benchmark", "judge", "anthropic")),
        ("JUDGE_OPENAI", ("benchmark", "judge", "openai")),
    ):
        values = {
            key: os.environ[f"FLASHRSI_{role}_{suffix}"]
            for suffix, key in (
                ("MODEL", "model_name"),
                ("API_KEY", "api_key"),
                ("BASE_URL", "base_url"),
            )
            if os.environ.get(f"FLASHRSI_{role}_{suffix}")
        }
        if values:
            section = overrides
            for key in keys:
                section = section.setdefault(key, {})
            section.update(values)
    try:
        config = load_config(path, overrides=overrides)
    except ValueError as exc:
        if isinstance(exc.__cause__, yaml.YAMLError):
            # YAML diagnostics can quote the line containing a credential.
            mark = getattr(exc.__cause__, "problem_mark", None)
            location = (
                f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            )
            raise ValueError("Invalid configuration YAML" + location) from None
        raise

    base_dir = Path(path).expanduser().absolute().parent

    def absolute(value):
        if isinstance(value, Mapping):
            return {k: absolute(v) for k, v in value.items()}
        if isinstance(value, list):
            return [absolute(v) for v in value]
        if isinstance(value, (str, Path)) and str(value).strip():
            p = Path(value).expanduser()
            return str((base_dir / p).absolute())
        return value

    for section, keys in (
        (
            config.benchmark,
            ("data_path", "corpus_path", "gold_path", "evolution_data_path"),
        ),
        (config.agent, ("source_dir", "cli_path")),
        (config.algorithm.get("worker", {}), ("source_dir", "cli_path")),
    ):
        for key in keys:
            if section.get(key) is not None:
                section[key] = absolute(section[key])
    return config


def _plan(config):
    benchmark = config.benchmark.name
    dataset = config.benchmark.get("dataset", {})
    prepared = _hf_prepared(config.benchmark)
    target = config.agent.name
    evaluating = config.run.get("mode") == "evaluate"
    worker = (
        None
        if evaluating
        else (
            "llm"
            if config.algorithm.worker.type == "llm"
            else config.algorithm.worker.name
        )
    )
    if benchmark not in _BENCHMARK_EXTRAS:
        raise ValueError(
            "Unsupported benchmark; choose: " + ", ".join(_BENCHMARK_EXTRAS)
        )
    if target not in {"miniagent", "claude_code", "codex", "pi", "hermes", "openclaw", "opencode"} or (
        not evaluating
        and (
            worker not in _AGENT_EXTRAS
            or (config.algorithm.worker.type == "agent" and worker == "llm")
        )
    ):
        raise ValueError(
            "Agent/Agent Worker must be miniagent, claude_code, codex, pi, hermes, openclaw or opencode; a text Worker uses type: llm"
        )
    if not evaluating:
        algorithm_names = {
            getattr(algorithms, name).name for name in algorithms.__all__
        }
        if config.algorithm.name not in algorithm_names:
            raise ValueError(
                "Unsupported algorithm; choose: " + ", ".join(sorted(algorithm_names))
            )
        if (
            config.algorithm.name in _EDITING_ALGORITHMS
            and config.algorithm.worker.type != "agent"
        ):
            raise ValueError(
                f"{config.algorithm.name} requires algorithm.worker.type: agent and worker.name"
            )
    # Keep the public installation surface small: all non-Harbor integrations
    # are installed together, while Harbor stays opt-in because its LiteLLM
    # dependency conflicts with the external tau2-bench runtime.
    extras = ["all"]
    if benchmark in _HARBOR:
        extras.append("harbor")
    requirements = []
    prerequisites = []
    if "codex" in {target, worker}:
        prerequisites.append(
            "Codex requires explicit API keys and a streaming Responses API endpoint"
        )
    if benchmark in {
        "bfcl",
        "swe_bench",
        "swe_bench_pro",
        "terminal_bench",
        "deepswe",
        "spreadsheetbench2",
        "jobbench",
        "tau2",
        "tau3_banking",
        "harvey_lab",
    }:
        prerequisites.append("Git for upstream installation/data preparation")
    if benchmark in _CONTAINERS:
        prerequisites.append("Docker CLI and a running Linux daemon")
    if benchmark in _HARBOR:
        prerequisites.extend(["Python >=3.12", "Docker Compose"])
    if benchmark in {"tau2", "tau3_banking"}:
        requirements.append(_TAU_REQUIREMENT)
        prerequisites.append("Python >=3.12,<3.14")
        prerequisites.append("Separate benchmark.user_simulator.llm configuration")
        if (
            benchmark == "tau2"
            and config.benchmark.get("domain", "telecom") == "retail"
        ):
            prerequisites.append(
                "Independent benchmark.judge.llm for native NL assertions"
            )
    if benchmark == "harvey_lab":
        requirements.append("pypandoc_binary")
        prerequisites.extend(
            [
                "Python >=3.12,<3.14",
                "independent benchmark.judge.anthropic/openai credentials",
            ]
        )
    if benchmark == "officeqa_pro":
        prerequisites.append(
            "Authorized data access and benchmark.corpus_path containing public documents"
        )
    if benchmark in {"hle", "livemathbench"}:
        prerequisites.append(
            "Approved Hugging Face access (HF_TOKEN), or local benchmark.data_path"
        )
    if benchmark in {"hle", "livemathbench", "jobbench"}:
        prerequisites.append("Independent benchmark.judge.llm configuration")
    if benchmark in {"browsecomp", "deepsearchqa", "widesearch"}:
        prerequisites.append(
            "Configured search/web services and independent benchmark.judge credentials"
        )
    if benchmark == "hotpotqa" and config.benchmark.get("mode") == "fullwiki":
        prerequisites.append("Deployed retrieval service: benchmark.retrieval")
    if benchmark == "alfworld":
        prerequisites.append("Linux/WSL/macOS, Python 3.11 or 3.12")
        if not prepared:
            prerequisites.append(
                "Local native ALFWorld benchmark.data_path (no automatic download)"
            )
    if benchmark == "workbench":
        prerequisites.extend(["Python >=3.12", "Git for pinned native tools and data"])
    if benchmark == "travelplanner":
        prerequisites.append("Independent benchmark.postprocess.llm configuration")
    return {
        "algorithm": config.algorithm.name,
        "agent": target,
        "worker": worker,
        "benchmark": benchmark,
        "extras": extras,
        "requirements": requirements,
        "data": f"download/reuse HF prepared {dataset['config']} at {dataset.get('revision')}"
        if prepared
        else "use explicit local data"
        if config.benchmark.get("data_path")
        else (
            "provide local ALFWorld benchmark.data_path"
            if benchmark == "alfworld"
            else "download/reuse native upstream data"
        ),
        "prerequisites": prerequisites,
    }


def _validate_roles(config):
    _role(config.agent, "agent", config.agent.name)
    if config.run.get("mode") != "evaluate":
        worker = config.algorithm.worker
        _role(
            worker, "algorithm.worker", "llm" if worker.type == "llm" else worker.name
        )


def _validate_host(config):
    name = config.benchmark.name
    if name in _HARBOR and sys.version_info < (3, 12):
        raise ValueError(
            "Harbor benchmarks require Python >=3.12; rerun with that Python"
        )
    if name == "alfworld" and (
        os.name != "posix" or not (3, 11) <= sys.version_info < (3, 13)
    ):
        raise ValueError(
            "ALFWorld text requires Linux/WSL/macOS with Python 3.11 or 3.12"
        )
    if name == "workbench" and sys.version_info < (3, 12):
        raise ValueError("Pinned WorkBench requires Python >=3.12")
    if name == "harvey_lab" and not (3, 12) <= sys.version_info < (3, 14):
        raise ValueError("Harvey LAB requires Python >=3.12,<3.14")
    if name in {"tau2", "tau3_banking"} and not (3, 12) <= sys.version_info < (3, 14):
        raise ValueError("Pinned tau2-bench requires Python >=3.12,<3.14")
    needs_git = name in {
        "bfcl",
        "workbench",
        "swe_bench_pro",
        "terminal_bench",
        "deepswe",
        "spreadsheetbench2",
        "jobbench",
        "harvey_lab",
        "tau2",
        "tau3_banking",
    } and not config.benchmark.get("data_path")
    if name == "jobbench" and isinstance(config.benchmark.get("data_path"), str):
        needs_git = any(
            (Path(config.benchmark.data_path) / part).is_dir()
            for part in ("dataset", "dataset_easy")
        )
    if (
        needs_git
        or name
        in {"swe_bench", "tau2", "tau3_banking", "workbench", "spreadsheetbench2"}
    ) and shutil.which("git") is None:
        raise ValueError("Install Git and put it on PATH for the selected benchmark")
    if name in _CONTAINERS and shutil.which("docker") is None:
        raise ValueError(
            "Install Docker and start a Linux daemon for the selected benchmark"
        )
    if (
        name in {"tau2", "tau3_banking"}
        and config.benchmark.get("task_timeout") is not None
        and os.name != "posix"
    ):
        raise ValueError(
            "tau hard task_timeout requires Linux/WSL or another POSIX host"
        )
    if name in {"tau2", "tau3_banking"} and os.name == "nt" and not sys.flags.utf8_mode:
        raise ValueError("tau on Windows requires PYTHONUTF8=1 before starting Python")


async def _resources(config, cache):
    from filelock import FileLock

    from flashrsi.integrations._docker import _docker

    name = config.benchmark.name
    prepared = _hf_prepared(config.benchmark)
    if name in _CONTAINERS:
        await _check_docker(name, config.benchmark, images=False)
    if name in {"appworld", "bfcl"}:
        from importlib import import_module

        integration = import_module(f"flashrsi.integrations.{name}")
        image = config.benchmark.get("image", integration._IMAGE)
        result = await _docker("image", "inspect", image, timeout=15, check=False)
        if result.returncode:
            if image == integration._IMAGE:
                dockerfile = Path(integration.__file__).with_name(f"{name}.Dockerfile")
                print(f"Building {name} runtime image...", flush=True)
                await _docker(
                    "build",
                    "-f",
                    str(dockerfile),
                    "-t",
                    image,
                    str(dockerfile.parent),
                    timeout=3600,
                )
            else:
                print(f"Pulling configured {name} image...", flush=True)
                await _docker("pull", image, timeout=3600)
        if (
            name == "appworld"
            and not prepared
            and not config.benchmark.get("data_path")
        ):
            parent = cache / "data"
            parent.mkdir(parents=True, exist_ok=True)
            destination = parent / "appworld-0.1.3.post1"
            with FileLock(str(destination) + ".lock"):
                if not destination.exists():
                    with TemporaryDirectory(
                        prefix=".appworld-", dir=parent
                    ) as temporary:
                        root = Path(temporary)
                        print("Downloading AppWorld native data...", flush=True)
                        await _docker(
                            "run",
                            "--rm",
                            "--workdir",
                            "/world",
                            "--mount",
                            f"type=bind,source={root},target=/world",
                            image,
                            "appworld",
                            "download",
                            "data",
                            timeout=3600,
                        )
                        data = root / "data"
                        if not all(
                            (data / part).is_dir()
                            for part in ("datasets", "tasks", "base_dbs")
                        ):
                            raise ValueError(
                                "AppWorld downloader did not produce a complete native data directory"
                            )
                        data.rename(destination)
            config.benchmark.data_path = str(destination)
    if (
        name in {"tau2", "tau3_banking"}
        and not prepared
        and not os.environ.get("TAU2_DATA_DIR")
    ):
        from flashrsi.benchmarks._git import _download_git
        from flashrsi.benchmarks._tau import _GIT_URL, _UPSTREAM_REVISION

        root = _download_git(_GIT_URL, _UPSTREAM_REVISION, ["data/tau2"])
        os.environ["TAU2_DATA_DIR"] = str(root / "data")
    if name == "harvey_lab":
        import pypandoc

        directory = str(Path(pypandoc.get_pandoc_path()).parent)
        os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")


def _run(path, check_only, cache, snapshot=None):
    config = None
    try:
        config = _load(path, snapshot)
        _plan(config)
        _validate_roles(config)
        _validate_host(config)
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

        async def execute():
            from flashrsi.runner import _evaluate, _prepare, _prepare_evaluation
            from flashrsi.runner import _run as run_experiment

            print(
                f"[2/3] Preparing {config.benchmark.name} resources and native data...",
                flush=True,
            )
            await _resources(config, Path(cache))
            if check_only:
                if config.run.get("mode") == "evaluate":
                    _, benchmark = await _prepare_evaluation(config)
                else:
                    benchmark = (await _prepare(config)).benchmark
                counts = ", ".join(
                    f"{role}={len(getattr(benchmark, role))}"
                    for role in ("train", "dev", "test")
                )
                print(
                    f"[3/3] Ready: {counts}. No model calls or experiment directory created."
                )
                print(
                    "Remote API authentication and model availability were not tested."
                )
            else:
                print(
                    f"[3/3] Loading data, running {config.algorithm.name}, and saving results under {config.run.output_dir}",
                    flush=True,
                )
                run_dir, result = await (
                    _evaluate(config)
                    if config.run.get("mode") == "evaluate"
                    else run_experiment(config)
                )
                print(f"Completed experiment: {run_dir}")
                print(f"Test {result['primary_metric']}: {result['test_score']}")

        asyncio.run(execute())
        return 0
    except Exception as exc:
        secrets = _secrets(os.environ) + (
            _secrets(config) if config is not None else []
        )
        print("Experiment failed: " + _redact(str(exc), secrets), file=sys.stderr)
        return 1
