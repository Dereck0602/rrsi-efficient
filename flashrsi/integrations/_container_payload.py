"""Stage an importable Agent and its pinned Snapshot for container execution."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import sys
from collections.abc import Mapping
from importlib.metadata import metadata, requires, version
from pathlib import Path

import yaml
from packaging.markers import default_environment
from packaging.requirements import Requirement

_ROOT = "/opt/flashrsi-lite"
_RUNTIME_CACHE = "/opt/flashrsi-runtime-cache"


def runtime_cache_metadata(directory):
    """Validate a published runtime export before exposing it read-only to tasks."""
    path = Path(directory).expanduser().resolve(strict=True)
    metadata = json.loads((path.parent / "manifest.json").read_text())
    requirements = (path / "requirements.txt").read_bytes()
    if (
        not metadata.get("offline_setup_passed")
        or not metadata.get("python_version", "").startswith("3.12.")
        or hashlib.sha256(requirements).hexdigest()
        != metadata.get("requirements_sha256")
        or not (path / "venv/pyvenv.cfg").is_file()
    ):
        raise ValueError("Shared Agent runtime is incomplete or has stale requirements")
    return {**metadata, "path": str(path)}


def _stage(directory, agent, config, prompt):
    # Copy the adapter code separately from evolvable files. Linux dependencies
    # are installed in the container; never copy a host virtual environment.
    shutil.copytree(
        Path(__file__).parents[1],
        directory / "package" / "flashrsi",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env"),
    )
    agent.snapshot.save(directory / "snapshot.json")
    cls = type(agent)
    runtime_config = _plain(config)
    source = runtime_config.get("source_dir")
    if source is None:
        module_file = getattr(sys.modules[cls.__module__], "__file__", None)
        if module_file is not None:
            source = Path(module_file).resolve().parent
    if source is not None:
        # Keep host provenance for relocating native resource references only;
        # the container loads the serialized Snapshot, never this host directory.
        runtime_config["source_dir"] = str(Path(source).expanduser().resolve())
    request = directory / "request.json"
    request.write_text(
        json.dumps(
            {
                "agent_class": f"{cls.__module__}:{cls.__qualname__}",
                "config_yaml": yaml.safe_dump(runtime_config),
                "snapshot_path": f"{_ROOT}/snapshot.json",
                "prompt": prompt,
            },
        )
    )
    request.chmod(0o600)
    (directory / "requirements.txt").write_text(
        "".join(f"{package}=={version(package)}\n" for package in _dependencies(agent))
    )


def _dependencies(agent):
    """Read built-in dependency names from installed wheel metadata."""
    packages = ["PyYAML", *(agent._container_dependencies or ())]
    extra = agent._container_extra
    if extra is not None:
        if extra not in metadata("flashrsi-lite").get_all("Provides-Extra", []):
            raise ValueError(f"Unknown flashrsi-lite container extra: {extra}")
        environment = {
            **default_environment(),
            "sys_platform": "linux",
            "platform_system": "Linux",
            "os_name": "posix",
            "python_version": "3.12",
            "python_full_version": "3.12.0",
        }
        for value in requires("flashrsi-lite") or ():
            requirement = Requirement(value)
            marker = requirement.marker
            if (
                marker is not None
                and marker.evaluate({**environment, "extra": extra})
                and not marker.evaluate({**environment, "extra": ""})
            ):
                packages.append(requirement.name)
    return dict.fromkeys(packages)


def _plain(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _setup_script(uv_installer_url="https://astral.sh/uv/0.11.4/install.sh"):
    """Install an independent harness Python without replacing task dependencies."""
    return f"""set -eu
if [ -x {_RUNTIME_CACHE}/venv/bin/python ] && cmp -s {_ROOT}/requirements.txt {_RUNTIME_CACHE}/requirements.txt; then
    {_RUNTIME_CACHE}/venv/bin/python -I -c 'import importlib.metadata as m, pathlib, sys; assert sys.version_info[:2] == (3, 12); rows = pathlib.Path("{_ROOT}/requirements.txt").read_text().splitlines(); assert all(m.version(p) == v for p, v in (row.split("==") for row in rows))'
    if [ "$(readlink -f {_ROOT}/venv || true)" != {_RUNTIME_CACHE}/venv ]; then
        rm -rf {_ROOT}/venv
        ln -s {_RUNTIME_CACHE}/venv {_ROOT}/venv
    fi
    echo 'Using offline prepared Agent runtime (matching pinned requirements)'
    exit 0
fi
case "$({_ROOT}/bin/uv --version 2>/dev/null || true)" in
    'uv 0.11.4'|'uv 0.11.4 ('*')') ;;
    *)
if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl
fi
if command -v curl >/dev/null 2>&1; then
    curl -LsSf {shlex.quote(uv_installer_url)} -o {_ROOT}/install-uv.sh
else
    wget -q {shlex.quote(uv_installer_url)} -O {_ROOT}/install-uv.sh
fi
UV_INSTALL_DIR={_ROOT}/bin UV_NO_MODIFY_PATH=1 sh {_ROOT}/install-uv.sh
    ;;
esac
{_ROOT}/bin/uv venv --python 3.12 --managed-python {_ROOT}/venv
{_ROOT}/bin/uv pip install --python {_ROOT}/venv/bin/python -r {_ROOT}/requirements.txt
"""
