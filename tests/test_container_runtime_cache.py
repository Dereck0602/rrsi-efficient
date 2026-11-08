"""Execute the cached setup branch without a network or a Docker dependency."""

import hashlib
import json
import subprocess
import sys
from importlib.metadata import version

import pytest

from flashrsi.integrations import _container_payload as payload


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    if sys.version_info[:2] != (3, 12):
        pytest.skip("The cached runtime contract requires Python 3.12")
    root, cache = tmp_path / "payload", tmp_path / "cache"
    root.mkdir()
    (cache / "venv/bin").mkdir(parents=True)
    (cache / "venv/bin/python").symlink_to(sys.executable)
    requirements = f"PyYAML=={version('PyYAML')}\n"
    (root / "requirements.txt").write_text(requirements)
    (cache / "requirements.txt").write_text(requirements)
    monkeypatch.setattr(payload, "_ROOT", str(root))
    monkeypatch.setattr(payload, "_RUNTIME_CACHE", str(cache))
    commands = tmp_path / "commands"
    commands.mkdir()
    marker = tmp_path / "network-attempted"
    for name in ("curl", "wget"):
        script = commands / name
        script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 71\n")
        script.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", str(commands) + ":" + os.environ["PATH"])
    return root, cache, marker


def test_matching_cache_skips_download_and_can_be_set_up_twice(runtime):
    root, cache, marker = runtime
    for _ in range(2):
        result = subprocess.run(
            ["bash", "-c", payload._setup_script()], capture_output=True, text=True
        )
        assert result.returncode == 0, result.stderr
        assert "offline prepared Agent runtime" in result.stdout
        assert (root / "venv").resolve() == cache / "venv"
        assert not marker.exists()


def test_changed_requirements_do_not_silently_reuse_cache(runtime):
    root, _, marker = runtime
    (root / "requirements.txt").write_text("PyYAML==0.0.0\n")
    result = subprocess.run(
        ["bash", "-c", payload._setup_script()], capture_output=True
    )
    assert result.returncode == 71
    assert marker.exists()
    assert not (root / "venv").exists()


def test_broken_cache_fails_before_starting_an_agent(runtime):
    root, cache, marker = runtime
    for directory in (root, cache):
        (directory / "requirements.txt").write_text("PyYAML==0.0.0\n")
    result = subprocess.run(
        ["bash", "-c", payload._setup_script()], capture_output=True
    )
    assert result.returncode != 0
    assert not marker.exists()
    assert not (root / "venv").exists()


def test_shared_export_rejects_a_changed_requirements_file(tmp_path):
    directory = tmp_path / "cache"
    (directory / "venv").mkdir(parents=True)
    requirements = b"PyYAML==6.0.3\n"
    (directory / "requirements.txt").write_bytes(requirements)
    (directory / "venv/pyvenv.cfg").write_text("version = 3.12.14\n")
    metadata = {
        "python_version": "3.12.14",
        "offline_setup_passed": True,
        "requirements_sha256": hashlib.sha256(requirements).hexdigest(),
    }
    (tmp_path / "manifest.json").write_text(json.dumps(metadata))
    assert payload.runtime_cache_metadata(directory)["path"] == str(directory)
    (directory / "requirements.txt").write_text("changed\n")
    with pytest.raises(ValueError, match="stale requirements"):
        payload.runtime_cache_metadata(directory)


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("verifier_cached", [False, True])
def test_compose_mounts_only_the_runtime_read_only(
    tmp_path, monkeypatch, cached, verifier_cached
):
    pytest.importorskip("harbor")
    from harbor.environments.docker.docker import DockerEnvironment

    from flashrsi.integrations._harbor_remote import _RemoteDockerEnvironment

    path = tmp_path / "compose.json"
    monkeypatch.setattr(
        DockerEnvironment, "_write_mounts_compose_file", lambda self: path
    )
    monkeypatch.setattr(DockerEnvironment, "_uses_compose", False)
    environment = _RemoteDockerEnvironment.__new__(_RemoteDockerEnvironment)
    environment._is_windows_container = False
    environment._mounts = [
        {"type": "bind", "target": "/logs/agent", "source": "/host/logs"}
    ]
    environment._agent_runtime_cache = (
        {"path": "/published/runtime"} if cached else None
    )
    environment._verifier_runtime_cache = (
        {"path": "/published/verifier"} if verifier_cached else None
    )
    environment._write_mounts_compose_file()
    mounts = json.loads(path.read_text())["services"]["main"]["volumes"]
    expected = (
        [
            {
                "type": "bind",
                "source": "/published/runtime",
                "target": payload._RUNTIME_CACHE,
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        ]
        if cached
        else []
    )
    if verifier_cached:
        expected.append(
            {
                "type": "bind",
                "source": "/published/verifier",
                "target": "/opt/flashrsi-verifier",
                "read_only": True,
                "bind": {"create_host_path": False},
            }
        )
    assert mounts == expected
