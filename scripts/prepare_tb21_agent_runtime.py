"""Add an offline MiniAgent runtime to already prepared local verifier images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import uuid
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from prepare_tb21_verifiers import inspect_image, run, verify_image  # noqa: E402

from flashrsi.agents import MiniAgent  # noqa: E402
from flashrsi.integrations._container_payload import (  # noqa: E402
    _ROOT as AGENT_ROOT,
)
from flashrsi.integrations._container_payload import (  # noqa: E402
    _RUNTIME_CACHE,
    _dependencies,
    _setup_script,
)


def offline_check(image):
    setup = _setup_script()
    # Use the exact runtime setup path with networking disabled.
    command = (
        f"set -eu; mkdir -p {AGENT_ROOT}; "
        f"cp {_RUNTIME_CACHE}/requirements.txt {AGENT_ROOT}/requirements.txt; "
        f"bash -c {shlex.quote(setup)}; "
        f"{AGENT_ROOT}/venv/bin/python -I -c 'import openai,requests,yaml'"
    )
    run(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        "/bin/bash",
        image,
        "-c",
        command,
        timeout=120,
    )


def export_shared_runtime(entry, directory):
    """Publish an immutable export; never overwrite files used by active trials."""
    metadata = entry["agent_runtime"]
    output = directory / "shared" / metadata["requirements_sha256"]
    if output.exists():
        saved = json.loads((output / "manifest.json").read_text())
        if saved["source_image_id"] != entry["image_id"]:
            raise ValueError(
                "Shared runtime already exists from another image; choose a new export location"
            )
        return output / "flashrsi-runtime-cache"
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="runtime-export-", dir=output.parent) as temporary:
        staged = Path(temporary) / "export"
        staged.mkdir()
        name = "flashrsi-agent-export-" + uuid.uuid4().hex[:12]
        try:
            run("docker", "create", "--name", name, entry["image_id"], "true")
            run("docker", "cp", name + ":" + _RUNTIME_CACHE, str(staged))
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        published = {
            **metadata,
            "source_image_id": entry["image_id"],
            "path": str(output / "flashrsi-runtime-cache"),
        }
        (staged / "manifest.json").write_text(json.dumps(published, indent=2) + "\n")
        staged.rename(output)
    return output / "flashrsi-runtime-cache"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", nargs="+", required=True)
    parser.add_argument(
        "--export-shared",
        action="store_true",
        help="Export the first selected image's runtime for read-only local Docker mounts",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "runs/tb21-verifier-images/manifest.json",
    )
    args = parser.parse_args()
    manifest_path = args.manifest.resolve()
    manifest = json.loads(manifest_path.read_text())
    tasks = [
        name if name.startswith("terminal-bench/") else "terminal-bench/" + name
        for name in args.tasks
    ]
    if not set(tasks) <= manifest["tasks"].keys():
        raise ValueError("Prepare verifier images for these tasks first")
    if sys.version_info[:2] != (3, 12):
        raise ValueError("Run preparation with the experiment's Python 3.12")
    requirements = "".join(f"{p}=={version(p)}\n" for p in _dependencies(MiniAgent({})))
    fingerprint = hashlib.sha256(requirements.encode()).hexdigest()
    python_version = platform.python_version()
    directory = ROOT / "runs/tb21-agent-runtime-images"
    assets = directory / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    cache = directory / "download-cache"
    cache.mkdir(exist_ok=True)
    uv = shutil.which("uv")
    if not uv:
        raise ValueError("A host uv executable is required")
    uv_version = run(uv, "--version", capture=True).strip()
    run(
        uv,
        "python",
        "install",
        python_version,
        env={
            **os.environ,
            "UV_PYTHON_INSTALL_DIR": str(assets / "python"),
            "UV_PYTHON_BIN_DIR": str(assets / "bin"),
            "UV_CACHE_DIR": str(cache),
        },
    )
    python_name = next(
        (assets / "python").glob(f"cpython-{python_version}-linux-x86_64-gnu")
    ).name
    for task in tasks:
        entry = manifest["tasks"][task]
        if entry.get("agent_runtime", {}).get("requirements_sha256") == fingerprint:
            offline_check(entry["image_id"])
            print("Reused offline runtime:", task, flush=True)
            continue
        docker_root = run(
            "docker", "info", "--format", "{{.DockerRootDir}}", capture=True
        ).strip()
        if shutil.disk_usage(docker_root).free < 4 * 1024**3:
            raise RuntimeError(
                "Docker has less than 4 GiB free; no existing data was deleted"
            )
        working = directory / "builds" / task.split("/")[-1]
        working.mkdir(parents=True, exist_ok=True)
        (working / "requirements.txt").write_text(requirements)
        proxy = []
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            if value := os.environ.get(key):
                proxy.extend(
                    [
                        f"export {key}={shlex.quote(value)}",
                        f"export {key.lower()}={shlex.quote(value)}",
                    ]
                )
        proxy_path = working / "proxy.sh"
        proxy_path.write_text("\n".join(proxy) + "\n")
        proxy_path.chmod(0o600)
        script = f"""set -euo pipefail
