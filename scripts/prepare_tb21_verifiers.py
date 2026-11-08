"""Build additive, locally pinned TB2.1 verifier images and check them offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from flashrsi.integrations._verifier_dependencies import (  # noqa: E402
    PYTHON_VERSION,
    ROOT,
    UV_VERSION,
    dependency_spec,
)


def run(*args, capture=False, env=None, timeout=1800):
    return subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        env=env,
        timeout=timeout,
    ).stdout


def inspect_image(image):
    return json.loads(run("docker", "image", "inspect", image, capture=True))[0]


def compatibility_constraints(spec):
    # MTEB 1.36.8 imports transformer APIs removed in transformers 5. Its
    # unconstrained transitive requirements otherwise resolve to incompatible
    # current releases. Keep the task's declared packages unchanged.
    if "mteb==1.36.8" in spec["requirements"]:
        return ["transformers==4.55.0", "sentence-transformers==4.1.0"]
    return []


def docker_free_bytes(directory, *, include_reserved=False):
    if not include_reserved:
        return shutil.disk_usage(directory).free
    if os.geteuid() != 0:
        raise ValueError("Reserved filesystem space requires root")
    security = json.loads(
        run("docker", "info", "--format", "{{json .SecurityOptions}}", capture=True)
    )
    if any("rootless" in option for option in security):
        raise ValueError("Rootless Docker cannot use reserved filesystem space")
    stats = os.statvfs(directory)
    return stats.f_bfree * stats.f_frsize


def download(url, destination):
    temporary = destination.with_suffix(destination.suffix + ".partial")
    for attempt in range(3):
        try:
            with (
                urllib.request.urlopen(url, timeout=90) as response,
                temporary.open("wb") as out,
            ):
                shutil.copyfileobj(response, out)
            temporary.replace(destination)
            return
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2**attempt)


def assets(directory, python_version=PYTHON_VERSION):
    directory.mkdir(parents=True, exist_ok=True)
    uv = directory / "uv"
    if not uv.exists():
        metadata = directory / "uv-release.json"
        download(f"https://pypi.org/pypi/uv/{UV_VERSION}/json", metadata)
        wheel = next(
            item
            for item in json.loads(metadata.read_text())["urls"]
            if "manylinux" in item["filename"] and "x86_64" in item["filename"]
        )
        path = directory / wheel["filename"]
        download(wheel["url"], path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != wheel["digests"]["sha256"]:
            raise ValueError("uv wheel checksum mismatch")
        with zipfile.ZipFile(path) as archive:
            for name in ("uv", "uvx"):
                member = next(n for n in archive.namelist() if n.endswith("/" + name))
                target = directory / name
                target.write_bytes(archive.read(member))
                target.chmod(0o755)
    if not run(str(uv), "--version", capture=True).startswith(f"uv {UV_VERSION}"):
        raise ValueError("Unexpected uv version in preparation assets")
    env = {
        **os.environ,
        "UV_PYTHON_INSTALL_DIR": str(directory / "python"),
        "UV_PYTHON_BIN_DIR": str(directory / "bin"),
        "UV_CACHE_DIR": str(directory / "download-cache"),
    }
    python = directory / "python" / f"cpython-{python_version}-linux-x86_64-gnu"
    if not python.exists():
        # Keep the task's uv version for dependency resolution, but allow a
        # newer host uv to download Python releases absent from its catalogue.
        installer = shutil.which("uv") or str(uv)
        run(installer, "python", "install", python_version, env=env)
    actual = run(str(python / "bin/python3"), "--version", capture=True).strip()
    if actual != f"Python {python_version}":
        raise ValueError(f"Unexpected preparation interpreter: {actual}")
    return python.name


def verify_image(image, spec, runtime_cache=None):
    runtime_cache = runtime_cache or spec.get("runtime_cache")
    # Harbor supplies this command without overriding the image entrypoint.
    run(
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        image,
        "sh",
        "-c",
        "true",
        timeout=60,
    )
    system = spec.get("runner") == "system"
    python = (
        spec.get("system_python", "python3") if system else f"{ROOT}/venv/bin/python"
    )
    pytest = "pytest" if system else f"{ROOT}/venv/bin/pytest"
    check = f"""
