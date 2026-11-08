"""Collected outputs survive temporary cleanup without copying credentials."""

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_rrsi import saved, setup

pytest.importorskip("harbor")
from harbor.models.trial.paths import TrialPaths

from flashrsi.benchmarks.terminal_bench import TerminalBench
from flashrsi.core import Task
from flashrsi.integrations import harbor


@pytest.fixture
def execution(tmp_path, monkeypatch):
    agent = SimpleNamespace(
        config={
            "llm": {"model_name": "fixture", "api_key": "credential-for-archive-test"},
            "harbor": {"artifacts_dir": str(tmp_path / "saved")},
        },
        snapshot=SimpleNamespace(id="snapshot"),
        _check_container=lambda: None,
    )
    task = Task("../untrusted-task-id", "Edit workbook", env=str(tmp_path / "task"))
    native = SimpleNamespace(
        instruction=task.question,
        checksum="checksum",
        config=SimpleNamespace(
            steps=[],
            environment=SimpleNamespace(os=SimpleNamespace(value="linux")),
            agent=SimpleNamespace(timeout_sec=30),
            verifier=SimpleNamespace(
                environment_mode=harbor.VerifierEnvironmentMode.SEPARATE,
                environment=None,
            ),
        ),
        paths=SimpleNamespace(
            environment_dir=tmp_path / "task/environment",
            tests_dir=tmp_path / "task/tests",
        ),
    )
    monkeypatch.setattr(harbor, "HarborTask", lambda _: native)
    monkeypatch.setattr(harbor.shutil, "which", lambda _: "docker-fixture")
    monkeypatch.setattr(harbor, "_stage", lambda *args: None)
    return agent, task


def outputs(paths):
    paths.mkdir()
    workbook = paths.artifacts_dir / "logs/artifacts/answer.xlsx"
    workbook.parent.mkdir(parents=True)
    workbook.write_bytes(b"PK\x03\x04\x00\xffWORKBOOK")
    paths.test_stdout_path.write_text("credential-for-archive-test")
    (paths.verifier_dir / "native-result.json").write_text('{"accuracy": 1}')
    (paths.agent_dir / "flashrsi.json").write_text(
        json.dumps({"messages": [{"type": "RunConfig", "snapshot_id": "snapshot"}]})
    )
    paths.config_path.write_text('{"api_key": "credential-for-archive-test"}')
    paths.result_path.write_text('{"status": "fixture"}')


@pytest.mark.parametrize("outcome", ["success", "error", "cancel"])
def test_outputs_survive_trial_cleanup(execution, tmp_path, monkeypatch, outcome):
    agent, task = execution
    originals = []

    async def create(config):
        paths = TrialPaths(config.trials_dir / "same-trial-name")
        outputs(paths)
        originals.append(paths.trial_dir)

        async def run():
            if outcome == "cancel":
                raise asyncio.CancelledError()
            if outcome == "error":
                raise RuntimeError("verifier failed")
            return SimpleNamespace(
                id="same-trial-id",
                exception_info=None,
                verifier_result=SimpleNamespace(rewards={"reward": 1}),
            )

        return SimpleNamespace(paths=paths, run=run)

    monkeypatch.setattr(harbor.Trial, "create", create)
    for _ in range(2):
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(harbor._run(agent, task))
        else:
            result = asyncio.run(harbor._run(agent, task))
            assert result.status == ("finished" if outcome == "success" else "error")
            assert Path(result.messages[-1]["artifacts"]["directory"]).is_dir()
    assert all(not path.exists() for path in originals)
    archives = list((tmp_path / "saved").iterdir())
    assert len(archives) == 2  # Repeated task, Snapshot and trial IDs never overwrite.
    for archive in archives:
        assert (archive / "artifacts/logs/artifacts/answer.xlsx").read_bytes() == (
            b"PK\x03\x04\x00\xffWORKBOOK"
        )
        assert json.loads((archive / "verifier/native-result.json").read_text()) == {
            "accuracy": 1
        }
        assert (
            "credential-for-archive-test"
            not in (archive / "verifier/test-stdout.txt").read_text()
        )
        assert not (archive / "config.json").exists()
        manifest = json.loads((archive / "manifest.json").read_text())
        assert manifest["task_id"] == task.task_id
        assert manifest["snapshot_id"] == "snapshot"


