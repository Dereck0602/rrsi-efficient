"""Selected dependency plans and automatic native resource preparation."""

import asyncio
import contextlib
import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml

from flashrsi import _launch, _startup
from flashrsi.config import load_config
from flashrsi.runner import _AGENTS, _ALGORITHMS, _BENCHMARKS
from scripts import run_experiment as launcher


def config(benchmark="searchqa", agent="miniagent", algorithm="skillopt", worker="llm"):
    return load_config(
        overrides={
            "benchmark": {"name": benchmark},
            "agent": {
                "name": agent,
                "llm": {"model_name": "target", "api_key": "target-secret"},
            },
            "algorithm": {
                "name": algorithm,
                "worker": {
                    "type": "llm" if worker == "llm" else "agent",
                    **({"name": worker} if worker != "llm" else {}),
                    "llm": {"model_name": "worker", "api_key": "worker-secret"},
                },
            },
        }
    )


class GenericLauncherTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="flashrsi-launch-test-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))

    def test_child_commands_use_selected_runtime(self):
        path = self.root / "experiment.yaml"
        path.touch()
        python = (
            self.root
            / "runtime"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        with (
            patch.object(_launch, "_load", return_value=config()),
            patch.object(_launch, "_validate_host"),
            patch.object(launcher, "_runtime", return_value=python),
            patch.object(launcher.subprocess, "run") as run,
        ):
            run.return_value.returncode = 0
            self.assertEqual(launcher.main(["--_launch", "--config", str(path)]), 0)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["PATH"].split(os.pathsep)[0], str(python.parent))
        self.assertEqual(env["VIRTUAL_ENV"], str(python.parent.parent))

    def test_dependency_table_covers_runner_and_declared_extras(self):
        self.assertEqual(set(_launch._BENCHMARK_EXTRAS), set(_BENCHMARKS))
        project = tomllib.loads((launcher.ROOT / "pyproject.toml").read_text())
        extras = set(project["project"]["optional-dependencies"])
        self.assertEqual(extras, {"all", "harbor"})
        for benchmark in _BENCHMARKS:
            for agent in _AGENTS:
                for algorithm in _ALGORITHMS:
                    worker = (
                        "claude_code"
                        if algorithm
                        in {
                            "ahe",
                            "arbor",
                            "adas",
                            "darwinx",
                            "evo_harness",
                            "meta_harness",
                            "modular_rsi",
                            "rrsi",
                        }
                        else "llm"
                    )
                    plan = _launch._plan(config(benchmark, agent, algorithm, worker))
                    self.assertLessEqual(set(plan["extras"]), extras)
                    expected = {"all"}
                    if benchmark in _launch._HARBOR:
                        expected.add("harbor")
                    self.assertEqual(set(plan["extras"]), expected)
        self.assertNotIn("harbor", _launch._plan(config("tau3_banking"))["extras"])
        self.assertNotIn("tau2", _launch._plan(config("terminal_bench"))["extras"])

    def test_invalid_algorithm_and_ahe_worker_fail_before_install(self):
        for cfg in (config(algorithm="unknown"), config(algorithm="ahe")):
            with self.assertRaises(ValueError):
                _launch._plan(cfg)

    def test_yaml_relative_paths_and_independent_judge_overrides(self):
        path = self.root / "experiment.yaml"
        values = json.loads(json.dumps(config(), default=dict))
        values["benchmark"]["data_path"] = {"train": ["data/train.json"]}
        values["benchmark"]["evolution_data_path"] = "external-tasks"
        values["agent"]["source_dir"] = "my-agent"
        path.write_text(yaml.safe_dump(values), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLASHRSI_")}
        env["FLASHRSI_JUDGE_API_KEY"] = "judge-secret"
        with patch.dict(os.environ, env, clear=True):
            loaded = _launch._load(path)
        self.assertEqual(
            loaded.benchmark.data_path.train, [str(self.root / "data/train.json")]
        )
        self.assertEqual(loaded.agent.source_dir, str(self.root / "my-agent"))
        self.assertEqual(
            loaded.benchmark.evolution_data_path, str(self.root / "external-tasks")
        )
        self.assertEqual(loaded.benchmark.judge.llm.api_key, "judge-secret")
        self.assertEqual(loaded.agent.llm.api_key, "target-secret")

    def test_pi_rejects_external_node_runtime(self):
        path = self.root / "experiment.yaml"
        values = json.loads(json.dumps(config("searchqa", agent="pi"), default=dict))
        values["agent"]["llm"]["model_name"] = "openai/target"
        values["agent"]["node_path"] = "node"
        path.write_text(yaml.safe_dump(values), encoding="utf-8")
        loaded = _launch._load(path)
        with self.assertRaisesRegex(ValueError, "node_path"):
            _startup._role(loaded.agent, "agent", "pi")

    def test_hermes_plans_as_target_and_agent_worker(self):
        for benchmark in ("searchqa", "terminal_bench"):
            for target, algorithm, worker in (
                ("hermes", "skillopt", "llm"),
                ("pi", "ahe", "hermes"),
            ):
                with self.subTest(benchmark=benchmark, target=target, worker=worker):
                    plan = _launch._plan(config(benchmark, target, algorithm, worker))
                    self.assertEqual(plan["agent"], target)
                    self.assertEqual(plan["worker"], worker)
                    self.assertEqual(
                        set(plan["extras"]),
                        {"all", "harbor"} if benchmark == "terminal_bench" else {"all"},
                    )
                    self.assertEqual(plan["requirements"], [])
                    self.assertNotIn("secret", json.dumps(plan))

    def test_hermes_roles_keep_independent_connections(self):
        path = self.root / "hermes.yaml"
        values = json.loads(
            json.dumps(config(agent="hermes", worker="hermes"), default=dict)
        )
        path.write_text(yaml.safe_dump(values), encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLASHRSI_")}
        env.update(
            {
                "FLASHRSI_AGENT_MODEL": "openai/target-runtime",
                "FLASHRSI_AGENT_API_KEY": "independent-target-key",
                "FLASHRSI_AGENT_BASE_URL": "https://target.test/v1",
                "FLASHRSI_WORKER_MODEL": "openai/worker-runtime",
                "FLASHRSI_WORKER_API_KEY": "independent-worker-key",
                "FLASHRSI_WORKER_BASE_URL": "https://worker.test/v1",
            }
        )
        with patch.dict(os.environ, env, clear=True):
            loaded = _launch._load(path)
        self.assertEqual(loaded.agent.llm.model_name, "openai/target-runtime")
        self.assertEqual(loaded.agent.llm.api_key, "independent-target-key")
        self.assertEqual(loaded.agent.llm.base_url, "https://target.test/v1")
        worker = loaded.algorithm.worker
        self.assertEqual(worker.llm.model_name, "openai/worker-runtime")
        self.assertEqual(worker.llm.api_key, "independent-worker-key")
        self.assertEqual(worker.llm.base_url, "https://worker.test/v1")
        _startup._role(loaded.agent, "agent", "hermes")
        _startup._role(worker, "algorithm.worker", "hermes")
        worker.llm.api_key = None
        with self.assertRaisesRegex(ValueError, "api_key"):
            _startup._role(worker, "algorithm.worker", "hermes")
        self.assertEqual(loaded.agent.llm.api_key, "independent-target-key")

    def test_hermes_rejects_host_cli(self):
        cfg = config(agent="hermes")
        cfg.agent.cli_path = "hermes"
        with self.assertRaisesRegex(ValueError, "cli_path"):
            _startup._role(cfg.agent, "agent", "hermes")

    def test_opencode_plans_and_rejects_host_runtime(self):
        cfg = config(agent="opencode")
        plan = _launch._plan(cfg)
        self.assertEqual(plan["agent"], "opencode")
        cfg.agent.node_path = "node"
        with self.assertRaisesRegex(ValueError, "node_path"):
            _startup._role(cfg.agent, "agent", "opencode")

    def test_plan_does_not_install_selected_dependencies_or_expose_keys(self):
        path = self.root / "plan.yaml"
        path.touch()
        out = io.StringIO()
        with (
            patch.object(_launch, "_load", return_value=config("hle")),
            patch.object(launcher, "_runtime") as install,
            contextlib.redirect_stdout(out),
        ):
            self.assertEqual(
                launcher.main(["--_launch", "--plan", "--config", str(path)]), 0
            )
            install.assert_not_called()
        self.assertIn('"hle"', out.getvalue())
        self.assertNotIn("secret", out.getvalue())

    def test_invalid_yaml_does_not_echo_credential_lines(self):
        path = self.root / "invalid.yaml"
        path.write_text('api_key: "private-api-secret" trailing', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Invalid configuration YAML") as caught:
            _launch._load(path)
        self.assertNotIn("private-api-secret", str(caught.exception))

    def test_host_constraints_are_reported_before_install(self):
        with patch.object(_launch.sys, "version_info", (3, 11)):
            with self.assertRaisesRegex(ValueError, "Python >=3.12"):
                _launch._validate_host(config("terminal_bench"))
        with patch.object(_launch.shutil, "which", return_value=None):
            with self.assertRaisesRegex(ValueError, "Docker"):
                _launch._validate_host(config("appworld"))
            with patch.object(_launch.sys, "version_info", (3, 12)):
                for name in ("terminal_bench", "jobbench"):
                    with self.assertRaisesRegex(ValueError, "Git"):
                        _launch._validate_host(config(name))

    def test_conflicting_profiles_get_separate_environments(self):
        def create(builder, directory):
            exe = directory / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            exe.parent.mkdir(parents=True)
            exe.touch()

        with (
            patch.object(launcher.venv.EnvBuilder, "create", create),
            patch.object(launcher.subprocess, "run"),
        ):
            tau = _launch._plan(config("tau3_banking"))
            harbor = _launch._plan(config("terminal_bench"))
            first = launcher._runtime(self.root, {}, tau["extras"], tau["requirements"])
            second = launcher._runtime(
                self.root, {}, harbor["extras"], harbor["requirements"]
            )
        self.assertNotEqual(first, second)

    def test_appworld_build_download_and_reuse(self):
        commands = []

        async def docker(*args, **kwargs):
            commands.append(args)
            if args[:2] == ("image", "inspect"):
                return subprocess.CompletedProcess(args, 1 if len(commands) == 1 else 0)
            if args[0] == "run":
                mount = args[args.index("--mount") + 1]
                root = Path(
                    mount.removeprefix("type=bind,source=").removesuffix(
                        ",target=/world"
                    )
                )
                # The native CLI's --root defaults to cwd, even when the image
                # sets APPWORLD_ROOT. Exit zero without publishing mounted
                # data if the command still runs in the image's default '/'.
                cwd = args[args.index("--workdir") + 1] if "--workdir" in args else "/"
                if cwd != "/world":
                    return subprocess.CompletedProcess(args, 0)
                for part in ("datasets", "tasks", "base_dbs"):
                    (root / "data" / part).mkdir(parents=True)
            return subprocess.CompletedProcess(args, 0)

        with (
            patch.object(_launch, "_check_docker", new_callable=AsyncMock),
            patch("flashrsi.integrations._docker._docker", side_effect=docker),
        ):
            first = config("appworld")
            asyncio.run(_launch._resources(first, self.root))
            second = config("appworld")
            asyncio.run(_launch._resources(second, self.root))
        self.assertEqual(first.benchmark.data_path, second.benchmark.data_path)
        self.assertEqual(sum(c[0] == "build" for c in commands), 1)
        self.assertEqual(sum(c[0] == "run" for c in commands), 1)

    def test_explicit_local_data_and_custom_image_are_honored(self):
        cfg = config("appworld")
        cfg.benchmark.data_path = str(self.root / "user-data")
        cfg.benchmark.image = "registry.example/custom:v1"
        with (
            patch.object(_launch, "_check_docker", new_callable=AsyncMock),
            patch(
                "flashrsi.integrations._docker._docker", new_callable=AsyncMock
            ) as docker,
        ):
            docker.return_value = subprocess.CompletedProcess([], 1)
            asyncio.run(_launch._resources(cfg, self.root))
        self.assertEqual([c.args[0] for c in docker.call_args_list], ["image", "pull"])
        self.assertEqual(docker.call_args_list[1].args[1], "registry.example/custom:v1")
        self.assertEqual(cfg.benchmark.data_path, str(self.root / "user-data"))

    def test_failed_appworld_download_does_not_publish_cache(self):
        async def docker(*args, **kwargs):
            if args[0] == "run":
                raise RuntimeError("download failed")
            return subprocess.CompletedProcess(args, 0)

        with (
            patch.object(_launch, "_check_docker", new_callable=AsyncMock),
            patch("flashrsi.integrations._docker._docker", side_effect=docker),
        ):
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                asyncio.run(_launch._resources(config("appworld"), self.root))
        self.assertFalse((self.root / "data/appworld-0.1.3.post1").exists())
        self.assertEqual(list(self.root.glob("data/.appworld-*")), [])

    def test_tau_data_uses_same_revision_as_installed_simulator(self):
        from flashrsi.benchmarks.tau3_banking import _UPSTREAM_REVISION

        env = {k: v for k, v in os.environ.items() if k != "TAU2_DATA_DIR"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "flashrsi.benchmarks._git._download_git", return_value=self.root
            ) as download,
        ):
            asyncio.run(_launch._resources(config("tau3_banking"), self.root))
            self.assertEqual(os.environ["TAU2_DATA_DIR"], str(self.root / "data"))
            self.assertEqual(download.call_args.args[1], _UPSTREAM_REVISION)
            self.assertTrue(_launch._TAU_REQUIREMENT.endswith(_UPSTREAM_REVISION))
            asyncio.run(_launch._resources(config("tau3_banking"), self.root))
            self.assertEqual(download.call_count, 1)


if __name__ == "__main__":
    unittest.main()