import importlib, importlib.metadata as m, json, subprocess, sys, warnings
from packaging.requirements import Requirement
warnings.filterwarnings('ignore', category=SyntaxWarning)
requirements = {spec["requirements"] + spec.get("compatibility_constraints", [])!r}
expected_python = {None if system else spec["python_version"]!r}
assert expected_python is None or sys.version.split()[0] == expected_python
installed_apt = set()
inventory = subprocess.check_output(['dpkg-query', '-W', '-f=${{binary:Package}}\\t${{Status}}\\t${{Provides}}\\n'], text=True)
for row in inventory.splitlines():
    package, status, provides = row.split('\\t', 2)
    if status == 'install ok installed':
        installed_apt.add(package.split(':', 1)[0])
        installed_apt.update(p.strip().split(' ', 1)[0].split(':', 1)[0] for p in provides.split(',') if p.strip())
missing_apt = set({spec["apt_packages"]!r}) - installed_apt
assert not missing_apt, f'Missing apt dependencies: {{sorted(missing_apt)}}'
versions = {{r.split('==')[0]: m.version(r.split('==')[0]) for r in requirements}}
assert all(versions[r.split('==')[0]] in Requirement(r).specifier for r in requirements)
module_names = {{
    'beautifulsoup4': 'bs4', 'pillow': 'PIL.Image', 'scikit-image': 'skimage',
    'opencv-python': 'cv2', 'opencv-contrib-python': 'cv2',
    'biopython': 'Bio', 'gitpython': 'git',
}}
for name in versions:
    if name in module_names or name in {{'numpy','scipy','torch','torchvision','selenium','pandas','matplotlib','requests','psutil','shapely','timm','pytesseract','mailman','mteb','pyarrow','chess','datasets','tqdm','rdflib','transformers','mujoco','toml'}}:
        importlib.import_module(module_names.get(name, name))
if 'scipy' in versions:
    import scipy.linalg, scipy.stats
for source in {spec.get("vcs_requirements", [])!r}:
    metadata = json.loads(m.distribution(source['name']).read_text('direct_url.json'))
    assert metadata['vcs_info']['commit_id'] == source['commit']
    importlib.import_module(source['name'])
print(json.dumps(versions, sort_keys=True))
"""
    checks = [
        "set -eu",
        f'test "$(cat {ROOT}/spec.sha256)" = {shlex.quote(spec["spec_sha256"])}',
        f"{pytest} --help >/dev/null",
        f"{python} -c {shlex.quote(check)}",
    ]
    if spec.get("source_builds"):
        checks.insert(-1, "test -x /usr/local/bin/fasttext")
    for asset in spec.get("asset_downloads", []):
        checks.insert(-1, f"test -s {ROOT}/assets/{shlex.quote(asset['name'])}")
    mounts = []
    if runtime_cache:
        mounts = [
            "--mount",
            f"type=bind,src={runtime_cache['path']},dst={ROOT},readonly",
        ]
        checks.insert(
            -1, f"if touch {ROOT}/must-not-be-writable 2>/dev/null; then exit 1; fi"
        )
        for relative, digest in runtime_cache.get("files_sha256", {}).items():
            checks.insert(
                -1,
                f"printf '%s\\n' {shlex.quote(digest + '  ' + ROOT + '/' + relative)} | sha256sum -c - >/dev/null",
            )
    return json.loads(
        run(
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            *mounts,
            "--entrypoint",
            "/bin/bash",
            image,
            "-c",
            "; ".join(checks),
            capture=True,
            timeout=120,
        )
    )


def build(
    task_dir,
    spec,
    directory,
    python_name,
    *,
    shared_cache=False,
    min_docker_free_gib=8,
    include_reserved_space=False,
):
    native = tomllib.loads((task_dir / "task.toml").read_text())
    base = native["environment"]["docker_image"]
    original = inspect_image(base)
    docker_root = run(
        "docker", "info", "--format", "{{.DockerRootDir}}", capture=True
    ).strip()
    free = docker_free_bytes(docker_root, include_reserved=include_reserved_space)
    if free < min_docker_free_gib * 1024**3:
        raise RuntimeError(
            f"Docker has less than {min_docker_free_gib:g} GiB free; preparation stopped without deleting existing data"
        )
    working = directory / "builds" / task_dir.name
    working.mkdir(parents=True, exist_ok=True)
    (working / "tmp").mkdir(exist_ok=True)
    (working / "tmp").chmod(0o1777)
    (working / "source-builds").mkdir(exist_ok=True)
    (directory / "apt-cache").mkdir(exist_ok=True)
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
    system = spec.get("runner") == "system"
    constraints = compatibility_constraints(spec)
    (working / "constraints.txt").write_text("\n".join(constraints) + "\n")
    constraint_args = ["--constraint", "/prep/constraints.txt"] if constraints else []
    runtime_cache = None
    if shared_cache and not system:
        caches = directory / "runtimes"
        caches.mkdir(exist_ok=True)
        runtime_cache = {
            "path": tempfile.mkdtemp(prefix=task_dir.name + "-", dir=caches)
        }
    # Only install absent system packages, avoiding unnecessary upgrades to
    # libraries already supplied by the pinned task image.
    script = f"""#!/bin/bash
