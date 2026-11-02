"""Runner startup checks for prerequisites not covered by native bind/load calls."""

import importlib.util
import os
import shutil
import sys
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit

from flashrsi.integrations._docker import _docker

_HARBOR = {
    "terminal_bench",
    "jobbench",
    "swe_bench_pro",
    "spreadsheetbench2",
    "deepswe",
}
_CONTAINERS = _HARBOR | {"appworld", "bfcl", "swe_bench"}


async def _preflight(config):
    benchmark = config.benchmark
    name = benchmark.name
    # Role mappings and lists keep their loader semantics. Only existence is
    # checked here; native loaders validate records, revisions and resources.
    for key in ("data_path", "corpus_path", "gold_path"):
        value = benchmark.get(key)
        if value is not None:
            _paths(value, f"benchmark.{key}")

    if name in _HARBOR:
        if sys.version_info < (3, 12):
            raise ValueError("Harbor benchmarks require Python >=3.12; see README.md")
        _module("harbor", "harbor")
    if name in {"tau2", "tau3_banking"}:
        if not (3, 12) <= sys.version_info < (3, 14):
            raise ValueError("Pinned tau2-bench requires Python >=3.12,<3.14")
        try:
            _module("tau2", "tau2")
        except ValueError:
            raise ValueError(
                "Install pinned tau2-bench separately alongside flashrsi-lite[all]; see README.md"
            ) from None
        _module("rank_bm25", "all")
        if version("tau2") != "1.0.1":
            raise ValueError(
                "This integration requires pinned tau2-bench v1.0.1; see README.md"
            )
        if os.name == "nt" and not sys.flags.utf8_mode:
            raise ValueError(
                "tau on Windows requires PYTHONUTF8=1 before starting Python"
            )
        from flashrsi._hf import selected

        # HF resolution validates and binds its own native prompt files later.
        if not selected(benchmark):
            from tau2.user.user_simulator import get_global_user_sim_guidelines

            try:
                for tools in (False, True):
                    get_global_user_sim_guidelines(use_tools=tools)
            except OSError:
                raise ValueError(
                    "Prepare tau2 user_simulator/*.md and set TAU2_DATA_DIR before startup; see README.md"
                ) from None
        if benchmark.get("task_timeout") is not None and os.name != "posix":
            raise ValueError("tau hard task_timeout requires a POSIX host")
    if name in _CONTAINERS:
        await _check_docker(name, benchmark)

    for label, section in (
        ("benchmark.judge", benchmark.get("judge", {})),
        ("benchmark.user_simulator", benchmark.get("user_simulator", {})),
    ):
        if not isinstance(section, Mapping) or not isinstance(
            section.get("llm", {}), Mapping
        ):
            raise ValueError(f"{label}.llm must be a mapping")
        url = section.get("llm", {}).get("base_url")
        if url:
            _url(url, label + ".llm.base_url")


def _role(config, label, kind):
    llm = config["llm"]
    if kind == "claude_code":
        from flashrsi.agents.claude_code import _authentication_env

        _authentication_env(llm)
        if (
            config.get("cli_path")
            and not Path(config["cli_path"]).expanduser().is_file()
        ):
            raise ValueError(
                f"{label}.cli_path must identify an installed Claude Code CLI"
            )
    elif kind in {"miniagent", "llm", "codex"} and not llm.get("api_key"):
        raise ValueError(
            f"{label}.llm.api_key is required; credentials are independent for each role"
        )
    elif kind == "pi":
        if not llm.get("api_key"):
            raise ValueError(
                f"{label}.llm.api_key is required; credentials are independent for each role"
            )
        from flashrsi.agents.pi_agent import _executable, _model

        _executable(config)
        _model(llm)
        if config.get("cli_path") or config.get("node_path"):
            raise ValueError(
                f"{label}: Pi is implemented in the Snapshot's Python runtime; cli_path/node_path are unsupported"
            )
    elif kind == "hermes":
        from flashrsi.agents.hermes import _validate

        _validate(config)
    elif kind == "openclaw":
        from flashrsi.agents.openclaw import _validate

        _validate(config)
    elif kind == "opencode":
        from flashrsi.agents.opencode import _validate

        _validate(config)
    if kind == "codex":
        if config.get("cli_path"):
            raise ValueError(
                f"{label}: Codex is implemented entirely in Python; cli_path is unsupported"
            )
        if llm.get("auth", "api_key") != "api_key":
            raise ValueError(
                f"{label}: Codex requires explicit llm.api_key; native login is unsupported"
            )
    if llm.get("base_url"):
        _url(llm["base_url"], label + ".llm.base_url")


def _url(value, label):
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = urlsplit(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and (parsed.port is None or parsed.port > 0)
        )
    except ValueError:
        valid = False
    if not valid:
        # Never echo a connection string, which may contain credentials.
        raise ValueError(f"{label} must be an absolute HTTP(S) URL")


def _module(name, extra):
    try:
        present = importlib.util.find_spec(name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        present = False
    if not present:
        raise ValueError(f"Missing {name}; install flashrsi-lite[{extra}]")


def _paths(value, label):
    if isinstance(value, Mapping):
        for key, child in value.items():
            _paths(child, f"{label}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _paths(child, f"{label}[{index}]")
    elif not isinstance(value, (str, Path)) or not str(value).strip():
        raise ValueError(f"{label} must be a nonempty local path")
    elif not Path(value).expanduser().exists():
        raise ValueError(
            f"{label} does not exist; prepare native data first (README.md)"
        )


async def _check_docker(name, benchmark, *, images=True):
    if shutil.which("docker") is None:
        raise ValueError("Install Docker CLI and start a Linux daemon; see README.md")
    try:
        result = await _docker(
            "info", "--format", "{{.OSType}}", timeout=15, check=False
        )
    except (OSError, TimeoutError):
        raise ValueError(
            "Cannot reach Docker daemon; check Docker context/proxy (README.md)"
        ) from None
    if result.returncode or result.stdout.strip() != "linux":
        raise ValueError(
            "Docker requires a reachable Linux daemon; check Docker context/proxy (README.md)"
        )
    if name in _HARBOR:
        try:
            result = await _docker("compose", "version", timeout=10, check=False)
        except (OSError, TimeoutError):
            raise ValueError(
                "Docker Compose plugin unavailable; see README.md"
            ) from None
        if result.returncode:
            raise ValueError("Install Docker Compose plugin; see README.md")
    # These two integrations use prebuilt runtime images, not automatic builds.
    # Check only metadata: never pull/build or create a container during startup.
    if images and name in {"appworld", "bfcl"}:
        from importlib import import_module

        integration = import_module(f"flashrsi.integrations.{name}")
        image = benchmark.get("image", integration._IMAGE)
        try:
            result = await _docker("image", "inspect", image, timeout=10, check=False)
        except (OSError, TimeoutError):
            raise ValueError(
                f"Cannot inspect {name} runtime image; see README.md"
            ) from None
        if result.returncode:
            raise ValueError(
                f"Missing {name} runtime image; build benchmark.image first (README.md)"
            )