def test_simultaneous_repeats_use_independent_trials_and_artifacts(
    execution, tmp_path, monkeypatch
):
    agent, task = execution
    originals, payloads = [], []

    async def scenario():
        both_started = asyncio.Event()

        async def create(config):
            # Even a repeated native trial name is isolated by the adapter's
            # fresh temporary root. Real Harbor additionally makes unique IDs.
            paths = TrialPaths(config.trials_dir / "same-task")
            outputs(paths)
            number = len(originals)
            originals.append(paths.trial_dir)
            payloads.append(Path(config.agent.kwargs["payload_dir"]))
            row = json.loads((paths.agent_dir / "flashrsi.json").read_text())
            row["output"] = str(number)
            (paths.agent_dir / "flashrsi.json").write_text(json.dumps(row))

            async def run():
                if len(originals) == 2:
                    both_started.set()
                await asyncio.wait_for(both_started.wait(), 2)
                assert json.loads((paths.agent_dir / "flashrsi.json").read_text())[
                    "output"
                ] == str(number)
                return SimpleNamespace(
                    id=str(number),
                    exception_info=None,
                    verifier_result=SimpleNamespace(rewards={"reward": 1}),
                )

            return SimpleNamespace(paths=paths, run=run)

        monkeypatch.setattr(harbor.Trial, "create", create)
        return await asyncio.gather(harbor._run(agent, task), harbor._run(agent, task))

    results = asyncio.run(scenario())
    assert [row.output for row in results] == ["0", "1"]
    assert all(row.status == "finished" for row in results)
    assert len(set(originals)) == len(set(payloads)) == 2
    assert all(not p.exists() for p in originals + payloads)
    archives = [Path(row.messages[-1]["artifacts"]["directory"]) for row in results]
    assert len(set(archives)) == 2 and all(p.is_dir() for p in archives)
    assert [
        json.loads((p / "agent/flashrsi.json").read_text())["output"] for p in archives
    ] == ["0", "1"]


@pytest.mark.parametrize("concurrency", [1, 8])
def test_timed_rrsi_rollouts_grade_and_save_native_harbor_results(
    execution, tmp_path, monkeypatch, concurrency
):
    """Exercise the timing wrapper and real grader together, not in isolation."""
    fixture_agent, task = execution
    algo, _ = setup(
        monkeypatch, tmp_path, lambda *_: [], k=2, invalid_missing_frac=0.25
    )
    target = algo.target
    target.config.concurrency = concurrency
    target.config.harbor = fixture_agent.config["harbor"]
    target._env_runner = harbor._run
    tasks = tuple(
        replace(task, task_id=name, metadata={"task_checksum": "checksum"})
        for name in ("pass", "mixed")
    )
    benchmark = TerminalBench({"version": "2.1"})
    benchmark._splits = {"train": tasks, "dev": (), "test": ()}
    benchmark._tasks_by_id = {item.task_id: item for item in tasks}
    algo.benchmark = benchmark
    created = []

    async def create(config):
        paths = TrialPaths(config.trials_dir / "trial")
        outputs(paths)
        index = len(created)
        created.append(paths.trial_dir)
        failed = index == 3
        (paths.agent_dir / "flashrsi.json").write_text(
            json.dumps(
                {
                    "status": "error" if failed else "finished",
                    "error_message": "fixture timeout" if failed else None,
                    "messages": [
                        {"type": "RunConfig", "snapshot_id": target.snapshot.id},
                        {"role": "assistant", "content": "done"},
                    ],
                    "usage": {
                        "total_tokens": 17 if failed else 100,
                        "accounting_complete": not failed,
                    },
                }
            )
        )

        async def run():
            await asyncio.sleep(0)
            return SimpleNamespace(
                id=str(index),
                exception_info=SimpleNamespace(
                    exception_type="NonZeroAgentExitCodeError",
                    exception_message="agent exited with 1",
                )
                if failed
                else None,
                verifier_result=SimpleNamespace(rewards={"reward": int(index != 1)}),
            )

        return SimpleNamespace(paths=paths, run=run)

    monkeypatch.setattr(harbor.Trial, "create", create)
    measurement = asyncio.run(algo._measure(target.snapshot, "baseline"))
    assert measurement["score"] == 0.5
    assert measurement["per_task"] == {"pass": [1, 1], "mixed": [0, 0]}
    assert measurement["error_trials"] == 1
    assert measurement["cost"] is None
    assert measurement["cost_accounting"]["observed_tokens"] == 317
    assert measurement["cost_accounting"]["trials_partial"] == 1
    batches = saved(algo, "baseline")
    assert len(batches) == 2
    assert len(saved(algo, "measurement")) == 1
    for batch in batches:
        for row in batch["trajectories"]:
            messages = row["messages"]
            assert messages[0]["type"] == "RunConfig"
            assert messages[-1]["type"] == "HarborVerifier"
            timings = [m for m in messages if m.get("type") == "TaskExecutionTiming"]
            assert len(timings) == 1 and timings[0]["elapsed_seconds"] > 0
            assert Path(messages[-1]["artifacts"]["directory"]).is_dir()
    assert all(row["execution_seconds"] > 0 for row in measurement["evidence"])
    # Final evaluation uses the same rollout wrapper but the single-batch path.
    final = asyncio.run(algo._evaluate(target.snapshot, tasks, label="test"))
    assert final.score == 1
    assert len(saved(algo, "test")) == 1
    assert len(set(created)) == 6