set -euo pipefail
source /prep/proxy.sh
export DEBIAN_FRONTEND=noninteractive
export TMPDIR=/tmp XDG_CACHE_HOME=/download-cache/xdg PIP_CACHE_DIR=/download-cache/pip
export HF_HOME=/download-cache/huggingface TORCH_HOME=/download-cache/torch
source /etc/os-release
apt_cache="/apt-cache/${{ID}}-${{VERSION_ID}}"
mkdir -p "$apt_cache/lists/partial" "$apt_cache/archives/partial"
apt_cache_options=(-o "Dir::State::Lists=$apt_cache/lists" -o "Dir::Cache::Archives=$apt_cache/archives")
missing_packages=()
for package in {shlex.join(spec["apt_packages"])} ca-certificates; do
    if [[ "$(dpkg-query -W -f='${{Status}}' "$package" 2>/dev/null || true)" != 'install ok installed' ]]; then
        missing_packages+=("$package")
    fi
done
if (( ${{#missing_packages[@]}} )); then
apt_config=$(mktemp -d)
chmod 755 "$apt_config"
cp -a /etc/apt/. "$apt_config/"
source /etc/os-release
if [[ "${{ID:-}}" == debian && "${{VERSION_CODENAME:-}}" == bullseye ]]; then
    echo 'Preparation: using the official Debian archive for bullseye-security packages'
    find "$apt_config" -type f \\( -name '*.list' -o -name '*.sources' \\) -exec sed -i -E 's@https?://(deb.debian.org|security.debian.org)/debian-security@https://archive.debian.org/debian-security@g' {{}} +
fi
apt-get "${{apt_cache_options[@]}}" -o Dir::Etc="$apt_config" -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o APT::Update::Error-Mode=any update
if ! apt-get "${{apt_cache_options[@]}}" -o Dir::Etc="$apt_config" -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends "${{missing_packages[@]}}"; then
    echo 'Preparation: retrying official repositories over HTTPS after installation failure'
    find "$apt_config" -type f \\( -name '*.list' -o -name '*.sources' \\) -exec sed -i -E 's@http://(deb.debian.org|archive.ubuntu.com|security.ubuntu.com)/@https://\\1/@g' {{}} +
    apt-get "${{apt_cache_options[@]}}" -o Dir::Etc="$apt_config" -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o APT::Update::Error-Mode=any update
    apt-get "${{apt_cache_options[@]}}" -o Dir::Etc="$apt_config" -o Acquire::Retries=3 -o Acquire::http::Timeout=30 install -y --no-install-recommends "${{missing_packages[@]}}"
fi
rm -rf "$apt_config"
fi
"""
    if system:
        script += f"""
mkdir -p {ROOT}
pip install --cache-dir /download-cache/pip {shlex.join(constraint_args + spec.get("pip_options", []) + spec["requirements"])}
pip freeze > {ROOT}/requirements.lock
"""
    else:
        script += f"""
mkdir -p {ROOT}/bin {ROOT}/python
cp /assets/uv /assets/uvx {ROOT}/bin/
cp -a /assets/python/{python_name} {ROOT}/python/
{ROOT}/bin/uv venv --python {ROOT}/python/{python_name}/bin/python3 {ROOT}/venv
{ROOT}/bin/uv pip install --link-mode copy --cache-dir /download-cache --python {ROOT}/venv/bin/python {shlex.join(constraint_args + spec.get("uv_options", []) + spec["requirements"] + [v["url"] for v in spec.get("vcs_requirements", [])])}
{ROOT}/bin/uv pip freeze --python {ROOT}/venv/bin/python > {ROOT}/requirements.lock
"""
    for source in spec.get("source_builds", []):
        script += f"""
if [[ ! -d /source-builds/fastText/.git ]]; then
    git clone {shlex.quote(source["url"])} /source-builds/fastText
fi
make -C /source-builds/fastText -j2
cp /source-builds/fastText/fasttext /usr/local/bin/fasttext
git -C /source-builds/fastText rev-parse HEAD > {ROOT}/fasttext.commit
"""
    for asset in spec.get("asset_downloads", []):
        path = directory / "assets" / "models" / asset["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            download(asset["url"], path)
        script += f"mkdir -p {ROOT}/assets; cp /assets/models/{shlex.quote(asset['name'])} {ROOT}/assets/{shlex.quote(asset['name'])}\n"
    script += f"""
printf '%s\\n' {shlex.quote(spec["spec_sha256"])} > {ROOT}/spec.sha256
apt-get clean
rm -rf /var/lib/apt/lists/*
"""
    (working / "prepare.sh").write_text(script)
    cache = directory / "download-cache"
    cache.mkdir(exist_ok=True)
    name = "flashrsi-verifier-prep-" + uuid.uuid4().hex[:12]
    tag = f"flashrsi-tb21-verifier/{task_dir.name}:{spec['spec_sha256'][:12]}-{original['Id'][7:19]}"
    mounts = (
        ["--mount", f"type=bind,src={runtime_cache['path']},dst={ROOT}"]
        if runtime_cache
        else []
    )
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
            f"type=bind,src={directory / 'assets'},dst=/assets,readonly",
            "--mount",
            f"type=bind,src={cache},dst=/download-cache",
            "--mount",
            f"type=bind,src={working / 'tmp'},dst=/tmp",
            "--mount",
            f"type=bind,src={working / 'source-builds'},dst=/source-builds",
            "--mount",
            f"type=bind,src={directory / 'apt-cache'},dst=/apt-cache",
            *mounts,
            "--entrypoint",
            "",
            original["Id"],
            "/bin/bash",
            "/prep/prepare.sh",
        )
        run("docker", "start", "--attach", name, timeout=3600)
        state = json.loads(run("docker", "inspect", name, capture=True))[0]["State"]
        if state["ExitCode"] != 0:
            raise RuntimeError(
                f"Preparation failed for {task_dir.name}: exit {state['ExitCode']}"
            )
        config = original["Config"]
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
    image = inspect_image(tag)
    if runtime_cache and spec.get("asset_downloads"):
        runtime_cache["files_sha256"] = {
            "assets/" + asset["name"]: hashlib.sha256(
                (directory / "assets/models" / asset["name"]).read_bytes()
            ).hexdigest()
            for asset in spec["asset_downloads"]
        }
    checked_spec = {**spec, "compatibility_constraints": constraints} if constraints else spec
    versions = verify_image(image["Id"], checked_spec, runtime_cache)
    result = {
        **{k: v for k, v in spec.items() if k != "prepared_script"},
        "task_id": native["task"]["name"],
        "base_image": base,
        "base_image_id": original["Id"],
        "image_tag": tag,
        "image_id": image["Id"],
        "offline_check_passed": True,
        "installed_versions": versions,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    if constraints:
        result["compatibility_constraints"] = constraints
    if runtime_cache:
        result["runtime_cache"] = {
            **runtime_cache,
            "spec_sha256": spec["spec_sha256"],
            "requirements_lock_sha256": hashlib.sha256(
                (Path(runtime_cache["path"]) / "requirements.lock").read_bytes()
            ).hexdigest(),
        }
    if spec.get("asset_downloads"):
        result["prepared_asset_sha256"] = {
            asset["name"]: hashlib.sha256(
                (directory / "assets/models" / asset["name"]).read_bytes()
            ).hexdigest()
            for asset in spec["asset_downloads"]
        }
    if spec.get("source_builds") and runtime_cache:
        result["source_commits"] = {
            "fasttext": (Path(runtime_cache["path"]) / "fasttext.commit")
            .read_text()
            .strip()
        }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "build"), default="plan", nargs="?")
    parser.add_argument(
        "--data-path",
        type=Path,
        default=REPO.parent / "terminal-bench-2.1/dataset/tasks",
    )
    parser.add_argument("--failure-report", type=Path)
    parser.add_argument("--tasks", nargs="+")
    parser.add_argument(
        "--evolve-config",
        type=Path,
        help="Prepare exactly the real train split from this experiment YAML",
    )
    parser.add_argument(
        "--heldout-config",
        type=Path,
        help="Prepare exactly the real test split from this experiment YAML",
    )
    parser.add_argument(
        "--shared-cache",
        action="store_true",
        help="Store uv verifier runtimes on the output filesystem and mount read-only",
    )
    parser.add_argument(
        "--min-docker-free-gib",
        type=float,
        default=8,
        help="Minimum free Docker space before each build (default: 8 GiB)",
    )
    parser.add_argument(
        "--include-reserved-space",
        action="store_true",
        help="Explicitly count reserved filesystem blocks accessible to root Docker",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=REPO / "runs/tb21-verifier-images"
    )
    args = parser.parse_args()
    if not args.min_docker_free_gib >= 1:
        parser.error("--min-docker-free-gib must be at least 1")
    names = set(args.tasks or [])
    if args.evolve_config and args.heldout_config:
        parser.error("Choose either --evolve-config or --heldout-config")
    if args.evolve_config or args.heldout_config:
        from flashrsi._launch import _load
        from flashrsi.benchmarks.terminal_bench import TerminalBench

        config = _load(args.evolve_config or args.heldout_config)
        args.data_path = Path(config.benchmark.data_path)
        names.update(
            Path(task.env).name
            for task in TerminalBench(config.benchmark).load()[
                "train" if args.evolve_config else "test"
            ]
        )
    if args.failure_report:
        names.update(
            row["task"].split("/")[-1]
            for row in json.loads(args.failure_report.read_text())[
                "verifier_dependency_failures"
            ]
        )
    if not names:
        parser.error("Specify --evolve-config, --heldout-config, --failure-report or --tasks")
    tasks = []
    for name in sorted(names):
        if not re_full_task_name(name):
            raise ValueError("Expected a task directory name")
        directory = args.data_path / name
        spec = dependency_spec((directory / "tests/test.sh").read_text())
        tasks.append((directory, spec))
        print(
            json.dumps(
                {
                    "task": name,
                    "apt": spec["apt_packages"],
                    "python": spec["python_version"],
                    "requirements": spec["requirements"],
                }
            ),
            flush=True,
        )
    if args.mode == "plan":
        return
    directory = args.output_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text())
        if manifest_path.exists()
        else {"schema": 1, "tasks": {}}
    )
    (directory / "tmp").mkdir(exist_ok=True)
    os.environ["TMPDIR"] = str(directory / "tmp")
    python_names = {}
    for index, (task, spec) in enumerate(tasks, 1):
        task_id = tomllib.loads((task / "task.toml").read_text())["task"]["name"]
        existing = manifest["tasks"].get(task_id)
        if existing and existing["spec_sha256"] == spec["spec_sha256"]:
            checked_spec = {
                **spec,
                "compatibility_constraints": existing.get("compatibility_constraints", []),
            }
            verify_image(existing["image_id"], checked_spec, existing.get("runtime_cache"))
            print(
                f"[{index}/{len(tasks)}] reused offline-verified image: {task.name}",
                flush=True,
            )
            continue
        print(f"[{index}/{len(tasks)}] preparing {task.name}", flush=True)
        python_version = (
            spec["python_version"]
            if spec["python_version"] != "system"
            else PYTHON_VERSION
        )
        if python_version not in python_names:
            python_names[python_version] = assets(directory / "assets", python_version)
        manifest["tasks"][task_id] = build(
            task,
            spec,
            directory,
            python_names[python_version],
            shared_cache=args.shared_cache,
            min_docker_free_gib=args.min_docker_free_gib,
            include_reserved_space=args.include_reserved_space,
        )
        temporary = manifest_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(manifest, indent=2) + "\n")
        temporary.replace(manifest_path)
        print(f"[{index}/{len(tasks)}] offline check passed: {task.name}", flush=True)
    print(f"Manifest: {manifest_path}", flush=True)


def re_full_task_name(name):
    import re

    return re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name)


if __name__ == "__main__":
    main()