source /prep/proxy.sh
mkdir -p {_RUNTIME_CACHE}/python
cp -a /assets/python/{python_name} {_RUNTIME_CACHE}/python/
/host-uv venv --python {_RUNTIME_CACHE}/python/{python_name}/bin/python3 {_RUNTIME_CACHE}/venv
/host-uv pip install --link-mode copy --cache-dir /download-cache --python {_RUNTIME_CACHE}/venv/bin/python -r /prep/requirements.txt
cp /prep/requirements.txt {_RUNTIME_CACHE}/requirements.txt
/host-uv pip freeze --python {_RUNTIME_CACHE}/venv/bin/python > {_RUNTIME_CACHE}/requirements.lock
"""
        (working / "prepare.sh").write_text(script)
        base = inspect_image(entry["image_id"])
        name = "flashrsi-agent-prep-" + uuid.uuid4().hex[:12]
        tag = f"flashrsi-tb21-agent/{task.split('/')[-1]}:{fingerprint[:12]}-{base['Id'][7:19]}"
        print("Preparing Agent runtime:", task, flush=True)
        try:
            run(
                "docker",
                "create",
                "--name",
                name,
                "--network",
                "host",
                "--user",
                "root",
                "--mount",
                f"type=bind,src={working},dst=/prep,readonly",
                "--mount",
                f"type=bind,src={assets},dst=/assets,readonly",
                "--mount",
                f"type=bind,src={cache},dst=/download-cache",
                "--mount",
                f"type=bind,src={uv},dst=/host-uv,readonly",
                "--entrypoint",
                "",
                base["Id"],
                "/bin/bash",
                "/prep/prepare.sh",
            )
            run("docker", "start", "--attach", name, timeout=1200)
            config = base["Config"]
            run(
                "docker",
                "commit",
                "--change",
                "ENTRYPOINT " + json.dumps(config.get("Entrypoint") or []),
                "--change",
                "CMD " + json.dumps(config.get("Cmd") or []),
                "--change",
                "USER " + (config.get("User") or "root"),
                name,
                tag,
            )
        finally:
            subprocess.run(
                ["docker", "rm", "-f", name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proxy_path.unlink(missing_ok=True)
        image = inspect_image(tag)["Id"]
        verify_image(image, entry)
        offline_check(image)
        entry.update(
            image_id=image,
            image_tag=tag,
            agent_runtime={
                "parent_image_id": base["Id"],
                "requirements_sha256": fingerprint,
                "requirements": requirements.splitlines(),
                "python_version": python_version,
                "preparation_uv_version": uv_version,
                "offline_setup_passed": True,
            },
        )
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(manifest_path)
        print("Offline Agent + verifier passed:", task, flush=True)
    if args.export_shared:
        print(
            "Shared Agent runtime:",
            export_shared_runtime(manifest["tasks"][tasks[0]], directory),
        )


if __name__ == "__main__":
    main()