def test_archive_failure_is_not_reported_as_success(execution, monkeypatch):
    agent, task = execution

    async def create(config):
        paths = TrialPaths(config.trials_dir / "trial")
        outputs(paths)
        return SimpleNamespace(
            paths=paths,
            run=AsyncMock(
                return_value=SimpleNamespace(
                    id="trial",
                    exception_info=None,
                    verifier_result=SimpleNamespace(rewards={"reward": 1}),
                )
            ),
        )

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(harbor.Trial, "create", create)
    monkeypatch.setattr(harbor, "_archive_trial", fail)
    result = asyncio.run(harbor._run(agent, task))
    assert result.status == "error"
    assert "archive failed" in result.error_message
    assert result.messages[-1]["artifact_error"] == "disk full"


@pytest.mark.parametrize("shared_cache", [False, True])
def test_prepared_dependencies_wire_both_image_and_verifier(
    execution, monkeypatch, tmp_path, shared_cache
):
    from test_prepared_verifier import SCRIPT

    from flashrsi.integrations._verifier_dependencies import dependency_spec

    agent, task = execution
    native = harbor.HarborTask(Path(task.env))
    native.config.verifier.environment_mode = harbor.VerifierEnvironmentMode.SHARED
    native.config.environment.docker_image = "original:1"
    native.paths.tests_dir.mkdir(parents=True)
    (native.paths.tests_dir / "test.sh").write_text(SCRIPT)
    spec = dependency_spec(SCRIPT)
    entry = {
        "base_image": "original:1",
        "image_id": "sha256:" + "b" * 64,
        "spec_sha256": spec["spec_sha256"],
        "offline_check_passed": True,
    }
    if shared_cache:
        import hashlib

        cache = tmp_path / "runtime"
        (cache / "venv").mkdir(parents=True)
        (cache / "venv/pyvenv.cfg").write_text("version = 3.13.9\n")
        (cache / "spec.sha256").write_text(spec["spec_sha256"] + "\n")
        lock = b"pytest==8.4.1\n"
        (cache / "requirements.lock").write_bytes(lock)
        entry["runtime_cache"] = {
            "path": str(cache),
            "spec_sha256": spec["spec_sha256"],
            "requirements_lock_sha256": hashlib.sha256(lock).hexdigest(),
        }
    manifest = tmp_path / "prepared.json"
    manifest.write_text(json.dumps({"schema": 1, "tasks": {task.task_id: entry}}))
    agent.config["harbor"]["verifier_dependencies_manifest"] = str(manifest)
    hooks = []

    async def create(config):
        expected = {"prepared_image": entry["image_id"]}
        if shared_cache:
            expected["verifier_runtime_cache"] = entry["runtime_cache"]
        assert config.environment.kwargs == expected
        assert (
            config.verifier.import_path
            == "flashrsi.integrations._prepared_verifier:PreparedVerifier"
        )
        assert config.verifier.kwargs == {"dependency_spec_sha256": spec["spec_sha256"]}
        paths = TrialPaths(config.trials_dir / "trial")
        outputs(paths)
        return SimpleNamespace(
            paths=paths,
            add_hook=lambda *args: hooks.append(args),
            run=AsyncMock(
                return_value=SimpleNamespace(
                    id="trial",
                    exception_info=None,
                    verifier_result=SimpleNamespace(rewards={"reward": 1}),
                )
            ),
        )

    monkeypatch.setattr(harbor.Trial, "create", create)
    result = asyncio.run(harbor._run(agent, task))
    assert result.status == "finished", result.error_message
    assert result.messages[-1]["prepared_verifier"] == entry
    assert len(hooks) == 1


def test_failed_agent_partial_usage_survives_harbor_archive(execution, monkeypatch):
    agent, task = execution
    usage = {
        "model_calls": [
            {
                "call_id": "reported",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                },
            }
        ],
        "accounting_complete": False,
        "accounting_source": "trace.jsonl",
    }

    async def create(config):
        paths = TrialPaths(config.trials_dir / "trial")
        outputs(paths)
        trajectory = paths.agent_dir / "flashrsi.json"
        row = json.loads(trajectory.read_text())
        row.update(status="error", error_message="MiniAgent timed out", usage=usage)
        trajectory.write_text(json.dumps(row))
        return SimpleNamespace(
            paths=paths,
            run=AsyncMock(
                return_value=SimpleNamespace(
                    id="trial",
                    verifier_result=SimpleNamespace(rewards={"reward": 0}),
                    exception_info=SimpleNamespace(
                        exception_type="NonZeroAgentExitCodeError",
                        exception_message="agent exited with 1",
                    ),
                )
            ),
        )

    monkeypatch.setattr(harbor.Trial, "create", create)
    result = asyncio.run(harbor._run(agent, task))
    assert result.error_message == "MiniAgent timed out"
    assert dict(result.usage) == usage
    archive = Path(result.messages[-1]["artifacts"]["directory"])
    assert json.loads((archive / "agent/flashrsi.json").read_text())["usage"] == usage


