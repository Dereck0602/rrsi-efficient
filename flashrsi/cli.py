"""Command-line configuration and experiment preparation."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from flashrsi._records import _redact, _secrets
from flashrsi.config import _ConfigLoader, load_config, prepare_run_dir


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        normalized = _dotted_args(sys.argv[1:] if argv is None else argv)
    except ValueError as exc:
        parser.error(str(exc))
    args, unknown = parser.parse_known_args(normalized)
    if unknown:
        parser.error("Unrecognized arguments; use 'flashrsi run --help' for usage.")
    try:
        config = load_config(args.config, overrides=_overrides(args))
    except ValueError as exc:
        if isinstance(exc.__cause__, yaml.YAMLError):
            parser.error("Invalid YAML in configuration file; check its syntax.")
        parser.error(str(exc))
    except OSError as exc:
        parser.error(str(exc))

    if args.command == "evaluate":
        from flashrsi.runner import _evaluate, _prepare_evaluation

        try:
            if args.check_only:
                target, benchmark = asyncio.run(_prepare_evaluation(config))
                print(
                    f"Evaluation checks passed: snapshot={target.snapshot.id}, test={len(benchmark.test)}"
                )
                print(
                    "No model calls or experiment directory created; remote APIs were not tested."
                )
                return 0
            run_dir, result = asyncio.run(_evaluate(config))
        except Exception as exc:
            parser.error(_redact(str(exc), _secrets(config) + _secrets(os.environ)))
        print(f"Completed evaluation: {run_dir}")
        print(f"Snapshot: {result['snapshot_id']}")
        print(f"Test {result['primary_metric']}: {result['test_score']}")
        return 0

    if not args.prepare_only:
        from flashrsi.runner import _prepare, _run

        try:
            if args.check_only:
                algorithm = asyncio.run(_prepare(config))
                counts = ", ".join(
                    f"{role}={len(getattr(algorithm.benchmark, role))}"
                    for role in ("train", "dev", "test")
                )
                print(f"Startup checks passed: {counts}")
                print(
                    "No tasks or model calls were run; no experiment directory created."
                )
                print(
                    "Remote API authentication and model availability were not tested."
                )
                return 0
            run_dir, result = asyncio.run(_run(config))
        except Exception as exc:
            parser.error(_redact(str(exc), _secrets(config) + _secrets(os.environ)))
        print(f"Completed experiment: {run_dir}")
        print(f"Snapshot: {result['snapshot_id']}")
        print(f"Test {result['primary_metric']}: {result['test_score']}")
        return 0

    try:
        run_dir = prepare_run_dir(config)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    print(f"Prepared experiment: {run_dir}")
    print(f"Configuration: {run_dir / 'config.yaml'}")
    print("Preparation only; no tasks or model calls were run.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flashrsi", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("run", "evaluate"):
        sub = commands.add_parser(
            command,
            allow_abbrev=False,
            help="Run evolution"
            if command == "run"
            else "Test a saved Snapshot without evolution",
            epilog="Any configuration field accepts --path.to.key VALUE or --set path.to.key=VALUE.",
        )
        sub.add_argument(
            "--config", "-c", metavar="PATH", help="Read configuration from YAML"
        )
        sub.add_argument("--benchmark", metavar="NAME", help="Override benchmark.name")
        sub.add_argument(
            "--agent-harness",
            "--agent",
            dest="agent",
            metavar="NAME",
            help="Override agent.name (target harness)",
        )
        sub.add_argument(
            "--model", metavar="NAME", help="Override agent.llm.model_name"
        )
        sub.add_argument("--output-dir", metavar="PATH", help="Override run.output_dir")
        sub.add_argument(
            "--set",
            dest="settings",
            metavar="PATH=VALUE",
            action="append",
            default=[],
            help="Set a dotted configuration path to a YAML value",
        )
        preparation = sub.add_mutually_exclusive_group()
        preparation.add_argument(
            "--check-only",
            action="store_true",
            help="Check startup without model calls; may download native data",
        )
        if command == "run":
            sub.add_argument(
                "--resume-from",
                metavar="RUN_DIR",
                help="Resume RRSI into a new run directory",
            )
            sub.add_argument(
                "--resume-mode",
                choices=("round", "trial"),
                default=None,
                help="Reuse completed rounds or also completed trials (default: trial)",
            )
            sub.add_argument(
                "--algorithm",
                "--algo",
                dest="algorithm",
                metavar="NAME",
                help="Override algorithm.name",
            )
            sub.add_argument(
                "--worker-model",
                metavar="NAME",
                help="Override algorithm.worker.llm.model_name",
            )
            preparation.add_argument(
                "--prepare-only",
                action="store_true",
                help="Create a configuration directory without checking runtime readiness",
            )
        else:
            sub.add_argument(
                "--snapshot",
                required=True,
                metavar="PATH",
                help="Saved Snapshot JSON, relative to the working directory",
            )
    return parser


def _dotted_args(argv: Sequence[str]) -> list[str]:
    """Normalize dotted flags to --set while preserving setting order."""
    result = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            result.extend(argv[index:])
            break
        flag, separator, value = arg.partition("=")
        if flag == "--set" and not separator:
            # Its payload is a single argument, not another option to normalize.
            result.extend(argv[index : index + 2])
            index += 2
            continue
        if flag.startswith("--") and "." in flag:
            if not separator:
                index += 1
                if index >= len(argv) or _looks_like_option(argv[index]):
                    raise ValueError(f"{flag} requires a value; use {flag}=VALUE")
                value = argv[index]
            result.extend(["--set", f"{flag[2:]}={value}"])
        else:
            result.append(arg)
        index += 1
    return result


def _looks_like_option(value: str) -> bool:
    if not value.startswith("-"):
        return False
    try:
        float(value)
    except ValueError:
        return True
    return False


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    result: dict[str, Any] = {}
    fields = {
        "resume_from": "run.resume_from",
        "resume_mode": "run.resume_mode",
        "benchmark": "benchmark.name",
        "algorithm": "algorithm.name",
        "agent": "agent.name",
        "model": "agent.llm.model_name",
        "worker_model": "algorithm.worker.llm.model_name",
        "output_dir": "run.output_dir",
    }
    for flag, path in fields.items():
        value = getattr(args, flag, None)
        if value is not None:
            _set_value(result, path, value)

    for setting in args.settings:
        path, separator, raw_value = setting.partition("=")
        if not separator:
            raise ValueError("--set requires PATH=VALUE, e.g. algorithm.rounds=3")
        try:
            value = yaml.load(raw_value, Loader=_ConfigLoader)
        except (yaml.YAMLError, ValueError):
            # Parser diagnostics may include a credential supplied as the value.
            raise ValueError(f"Invalid YAML value for --set {path}") from None
        _set_value(result, path, value)
    result.setdefault("run", {})["mode"] = (
        "evaluate" if args.command == "evaluate" else "run"
    )
    if args.command == "evaluate":
        result["run"]["snapshot"] = str(Path(args.snapshot).expanduser().resolve())
    return result


def _set_value(values: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    if any(not part or part != part.strip() for part in parts):
        raise ValueError("--set paths must contain non-empty dot-separated keys")
    current = values
    for index, part in enumerate(parts[:-1]):
        current = current.setdefault(part, {})
        if not isinstance(current, dict):
            parent = ".".join(parts[: index + 1])
            raise ValueError(f"Cannot set {path}: {parent} is not a mapping")
    current[parts[-1]] = value
