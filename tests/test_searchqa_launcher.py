"""Launcher contracts and a local HTTP end-to-end experiment; no paid API calls."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from scripts import run_experiment as launcher


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flashrsi-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.output = io.StringIO()
        self.enterContext(contextlib.redirect_stdout(self.output))
        self.enterContext(contextlib.redirect_stderr(self.output))
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLASHRSI_")}
        self.enterContext(patch.dict(os.environ, env, clear=True))

    def config(self, *, credentials=True, local=False):
        config = yaml.safe_load(launcher.TEMPLATE.read_text(encoding="utf-8"))
        for name, section in (
            ("target", config["agent"]),
            ("worker", config["algorithm"]["worker"]),
        ):
            section["llm"].update(
                model_name=name, api_key=f"test-{name}-secret" if credentials else None
            )
        if local:
            paths = {}
            for role in ("train", "dev", "test"):
                path = self.root / f"{role}.json"
                path.write_text(json.dumps([self.row(role)]), encoding="utf-8")
                paths[role] = str(path)
            config["benchmark"]["data_path"] = paths
        path = self.root / "experiment.yaml"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        return path, config

    @staticmethod
    def row(role):
        return {
            "id": role,
            "question": "Capital of France?",
            "context": "Paris is the capital.",
            "answer": "Paris",
        }

    def test_first_launch_creates_config_without_installing(self):
        with (
            patch.object(launcher, "ROOT", self.root),
            patch.object(launcher, "_runtime") as install,
        ):
            self.assertEqual(launcher.main([]), 2)
            self.assertEqual(
                (self.root / "experiment.local.yaml").read_bytes(),
                launcher.TEMPLATE.read_bytes(),
            )
            install.assert_not_called()

    def test_cache_environment_preserves_explicit_locations(self):
        with patch.dict(
            os.environ, {"HF_HOME": "custom-hf", "PIP_CACHE_DIR": "custom-pip"}
        ):
            env = launcher._environment(self.root)
        self.assertEqual(env["HF_HOME"], "custom-hf")
        self.assertEqual(env["PIP_CACHE_DIR"], "custom-pip")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")

    def test_failed_install_retries_and_successful_install_is_reused(self):
        project = self.root / "project"
        (project / "flashrsi").mkdir(parents=True)
        source = project / "flashrsi" / "__init__.py"
        source.write_text("# version one")
        (project / "pyproject.toml").write_text("# fixture")

        def create(builder, directory):
            executable = directory / (
                "Scripts/python.exe" if os.name == "nt" else "bin/python"
            )
            executable.parent.mkdir(parents=True)
            executable.touch()

        with (
            patch.object(launcher, "ROOT", project),
            patch.object(launcher.venv.EnvBuilder, "create", create),
        ):
            with patch.object(
                launcher.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(1, "pip"),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    launcher._runtime(self.root / "cache", {})
            self.assertEqual(list(self.root.rglob("flashrsi-source.sha256")), [])
            with patch.object(launcher.subprocess, "run") as run:
                launcher._runtime(self.root / "cache", {})
                self.assertEqual(run.call_count, 2)
                launcher._runtime(self.root / "cache", {})
                self.assertEqual(run.call_count, 2)
                source.write_text("# version two")
                launcher._runtime(self.root / "cache", {})
                self.assertEqual(run.call_count, 4)

    def test_missing_credentials_fail_before_download(self):
        path, _ = self.config(credentials=False)
        with patch("huggingface_hub.hf_hub_download") as download:
            self.assertEqual(launcher._run(path, True), 1)
            download.assert_not_called()
        self.assertIn("agent.llm.api_key", self.output.getvalue())
        self.assertFalse((self.root / "runs").exists())

    def test_target_credentials_do_not_fill_worker_credentials(self):
        path, _ = self.config(credentials=False)
        with patch.dict(os.environ, {"FLASHRSI_AGENT_API_KEY": "only-target-secret"}):
            self.assertEqual(launcher._run(path, True), 1)
        self.assertIn("algorithm.worker.llm.api_key", self.output.getvalue())
        self.assertNotIn("only-target-secret", self.output.getvalue())

    def test_check_only_uses_native_archives_without_api_calls_or_outputs(self):
        path, _ = self.config()
        archives = {}
        for role, native in (("train", "train"), ("dev", "val"), ("test", "test")):
            archive = self.root / f"{native}.zip"
            with zipfile.ZipFile(archive, "w") as stream:
                row = self.row(role)
                row.pop("context")
                row.update(
                    id=0,
                    search_results=[
                        {"snippet": "Paris is the capital."},
                        {"snippet": None},
                    ],
                )
                stream.writestr("question.json", json.dumps(row))
            archives[f"data/train_test_val/{native}.zip"] = str(archive)

        def download(**kwargs):
            self.assertEqual(kwargs["repo_id"], "kyunghyuncho/search_qa")
            self.assertEqual(
                kwargs["revision"], "06907e45883b7cae435453b65d598447039fde79"
            )
            return archives[kwargs["filename"]]

        with (
            patch("huggingface_hub.hf_hub_download", side_effect=download) as fetch,
            patch("shutil.which", side_effect=lambda name: name),
            patch(
                "flashrsi._startup._docker",
                return_value=SimpleNamespace(returncode=0, stdout="linux"),
            ) as docker,
        ):
            with patch(
                "flashrsi.agents.MiniAgent._run",
                side_effect=AssertionError("No models during check"),
            ):
                self.assertEqual(launcher._run(path, True), 0)
            self.assertEqual(fetch.call_count, 3)
            self.assertTrue(
                all(call.args[0] == "info" for call in docker.call_args_list)
            )
            self.assertGreater(docker.call_count, 0)
        self.assertIn("train=1, dev=1, test=1", self.output.getvalue())
        self.assertFalse((self.root / "runs").exists())

    @unittest.skipUnless(
        os.environ.get("FLASHRSI_TEST_PYTHON"),
        "Set FLASHRSI_TEST_PYTHON to the bootstrapped environment Python",
    )
    def test_installed_launcher_switches_algorithms_and_benchmarks(self):
        python = TEST_PYTHON
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                calls.append(request)
                if request["model"] == "worker":
                    content = json.dumps(
                        {
                            "edits": [
                                {
                                    "op": "append",
                                    "path": "skills/main.md",
                                    "content": "QUICKSTART_TEST_SKILL",
                                }
                            ]
                        }
                    )
                    if "<INS>" in json.dumps(request["messages"]):
                        content = "<INS>QUICKSTART_TEST_SKILL</INS>"
                else:
                    evolved = "QUICKSTART_TEST_SKILL" in json.dumps(request["messages"])
                    content = "Paris" if evolved else "London"
                response = json.dumps(
                    {
                        "id": "local-test",
                        "object": "chat.completion",
                        "created": 0,
                        "model": request["model"],
                        "choices": [
                            {
                                "index": 0,
                                "finish_reason": "stop",
                                "message": {"role": "assistant", "content": content},
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        path, config = self.config(local=True)
        for section in (config["agent"], config["algorithm"]["worker"]):
            section["llm"]["base_url"] = f"http://127.0.0.1:{server.server_port}/v1"
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        command = [
            python,
            "-B",
            "-X",
            "utf8",
            str(launcher.ROOT / "scripts/run_experiment.py"),
            "--config",
            str(path),
        ]
        checked = subprocess.run(
            command + ["--check-only"],
            capture_output=True,
            encoding="utf-8",
            timeout=60,
        )
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn("Reusing Python environment", checked.stdout)
        self.assertEqual(calls, [])
        self.assertFalse((self.root / "runs").exists())
        for _ in range(2):
            completed = subprocess.run(
                command, capture_output=True, encoding="utf-8", timeout=120
            )
            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
        results = list((self.root / "runs").glob("*/result.json"))
        self.assertEqual(len(results), 2)
        # Switch both selectors while reusing the same dependency environment.
        worker = config["algorithm"]["worker"]
        config["algorithm"] = {
            "name": "opro",
            "max_steps": 1,
            "num_candidates": 1,
            "few_shot_size": 1,
            "worker": worker,
        }
        config["benchmark"]["name"] = "hotpotqa"
        for role, filename in config["benchmark"]["data_path"].items():
            row = self.row(role)
            row["context"] = [["France", ["Paris is the capital."]]]
            Path(filename).write_text(json.dumps([row]), encoding="utf-8")
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        switched = subprocess.run(
            command, capture_output=True, encoding="utf-8", timeout=120
        )
        self.assertEqual(switched.returncode, 0, switched.stdout + switched.stderr)
        self.assertIn("Selected: opro + miniagent + hotpotqa", switched.stdout)
        self.assertIn("Reusing Python environment", switched.stdout)
        results = list((self.root / "runs").glob("*/result.json"))
        self.assertEqual(len(results), 3)
        self.assertEqual(
            sum(p.parent.name.startswith("opro-miniagent-hotpotqa-") for p in results),
            1,
        )
        for result in results:
            summary = json.loads(result.read_text(encoding="utf-8"))
            self.assertEqual(summary["test_score"], 1.0)
            self.assertEqual(summary["test_tasks"], 1)
        self.assertTrue(any(call["model"] == "worker" for call in calls))
        persisted = "\n".join(
            p.read_text(encoding="utf-8")
            for p in (self.root / "runs").rglob("*")
            if p.is_file()
        )
        self.assertNotIn("test-target-secret", persisted)
        self.assertNotIn("test-worker-secret", persisted)


TEST_PYTHON = os.environ.get("FLASHRSI_TEST_PYTHON")


if __name__ == "__main__":
    unittest.main()