def test_archive_rejects_links_outside_trial(tmp_path):
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("private host data")
    try:
        (paths.artifacts_dir / "link").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires OS permission")
    with pytest.raises(RuntimeError, match="Refusing linked artifact"):
        harbor._archive_trial(
            SimpleNamespace(paths=paths), tmp_path / "saved", {"task_id": "task"}, []
        )
    archive = next((tmp_path / "saved").iterdir())
    assert not (archive / "artifacts/link").exists()
    assert "error" in json.loads((archive / "manifest.json").read_text())


@pytest.mark.parametrize("failed_phase", [None, "solve", "regrade"])
def test_swe_pro_keeps_both_phases(execution, tmp_path, monkeypatch, failed_phase):
    from flashrsi.integrations import swe_bench_pro as pro

    agent, task = execution
    agent.config["timeout"] = 30
    task = replace(task, metadata={"task_checksum": "checksum"})
    monkeypatch.setattr(pro, "HarborTask", harbor.HarborTask)
    monkeypatch.setattr(pro, "_stage", lambda *args: None)
    monkeypatch.setattr(pro, "_runtime", lambda _: (agent.config, []))
    monkeypatch.setattr(pro, "_native_tooling", lambda: ("fixture:ReplayAgent", "true"))
    phases = []
    patch_path = None

    async def create(config):
        nonlocal patch_path
        phase = "solve" if not phases else "regrade"
        phases.append(phase)
        paths = TrialPaths(config.trials_dir / "trial")
        outputs(paths)
        if phase == "solve":
            patch_path = paths.agent_dir / "model.patch"
            patch_path.write_bytes(b"--- before\n+++ after\n")
        else:
            (paths.agent_dir / "replay.json").write_text(
                json.dumps({"patch": str(patch_path), "apply_rc": 0})
            )

        async def run():
            if phase == failed_phase:
                raise RuntimeError(phase + " failed")
            return SimpleNamespace(
                id=phase,
                exception_info=None,
                verifier_result=SimpleNamespace(rewards={"reward": 1}),
            )

        return SimpleNamespace(paths=paths, run=run)

    monkeypatch.setattr(pro.Trial, "create", create)
    result = asyncio.run(pro._run(agent, task, {}))
    assert result.status == ("error" if failed_phase else "finished")
    assert not patch_path.exists()
    for phase in phases:
        archive = Path(result.messages[-1][phase + "_artifacts"]["directory"])
        manifest = json.loads((archive / "manifest.json").read_text())
        assert manifest["phase"] == phase
        assert manifest["snapshot_id"] == "snapshot"
        assert (archive / "verifier/native-result.json").is_file()
        if phase == "solve":
            assert (
                archive / "agent/model.patch"
            ).read_bytes() == b"--- before\n+++ after\n"
    assert len(list((tmp_path / "saved").iterdir())) == len(phases)


@pytest.mark.parametrize("explicit", [False, True])
def test_runner_places_archives_in_experiment(tmp_path, monkeypatch, explicit):
    from flashrsi import runner
    from flashrsi.benchmarks import SpreadsheetBench2
    from flashrsi.config import load_config

    config = load_config(
        overrides={
            "run": {"output_dir": str(tmp_path / "runs")},
            "agent": {"llm": {"model_name": "fixture"}},
            "algorithm": {"worker": {"llm": {"model_name": "fixture"}}},
        }
    )
    benchmark = SpreadsheetBench2({})
    benchmark._splits = {"test": ()}
    target_config = (
        {"harbor": {"artifacts_dir": str(tmp_path / "custom")}} if explicit else {}
    )
    algorithm = SimpleNamespace(
        config=config.algorithm,
        target=SimpleNamespace(config=target_config),
        worker=SimpleNamespace(component_rules={}),
        benchmark=benchmark,
        evolve=AsyncMock(return_value=SimpleNamespace(id="snapshot")),
        _evaluate=AsyncMock(return_value=()),
    )
    monkeypatch.setattr(runner, "_prepare", AsyncMock(return_value=algorithm))
    directory, _ = asyncio.run(runner._run(config))
    expected = tmp_path / "custom" if explicit else directory / "artifacts/harbor"
    assert Path(target_config["harbor"]["artifacts_dir"]) == expected
