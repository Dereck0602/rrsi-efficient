"""Load the experiment configuration described in example.yaml."""

from __future__ import annotations

import copy
import math
import re
import shutil
from collections.abc import Iterator, Mapping, MutableMapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from flashrsi._records import _redact_config

_SPLIT_RATIOS = {"train": 0.6, "dev": 0.2, "test": 0.2}

# Previously accepted request fields now have one unambiguous location.
_LLM_FIELDS = (
    "model_name",
    "api_key",
    "base_url",
    "max_tokens",
    "reasoning_effort",
    "effort",
    "llm_max_retries",
    "llm_retry_policy",
    "extra_body",
)

_DEFAULTS = {
    "run": {"output_dir": "runs"},
    "benchmark": {
        "name": "searchqa",
        "split": "auto",
    },
    "agent": {
        "name": "miniagent",
        "llm": {"model_name": None, "api_key": None, "base_url": None},
    },
    "algorithm": {
        "name": "skillopt",
        "worker": {
            "type": "llm",
            "llm": {"model_name": None, "api_key": None, "base_url": None},
        },
    },
}


class Config(MutableMapping[str, Any]):
    """A configuration mapping with recursive attribute access.

    Both ``config.benchmark.name`` and ``config["benchmark"]["name"]`` work.
    Keys matching mapping methods, such as ``items``, use bracket access.
    """

    def __init__(self, values: Mapping[str, Any] | None = None):
        object.__setattr__(self, "_values", {})
        if values is not None:
            self.update(values)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __setitem__(self, key: str, value: Any) -> None:
        if not isinstance(key, str):
            raise ValueError("Configuration keys must be strings")
        self._values[key] = _wrap(value)

    def __delitem__(self, key: str) -> None:
        del self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        values = object.__getattribute__(self, "_values")
        try:
            return values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __setattr__(self, name: str, value: Any) -> None:
        self[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self[name]
        except KeyError:
            raise AttributeError(name) from None


def _wrap(value: Any) -> Any:
    if isinstance(value, Mapping):
        return Config(value)
    if isinstance(value, list):
        return [_wrap(item) for item in value]
    return value


class _ConfigLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently discarding settings."""


def _unique_mapping(loader: _ConfigLoader, node: yaml.MappingNode) -> dict:
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ValueError("Configuration keys must be strings")
        if key in result:
            raise ValueError(f"Duplicate configuration key: {key}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_ConfigLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping
)


def load_config(
    path: str | Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
) -> Config:
    """Merge defaults < YAML < overrides and validate the configuration.

    ``run.output_dir`` is relative to the YAML file, or to the current directory
    when set through overrides. This function does not create directories or
    read environment variables. Each category accepts additional parameters;
    nested mappings support attribute access. Target and worker model settings
    live in their independent ``llm`` mappings. Credentials remain in the result.
    """
    base_dir = Path.cwd()
    values = {}
    if path is not None:
        config_path = Path(path).expanduser().resolve()
        base_dir = config_path.parent
        try:
            with config_path.open(encoding="utf-8") as stream:
                values = yaml.load(stream, Loader=_ConfigLoader)
        except yaml.YAMLError as exc:
            raise ValueError(f"Invalid YAML in {config_path}: {exc}") from exc
        if values is None:
            values = {}

    if not isinstance(values, Mapping):
        raise ValueError("Configuration must be a mapping")
    if overrides is None:
        overrides = {}
    if not isinstance(overrides, Mapping):
        raise ValueError("Configuration overrides must be a mapping")

    config = _merge(_merge(_DEFAULTS, values), overrides)
    if isinstance(config["run"], Mapping) and config["run"].get("mode") == "evaluate":
        # Evaluation has no search algorithm or Worker, including in saved configs.
        config["algorithm"] = {"name": "evaluation"}
    _validate(config)
    origin = Path.cwd() if "output_dir" in overrides.get("run", {}) else base_dir
    output_dir = Path(config["run"]["output_dir"]).expanduser()
    if not output_dir.is_absolute():
        output_dir = origin / output_dir
    config["run"]["output_dir"] = str(output_dir.resolve())
    return Config(config)


def prepare_run_dir(config: Mapping[str, Any]) -> Path:
    """Create ``{algo}-{agent_name}-{bench}-{time}`` under run.output_dir.

    Time is local, with microseconds. A collision advances the timestamp by one
    microsecond. The saved config omits credential values; the input is unchanged.
    This function prepares directories but does not execute an experiment.
    """
    resolved = load_config(overrides=config)
    output_dir = Path(resolved.run.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = "-".join(
        resolved[section].name for section in ("algorithm", "agent", "benchmark")
    )
    timestamp = datetime.now()
    while True:
        run_dir = output_dir / f"{prefix}-{timestamp:%Y%m%d-%H%M%S-%f}"
        try:
            run_dir.mkdir(mode=0o700)
            break
        except FileExistsError:
            timestamp += timedelta(microseconds=1)

    saved = _redact_config(resolved)
    try:
        for name in ("snapshots", "trajectories", "working"):
            (run_dir / name).mkdir()
        (run_dir / "config.yaml").write_text(
            yaml.safe_dump(saved, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    except Exception:
        shutil.rmtree(run_dir)
        raise
    return run_dir


def _merge(base: Mapping, updates: Mapping) -> dict:
    result = copy.deepcopy(dict(base))
    for key, value in updates.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _validate(config: dict) -> None:
    _reject_unknown(config, _DEFAULTS, "configuration")
    for section in _DEFAULTS:
        values = config[section]
        if not isinstance(values, Mapping):
            raise ValueError(f"{section} must be a mapping")
        if section != "run":
            _require_name(values["name"], f"{section}.name")

    output_dir = config["run"]["output_dir"]
    if isinstance(output_dir, Path):
        output_dir = str(output_dir)
    _require_text(output_dir, "run.output_dir")
    if config["run"].get("progress", "auto") not in ("auto", "bar", "log", "off"):
        raise ValueError("run.progress must be auto, bar, log, or off")

    split = config["benchmark"]["split"]
    if split != "auto":
        if not isinstance(split, Mapping):
            raise ValueError("benchmark.split must be 'auto' or a mapping")
        _reject_unknown(split, _SPLIT_RATIOS, "benchmark.split")
        split = {**_SPLIT_RATIOS, **split}
        for name, ratio in split.items():
            if type(ratio) not in (int, float) or not 0 <= ratio <= 1:
                raise ValueError(
                    f"benchmark.split.{name} must be a number between 0 and 1"
                )
        if not math.isclose(sum(split.values()), 1.0, rel_tol=0, abs_tol=1e-9):
            raise ValueError("benchmark.split ratios must sum to 1")
        config["benchmark"]["split"] = split

    mode = config["run"].get("mode", "run")
    if mode not in ("run", "evaluate"):
        raise ValueError("run.mode must be run or evaluate")
    roles = [("agent", config["agent"])]
    if mode == "evaluate":
        _require_text(config["run"].get("snapshot"), "run.snapshot")
    else:
        worker = config["algorithm"]["worker"]
        if not isinstance(worker, Mapping):
            raise ValueError("algorithm.worker must be a mapping")
        if worker["type"] not in ("llm", "agent"):
            raise ValueError("algorithm.worker.type must be 'llm' or 'agent'")
        if worker["type"] == "agent":
            _require_name(worker.get("name"), "algorithm.worker.name")
        elif worker.get("name") is not None:
            raise ValueError("algorithm.worker.name requires type: agent")

        roles.append(("algorithm.worker", worker))

    for section, values in roles:
        _validate_role_config(values, section)
        llm = values["llm"]
        if not isinstance(llm, Mapping):
            raise ValueError(f"{section}.llm must be a mapping")
        _require_text(llm["model_name"], f"{section}.llm.model_name")
        for field in ("api_key", "base_url"):
            if llm[field] is not None:
                _require_text(llm[field], f"{section}.llm.{field}")


def _validate_role_config(values: Mapping[str, Any], section: str) -> None:
    """Reject old field placement even when constructing an Agent directly."""
    for field in _LLM_FIELDS:
        if field in values:
            raise ValueError(f"Move {section}.{field} to {section}.llm.{field}")
    if "llm" in values and not isinstance(values["llm"], Mapping):
        raise ValueError(f"{section}.llm must be a mapping")
    llm = values.get("llm", {})
    if "llm_retry_policy" in llm:
        if llm["llm_retry_policy"] not in ("legacy", "transient"):
            raise ValueError(
                f"{section}.llm.llm_retry_policy must be legacy or transient"
            )
        if llm["llm_retry_policy"] == "transient":
            attempts = llm.get("llm_max_retries", 1)
            if type(attempts) is not int or attempts < 1:
                raise ValueError(
                    f"{section}.llm.llm_max_retries must be a positive integer"
                )


def _require_name(value: Any, field: str) -> None:
    _require_text(value, field)
    if re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise ValueError(f"{field} must use letters, numbers, underscores or hyphens")


def _require_text(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")


def _reject_unknown(values: Mapping, fields: Mapping, section: str) -> None:
    unknown = values.keys() - fields.keys()
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ValueError(f"Unknown keys in {section}: {names}")
