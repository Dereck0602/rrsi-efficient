"""Native trial storage survives payload cleanup and keeps task/version identity."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("harbor")
from harbor.models.trial.paths import TrialPaths

from flashrsi.core import Task
from flashrsi.integrations import harbor


@pytest.mark.parametrize("persistent", [True, False])
def test_native_trial_directory_lifetime(tmp_path, monkeypatch, persistent):
    monkeypatch.chdir(tmp_path)
    payloads = []
    trials = []
    config = {"llm": {"model_name": "local-test"}}
    if persistent:
        config["harbor"] = {"trials_dir": "retained"}
    agent = SimpleNamespace(
        config=config,
        snapshot=SimpleNamespace(id="snapshot-one"),
        _check_container=lambda: None,
    )
    task = Task(task_id="one", question="input", env=str(tmp_path))
    native = SimpleNamespace(
        config=SimpleNamespace(
            steps=None,
            environment=SimpleNamespace(os=SimpleNamespace(value="linux")),
            agent=SimpleNamespace(timeout_sec=1),
            verifier=SimpleNamespace(
                environment_mode=harbor.VerifierEnvironmentMode.SEPARATE,
                environment=None,
            ),
        ),
        instruction=task.question,
        checksum="task-checksum",
        paths=SimpleNamespace(
            environment_dir=tmp_path / "environment", tests_dir=tmp_path / "tests"
        ),
    )
    monkeypatch.setattr(harbor, "HarborTask", lambda path: native)
    monkeypatch.setattr(harbor.shutil, "which", lambda command: "docker")
    monkeypatch.setattr(
        harbor, "_stage", lambda directory, *args: payloads.append(directory)
    )

    async def create(config):
        paths = TrialPaths(trial_dir=config.trials_dir / uuid4().hex)
        paths.mkdir()

        async def run():
            (paths.agent_dir / "flashrsi.json").write_text(
                json.dumps(
                    {
                        "messages": [
                            {"type": "RunConfig", "snapshot_id": "snapshot-one"}
                        ],
                    }
                )
            )
            (paths.artifacts_dir / "output.xlsx").write_bytes(b"workbook")
            (paths.verifier_dir / "native-result.json").write_text('{"reward": 0}')
            return SimpleNamespace(
                id=uuid4(),
                exception_info=None,
                verifier_result=SimpleNamespace(rewards={"reward": 0}),
            )

        trials.append(paths)
        return SimpleNamespace(paths=paths, run=run)

    monkeypatch.setattr(harbor.Trial, "create", create)
    results = [asyncio.run(harbor._run(agent, task)) for _ in range(2)]
    assert all(not path.exists() for path in payloads)
    assert trials[0].trial_dir != trials[1].trial_dir
    for trajectory, paths in zip(results, trials):
        event = trajectory.messages[-1]
        assert trajectory.status == "finished"
        assert event["task_checksum"] == "task-checksum"
        assert event["snapshot_id"] == "snapshot-one"
        assert paths.trial_dir.exists() is persistent
        archived = Path(event["artifacts"]["directory"])
        assert (archived / "artifacts/output.xlsx").read_bytes() == b"workbook"
        if persistent:
            assert Path(event["trial_directory"]).parent.samefile(tmp_path / "retained")
            assert (paths.artifacts_dir / "output.xlsx").read_bytes() == b"workbook"
            assert (
                event["files"][str(Path("artifacts/output.xlsx"))]
                == hashlib.sha256(b"workbook").hexdigest()
            )
            assert str(Path("verifier/native-result.json")) in event["files"]
        else:
            assert "trial_directory" not in event
