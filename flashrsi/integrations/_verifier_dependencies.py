"""Prepare the pinned TB2.1 verifier dependencies without copying task tests."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from pathlib import Path

ROOT = "/opt/flashrsi-verifier"
PYTHON_VERSION = "3.13.9"
UV_VERSION = "0.9.5"


def dependency_spec(script: str) -> dict:
    """Accept only the known installer prefix; keep all test/reward logic intact."""
    header = re.match(r"(?:#[^\n]*\n)*?#!/bin/bash\n", script)
    if header is None:
        raise ValueError("Unsupported verifier shell")
    if "uv venv -p 3.12 .tb\n" in script:
        return _mailman_spec(script)
    if "uvx \\\n" not in script:
        return _system_pip_spec(script)
    start = script.index("uvx \\\n")
    match = re.search(r"(?m)^  pytest(?= )", script[start:])
    if match is None:
        raise ValueError("Unsupported verifier invocation")
    end = start + match.end()
    args = shlex.split(script[start:end].replace("\\\n", " "))
    options = []
    if args[1:5] == [
        "--index",
        "https://download.pytorch.org/whl/cpu",
        "--index-strategy",
        "unsafe-best-match",
    ]:
        options = args[1:5]
        args = [args[0], *args[5:]]
    if (
        args[:2] != ["uvx", "-p"]
        or args[2] not in {"3.13", "3.11"}
        or args[-1] != "pytest"
        or (len(args) - 4) % 2
    ):
        raise ValueError("Unsupported verifier interpreter or command")
    python_version = {"3.13": PYTHON_VERSION, "3.11": "3.11.14"}[args[2]]
    requirements = []
    vcs = []
    for index in range(3, len(args) - 1, 2):
        requirement = args[index + 1]
        if args[index] != "-w":
            raise ValueError("Verifier requirements must be pinned -w arguments")
        if re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", requirement):
            requirements.append(requirement)
        elif (
            requirement
            == "git+https://github.com/ChaoningZhang/MobileSAM.git@${COMMIT_HASH}"
        ):
            commit = re.search(r"(?m)^COMMIT_HASH='([0-9a-f]{40})'$", script)
            if commit is None:
                raise ValueError("Verifier git dependency requires a full commit")
            vcs.append(
                {
                    "name": "mobile_sam",
                    "commit": commit[1],
                    "url": requirement.replace("${COMMIT_HASH}", commit[1]),
                }
            )
        else:
            raise ValueError("Verifier requirements must be pinned -w arguments")
    prefix = script[:start]
    prefix, packages = _remove_apt(prefix, required="apt-get" in prefix)
    for line in (
        f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | sh\n",
        "source $HOME/.local/bin/env\n",
    ):
        count = prefix.count(line)
        if count > 1:
            raise ValueError("Verifier installer differs from the supported template")
        if count:
            prefix = prefix.replace(line, "", 1)
    if "https://astral.sh/uv/" in prefix or "source $HOME/.local/bin/env" in prefix:
        raise ValueError("Verifier installer differs from the supported template")
    extra = {}
    source = "git clone https://github.com/facebookresearch/fastText.git /opt/fastText\ncd /opt/fastText && make && cp fasttext /usr/local/bin/ && cd /app\n"
    if source in prefix:
        prefix = prefix.replace(source, "", 1)
        extra["source_builds"] = [
            {
                "kind": "fasttext",
                "url": "https://github.com/facebookresearch/fastText.git",
            }
        ]
    weight = "curl -L -o mobile_sam.pt https://github.com/ChaoningZhang/MobileSAM/raw/master/weights/mobile_sam.pt\n"
    if weight in prefix:
        prefix = prefix.replace(
            weight, f"cp {ROOT}/assets/mobile_sam.pt mobile_sam.pt\n", 1
        )
        extra["asset_downloads"] = [
            {
                "name": "mobile_sam.pt",
                "url": "https://github.com/ChaoningZhang/MobileSAM/raw/master/weights/mobile_sam.pt",
            }
        ]
    if options:
        extra["uv_options"] = options
    if vcs:
        extra["vcs_requirements"] = vcs
    prepared = (
        prefix.replace(
            "#!/bin/bash\n", f"#!/bin/bash\nexport PATH={ROOT}/venv/bin:$PATH\n", 1
        )
        + f"{ROOT}/venv/bin/pytest"
        + script[end:]
    )
    spec = {
        "schema": 1,
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "prepared_script_sha256": hashlib.sha256(prepared.encode()).hexdigest(),
        "python_version": python_version,
        "uv_version": UV_VERSION,
        "apt_packages": packages,
        "requirements": requirements,
        **extra,
    }
    spec["spec_sha256"] = hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode()
    ).hexdigest()
    return {**spec, "prepared_script": prepared}


def _remove_apt(prefix, *, required):
    pattern = r"(?m)^(?:(?:apt-get update && DEBIAN_FRONTEND=noninteractive |apt-get update -qq && ))?apt-get install -y ([a-z0-9+ .-]+)\n"
    matches = list(re.finditer(pattern, prefix))
    if (required and not matches) or len(matches) > 2:
        raise ValueError("Unsupported verifier apt installation")
    if not matches:
        if "apt-get" in prefix:
            raise ValueError("Unsupported verifier apt installation")
        return prefix, []
    packages = list(dict.fromkeys(p for m in matches for p in m[1].split()))
    if not packages or any(
        not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*", p) for p in packages
    ):
        raise ValueError("Unsupported apt package name")
    standalone = sum(not m[0].startswith("apt-get update") for m in matches)
    for match in reversed(matches):
        prefix = prefix[: match.start()] + prefix[match.end() :]
    if standalone:
        if prefix.count("apt-get update\n") != standalone:
            raise ValueError("Unsupported verifier apt update")
        prefix = prefix.replace("apt-get update\n", "")
    if "apt-get" in prefix:
        raise ValueError("Unsupported extra verifier apt installation")
    return prefix, packages


def _system_pip_spec(script):
    """Keep native pip verifiers on their original system Python and PATH."""
    match = re.search(r"(?m)^pip install ([^\n]+)\n", script)
    if match is None or len(re.findall(r"(?m)^pip install ", script)) != 1:
        raise ValueError("Unsupported verifier pip installation")
    requirements = shlex.split(match[1])
    options = []
    if requirements[-1:] == ["--break-system-packages"]:
        options = [requirements.pop()]
    if not requirements or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", r) for r in requirements
    ):
        raise ValueError("Verifier requirements must be pinned pip arguments")
    suffix = script[match.end() :]
    if not re.search(r"(?m)^(?:python -m )?pytest --ctrf ", suffix):
        raise ValueError("Unsupported native pytest invocation")
    prefix, packages = _remove_apt(script[: match.start()], required=False)
    prepared = prefix + suffix
    spec = {
        "schema": 1,
        "runner": "system",
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "prepared_script_sha256": hashlib.sha256(prepared.encode()).hexdigest(),
        "python_version": "system",
        "uv_version": None,
        "apt_packages": packages,
        "requirements": requirements,
    }
    if options:
        spec["pip_options"] = options
    if re.search(r"(?m)^python -m pytest --ctrf ", suffix):
        spec["system_python"] = "python"
    spec["spec_sha256"] = hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode()
    ).hexdigest()
    return {**spec, "prepared_script": prepared}


def _mailman_spec(script):
    start = script.index("uv venv -p 3.12 .tb\n")
    match = re.search(r"(?m)^uv pip install ([^\n]+)\n", script[start:])
    if match is None:
        raise ValueError("Unsupported uv venv verifier")
    requirements = shlex.split(match[1])
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+", r) for r in requirements
    ):
        raise ValueError("Verifier requirements must be pinned")
    suffix = script[start + match.end() :]
    if suffix.count("uv run pytest --ctrf ") != 1:
        raise ValueError("Unsupported uv venv pytest command")
    prefix, packages = _remove_apt(script[:start], required=True)
    for line in [
        f"curl -LsSf https://astral.sh/uv/{UV_VERSION}/install.sh | sh\n",
        "source $HOME/.local/bin/env\n",
    ]:
        if prefix.count(line) != 1:
            raise ValueError("Unsupported uv venv installer")
        prefix = prefix.replace(line, "", 1)
    prepared = prefix.replace(
        "#!/bin/bash\n", f"#!/bin/bash\nexport PATH={ROOT}/venv/bin:$PATH\n", 1
    ) + suffix.replace("uv run pytest", f"{ROOT}/venv/bin/pytest", 1)
    spec = {
        "schema": 1,
        "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        "prepared_script_sha256": hashlib.sha256(prepared.encode()).hexdigest(),
        "python_version": "3.12.14",
        "uv_version": UV_VERSION,
        "apt_packages": packages,
        "requirements": requirements,
    }
    spec["spec_sha256"] = hashlib.sha256(
        json.dumps(spec, sort_keys=True).encode()
    ).hexdigest()
    return {**spec, "prepared_script": prepared}


def verifier_runtime_metadata(metadata):
    """Check a published host cache before mounting it into a local container."""
    path = Path(metadata["path"])
    if (
        not path.is_absolute()
        or (path / "spec.sha256").read_text().strip() != metadata["spec_sha256"]
        or hashlib.sha256((path / "requirements.lock").read_bytes()).hexdigest()
        != metadata["requirements_lock_sha256"]
        or not (path / "venv/pyvenv.cfg").is_file()
    ):
        raise ValueError("Stale prepared verifier runtime cache")
    for relative, digest in metadata.get("files_sha256", {}).items():
        candidate = path / relative
        if (
            not candidate.resolve().is_relative_to(path.resolve())
            or hashlib.sha256(candidate.read_bytes()).hexdigest() != digest
        ):
            raise ValueError("Stale prepared verifier runtime asset")
    return {**metadata, "path": str(path.resolve())}


def load_manifest(path):
    manifest = json.loads(Path(path).read_text())
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported verifier dependency manifest")
    if not isinstance(manifest.get("tasks"), dict):
        raise ValueError("Verifier dependency manifest requires a task mapping")
    return manifest


def prepared_entry(manifest_path, task_id, native):
    manifest = (
        manifest_path
        if isinstance(manifest_path, Mapping)
        else load_manifest(manifest_path)
    )
    entry = manifest["tasks"].get(task_id)
    if entry is None:
        return None
    spec = dependency_spec((native.paths.tests_dir / "test.sh").read_text())
    if (
        entry.get("spec_sha256") != spec["spec_sha256"]
        or entry.get("base_image") != native.config.environment.docker_image
        or not entry.get("offline_check_passed")
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", entry.get("image_id", ""))
    ):
        raise ValueError(f"Stale or unverified dependency image for {task_id}")
    if cache := entry.get("runtime_cache"):
        if cache.get("spec_sha256") != spec["spec_sha256"]:
            raise ValueError(f"Stale dependency runtime for {task_id}")
        verifier_runtime_metadata(cache)
    return entry
