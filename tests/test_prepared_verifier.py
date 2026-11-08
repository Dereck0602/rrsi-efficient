"""Cached dependency setup preserves the native tests and the real grader."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from flashrsi.integrations._verifier_dependencies import (
    dependency_spec,
    prepared_entry,
    verifier_runtime_metadata,
)

SCRIPT = """#!/bin/bash
# public dependency bootstrap
apt-get update
apt-get install -y curl expect
curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh
source $HOME/.local/bin/env
cp /tests/helper.py /app/helper.py
uvx \\
  -p 3.13 \\
  -w pytest==8.4.1 \\
  -w pytest-json-ctrf==0.3.5 \\
  pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
"""


def test_only_installer_and_uvx_launch_are_replaced():
    spec = dependency_spec(SCRIPT)
    prepared = spec["prepared_script"]
    assert spec["apt_packages"] == ["curl", "expect"]
    assert spec["requirements"] == ["pytest==8.4.1", "pytest-json-ctrf==0.3.5"]
    assert "cp /tests/helper.py /app/helper.py\n" in prepared
    suffix = SCRIPT.split("  pytest", 1)[1]
    assert prepared.endswith("/venv/bin/pytest" + suffix)
    assert "apt-get" not in prepared and "curl -LsSf" not in prepared
    assert "source $HOME" not in prepared


def test_combined_apt_and_comment_before_shebang_preserve_test_suffix():
    script = "# Canary comment\n" + SCRIPT.replace(
        "apt-get update\napt-get install -y curl expect\n",
        "apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y expect curl\n",
    )
    spec = dependency_spec(script)
    assert spec["apt_packages"] == ["expect", "curl"]
    assert spec["prepared_script"].startswith("# Canary comment\n#!/bin/bash\n")
    assert spec["prepared_script"].endswith(SCRIPT.split("  pytest", 1)[1])
    assert "apt-get" not in spec["prepared_script"]


def test_uvx_without_installer_uses_same_pinned_runner():
    script = SCRIPT
    for line in [
        "apt-get update\n",
        "apt-get install -y curl expect\n",
        "curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh\n",
        "source $HOME/.local/bin/env\n",
    ]:
        script = script.replace(line, "")
    spec = dependency_spec(script)
    assert spec["apt_packages"] == []
    assert spec["prepared_script"].endswith(SCRIPT.split("  pytest", 1)[1])


def test_extra_system_packages_and_cpu_index_do_not_change_test_suffix():
    script = SCRIPT.replace(
        "uvx \\\n",
        "apt-get update -qq && apt-get install -y mtools socat\nuvx \\\n  --index https://download.pytorch.org/whl/cpu \\\n  --index-strategy unsafe-best-match \\\n",
        1,
    )
    spec = dependency_spec(script)
    assert spec["apt_packages"] == ["curl", "expect", "mtools", "socat"]
    assert spec["uv_options"] == [
        "--index",
        "https://download.pytorch.org/whl/cpu",
        "--index-strategy",
        "unsafe-best-match",
    ]
    assert "apt-get" not in spec["prepared_script"]
    assert spec["prepared_script"].endswith(SCRIPT.split("  pytest", 1)[1])


def test_pinned_git_dependency_and_weight_copy_preserve_grader_data_staging():
    commit = "a" * 40
    prefix = f"COMMIT_HASH='{commit}'\ncurl -L -o mobile_sam.pt https://github.com/ChaoningZhang/MobileSAM/raw/master/weights/mobile_sam.pt\ncp /tests/correct_output.csv .\n"
    script = (
        SCRIPT.replace("uvx \\\n", prefix + "uvx \\\n", 1)
        .replace("-p 3.13", "-p 3.11")
        .replace(
            "  pytest --ctrf",
            "  -w git+https://github.com/ChaoningZhang/MobileSAM.git@${COMMIT_HASH} \\\n  pytest --ctrf",
            1,
        )
    )
    spec = dependency_spec(script)
    assert spec["python_version"] == "3.11.14"
    assert spec["vcs_requirements"][0]["commit"] == commit
    assert "${COMMIT_HASH}" not in spec["vcs_requirements"][0]["url"]
    assert "cp /tests/correct_output.csv .\n" in spec["prepared_script"]
    assert (
        "cp /opt/flashrsi-verifier/assets/mobile_sam.pt mobile_sam.pt"
        in spec["prepared_script"]
    )
    assert spec["prepared_script"].endswith(SCRIPT.split("  pytest", 1)[1])


def test_source_build_is_prepared_without_unpacking_private_tests():
    setup = "git clone https://github.com/facebookresearch/fastText.git /opt/fastText\ncd /opt/fastText && make && cp fasttext /usr/local/bin/ && cd /app\n"
    script = SCRIPT.replace(
        "uvx \\\n", setup + "tar -xzf /tests/private_test.tar.gz -C /tests\nuvx \\\n", 1
    ).replace("-p 3.13", "-p 3.11")
    spec = dependency_spec(script)
    assert spec["source_builds"][0]["kind"] == "fasttext"
    assert "git clone" not in spec["prepared_script"]
    assert "tar -xzf /tests/private_test.tar.gz -C /tests" in spec["prepared_script"]


def test_native_python_module_pytest_and_pip_flag_are_retained():
    script = "#!/bin/bash\npip install pytest==8.4.1 --break-system-packages\npython -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\nORIGINAL_EXIT_CODE=$?\n"
    spec = dependency_spec(script)
    assert spec["pip_options"] == ["--break-system-packages"]
    assert spec["system_python"] == "python"
    assert (
        spec["prepared_script"]
        == "#!/bin/bash\npython -m pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\nORIGINAL_EXIT_CODE=$?\n"
    )


def test_explicit_uv_venv_uses_its_declared_python_version():
    script = (
        SCRIPT[: SCRIPT.index("cp /tests/helper.py")]
        + "uv venv -p 3.12 .tb\nsource .tb/bin/activate\nuv pip install pytest==8.4.1 mailman==3.3.8\nuv run pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\n"
        + SCRIPT[SCRIPT.index("if [ $? -eq 0 ]; then") :]
    )
    spec = dependency_spec(script)
    assert spec["python_version"] == "3.12.14"
    assert spec["requirements"] == ["pytest==8.4.1", "mailman==3.3.8"]
    assert "uv venv" not in spec["prepared_script"]
    assert spec["prepared_script"].endswith(
        "/opt/flashrsi-verifier/venv/bin/pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\n"
        + SCRIPT[SCRIPT.index("if [ $? -eq 0 ]; then") :]
    )


@pytest.mark.parametrize("apt", ["", "apt-get update\napt-get install -y vim\n"])
def test_native_pip_retains_system_python_and_all_test_logic(apt):
    prefix = "#!/bin/bash\n" + apt + 'test "$PWD" != / || exit 1\n'
    suffix = (
        "pytest --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py -rA\n"
        + "if [ $? -eq 0 ]; then"
        + SCRIPT.split("if [ $? -eq 0 ]; then", 1)[1]
    )
    script = prefix + "pip install pytest==8.4.1 requests==2.32.5\n" + suffix
    spec = dependency_spec(script)
    assert spec["runner"] == "system"
    assert spec["python_version"] == "system"
    assert (
        spec["prepared_script"] == '#!/bin/bash\ntest "$PWD" != / || exit 1\n' + suffix
    )
    assert "PATH=" not in spec["prepared_script"]
    with pytest.raises(ValueError, match="pinned"):
        dependency_spec(script.replace("pytest==8.4.1", "pytest>=8"))


def test_runtime_cache_marker_and_lock_must_match_manifest(tmp_path):
    import hashlib

    (tmp_path / "venv").mkdir()
    (tmp_path / "venv/pyvenv.cfg").write_text("version = 3.13.9\n")
    (tmp_path / "spec.sha256").write_text("a" * 64 + "\n")
    lock = b"pytest==8.4.1\n"
    (tmp_path / "requirements.lock").write_bytes(lock)
    meta = {
        "path": str(tmp_path),
        "spec_sha256": "a" * 64,
        "requirements_lock_sha256": hashlib.sha256(lock).hexdigest(),
    }
    assert verifier_runtime_metadata(meta) == meta
    (tmp_path / "requirements.lock").write_text("pytest==0.0.0\n")
    with pytest.raises(ValueError, match="Stale"):
        verifier_runtime_metadata(meta)


def test_runtime_cache_detects_modified_model_asset(tmp_path):
    import hashlib

    (tmp_path / "venv").mkdir()
    (tmp_path / "venv/pyvenv.cfg").write_text("version = 3.11.14\n")
    (tmp_path / "spec.sha256").write_text("a" * 64 + "\n")
    lock = b"pytest==8.4.1\n"
    (tmp_path / "requirements.lock").write_bytes(lock)
    (tmp_path / "assets").mkdir()
    asset = tmp_path / "assets/model.pt"
    asset.write_bytes(b"downloaded model")
    meta = {
        "path": str(tmp_path),
        "spec_sha256": "a" * 64,
        "requirements_lock_sha256": hashlib.sha256(lock).hexdigest(),
        "files_sha256": {"assets/model.pt": hashlib.sha256(asset.read_bytes()).hexdigest()},
    }
    assert verifier_runtime_metadata(meta) == meta
    asset.write_bytes(b"modified model")
    with pytest.raises(ValueError, match="Stale prepared verifier runtime asset"):
        verifier_runtime_metadata(meta)


def test_reserved_space_requires_explicit_root_docker_opt_in(monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "prepare_verifiers_test",
        Path(__file__).parents[1] / "scripts/prepare_tb21_verifiers.py",
    )
    preparation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preparation)
    monkeypatch.setattr(
        preparation.shutil, "disk_usage", lambda _: SimpleNamespace(free=0)
    )
    monkeypatch.setattr(
        preparation.os, "statvfs", lambda _: SimpleNamespace(f_bfree=10, f_frsize=1024)
    )
    monkeypatch.setattr(preparation.os, "geteuid", lambda: 0)
    monkeypatch.setattr(preparation, "run", lambda *args, **kwargs: "[]")
    assert preparation.docker_free_bytes("/docker") == 0
    assert preparation.docker_free_bytes("/docker", include_reserved=True) == 10240
    monkeypatch.setattr(preparation, "run", lambda *args, **kwargs: '["name=rootless"]')
    with pytest.raises(ValueError, match="Rootless"):
        preparation.docker_free_bytes("/docker", include_reserved=True)
    monkeypatch.setattr(preparation.os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="requires root"):
        preparation.docker_free_bytes("/docker", include_reserved=True)


@pytest.mark.parametrize(
    "before,after",
    [("0.9.5", "0.9.6"), ("-p 3.13", "-p 3.12"), ("pytest==8.4.1", "pytest>=8")],
)
def test_unknown_dependencies_do_not_silently_change_verification(before, after):
    with pytest.raises(ValueError):
        dependency_spec(SCRIPT.replace(before, after))


def test_manifest_is_bound_to_script_base_image_and_offline_check(tmp_path):
    (tmp_path / "test.sh").write_text(SCRIPT)
    spec = dependency_spec(SCRIPT)
    entry = {
        "spec_sha256": spec["spec_sha256"],
        "base_image": "original:1",
        "image_id": "sha256:" + "a" * 64,
        "offline_check_passed": True,
    }
    manifest = tmp_path / "manifest.json"
    native = SimpleNamespace(
        paths=SimpleNamespace(tests_dir=tmp_path),
        config=SimpleNamespace(environment=SimpleNamespace(docker_image="original:1")),
    )
    manifest.write_text(json.dumps({"schema": 1, "tasks": {"task": entry}}))
    assert prepared_entry(manifest, "task", native) == entry
    assert prepared_entry(manifest, "unprepared", native) is None
    (tmp_path / "test.sh").write_text(SCRIPT + "echo changed\n")
    with pytest.raises(ValueError, match="Stale"):
        prepared_entry(manifest, "task", native)
    (tmp_path / "test.sh").write_text(SCRIPT)
    for key, bad in (
        ("base_image", "other:1"),
        ("image_id", "mutable:tag"),
        ("offline_check_passed", False),
    ):
        manifest.write_text(
            json.dumps({"schema": 1, "tasks": {"task": {**entry, key: bad}}})
        )
        with pytest.raises(ValueError, match="Stale"):
            prepared_entry(manifest, "task", native)


def test_binding_pins_manifest_for_all_subsequent_trials(tmp_path):
    pytest.importorskip("harbor")
    from flashrsi.agents import MiniAgent
    from flashrsi.integrations.harbor import bind

    manifest = tmp_path / "manifest.json"
    original = {"schema": 1, "tasks": {"task": {"image_id": "sha256:" + "a" * 64}}}
    manifest.write_text(json.dumps(original))
    agent = MiniAgent(
        {
            "llm": {"model_name": "offline", "api_key": "unused"},
            "harbor": {"verifier_dependencies_manifest": str(manifest)},
        }
    )
    bind(agent)
    manifest.write_text(json.dumps({"schema": 1, "tasks": {}}))
    assert agent._prepared_verifier_manifest == original


@pytest.mark.parametrize("reward", [0, 1])
def test_real_harbor_verifier_preserves_tests_and_reads_original_rewards(
    tmp_path, reward
):
    pytest.importorskip("harbor")
    from harbor.models.task.config import TaskOS
    from harbor.models.task.paths import TaskPaths
    from harbor.models.trial.paths import TrialPaths

    from flashrsi.integrations._prepared_verifier import PreparedVerifier

    source = tmp_path / "tests"
    source.mkdir()
    (source / "test.sh").write_text(SCRIPT)
    (source / "test_outputs.py").write_bytes(b"original tests\x00\xff")
    spec = dependency_spec(SCRIPT)
    paths = TrialPaths(tmp_path / "trial")
    paths.mkdir()
    uploads, commands = [], []

    async def upload_dir(source_dir, target_dir):
        uploads.append(Path(source_dir))
        assert target_dir == "/tests"
        assert (
            source_dir / "test_outputs.py"
        ).read_bytes() == b"original tests\x00\xff"
        assert (source_dir / "test.sh").read_text() == spec["prepared_script"]

    async def execute(command, **kwargs):
        commands.append(command)
        if command.startswith("cat "):
            return SimpleNamespace(return_code=0, stdout=spec["spec_sha256"] + "\n")
        if not command.startswith("chmod"):
            paths.reward_text_path.write_text(str(reward))
        return SimpleNamespace(return_code=0, stdout="")

    environment = SimpleNamespace(
        os=TaskOS.LINUX,
        exec=execute,
        upload_dir=upload_dir,
        capabilities=SimpleNamespace(mounted=True),
    )
    task = SimpleNamespace(
        paths=TaskPaths(tmp_path),
        config=SimpleNamespace(verifier=SimpleNamespace(env={})),
    )
    verifier = PreparedVerifier(
        task=task,
        trial_paths=paths,
        environment=environment,
        dependency_spec_sha256=spec["spec_sha256"],
    )
    result = asyncio.run(verifier.verify())
    assert result.rewards == {"reward": reward}
    assert (source / "test.sh").read_text() == SCRIPT
    assert len(uploads) == 1 and not uploads[0].exists()
    assert any("/tests/test.sh" in cmd for cmd in commands)
    assert (paths.verifier_dir / "dependency-preparation.json").exists()
