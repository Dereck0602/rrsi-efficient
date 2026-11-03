"""Pinned Hub transport, exact published membership and native resource isolation."""

import asyncio
import copy
import hashlib
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from flashrsi.benchmarks import NQ, HotpotQA, TriviaQA, _prepared
from flashrsi.benchmarks._data import _split_tasks
from flashrsi.core import Task, Trajectory


def package(
    tmp_path,
    monkeypatch,
    name="hotpotqa",
    rows=None,
    resources=None,
    membership=None,
    revision=None,
):
    release = (
        "20261030-batch2"
        if name.startswith("tau2_")
        or name in {"alfworld", "math", "travelplanner", "twowiki_multihopqa"}
        else "20261030"
    )
    pin = revision or _prepared.REVISION
    canonical = "terminal_bench_2_0" if name == "terminal_bench" else name
    legacy = pin in _prepared.LEGACY_REVISIONS
    prefix = f"prepared/{release}/{name}" if legacy else canonical
    files = {}
    for role in ("train", "dev", "test"):
        path = tmp_path / f"{role}.parquet"
        row = (
            rows(role)
            if rows
            else {
                "id": role,
                "question": "Who?",
                "answer": "SECRET_LABEL",
                "native_source_split": "original-eval",
                "context": {"title": ["Public"], "sentences": [["Public context."]]},
            }
        )
        pq.write_table(pa.Table.from_pylist([row]), path)
        files[role + ".parquet"] = path
    ids = tmp_path / "split_ids.json"
    ids.write_text(
        json.dumps(membership or {r: [r] for r in ("train", "dev", "test")}),
        encoding="utf-8",
    )
    files["split_ids.json"] = ids
    files.update(resources or {})
    manifest = {
        "schema": "flashrsi.prepared-dataset.v1",
        "benchmark": name,
        "source": {"revision": "upstream"},
        "counts": dict.fromkeys(("train", "dev", "test"), 1),
        "split_policy": "fixture-published-roles",
        "files": {
            k: {
                "bytes": p.stat().st_size,
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for k, p in files.items()
        },
    }
    if not legacy:
        manifest.update(config_name=canonical, directory=prefix)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    files["manifest.json"] = manifest_path
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "layout": "benchmark-directories.v1",
                "configurations": [
                    {"name": canonical, "directory": prefix, "kind": "experiment"}
                ],
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def download(*, filename, repo_id, repo_type, revision):
        assert repo_id == _prepared.REPO and repo_type == "dataset"
        assert revision == pin
        if filename == "metadata/catalog.json":
            calls.append(filename)
            return str(catalog)
        assert filename.startswith(prefix + "/")
        relative = filename[len(prefix) + 1 :]
        calls.append(relative)
        return str(files[relative])

    monkeypatch.setattr(_prepared, "hf_hub_download", download)
    monkeypatch.setattr(_prepared.constants, "HF_HOME", str(tmp_path / "cache"))
    config = {
        "name": name,
        "dataset": {
            "source": _prepared.REPO,
            "revision": pin,
            "config": name + "_experiment_v1" if legacy else canonical,
        },
        "split": "auto",
    }
    return config, files, calls


def test_preserves_published_membership_and_does_not_mutate_input(
    tmp_path, monkeypatch
):
    config, files, calls = package(tmp_path, monkeypatch)
    original = copy.deepcopy(config)
    resolved = _prepared.prepare(config)
    assert config == original
    benchmark = HotpotQA(resolved)
    splits = benchmark.load()
    assert {r: [t.task_id for t in tasks] for r, tasks in splits.items()} == {
        r: [r] for r in splits
    }
    assert "SECRET_LABEL" not in benchmark.test[0].question
    assert benchmark.test[0].metadata["native_source_split"] == "original-eval"
    assert benchmark.test[0].metadata["source_split"] == "test"
    assert (
        benchmark.grade((Trajectory(task=benchmark.test[0], output="SECRET_LABEL"),))[
            0
        ].metrics["exact_match"]
        == 1
    )
    assert set(calls) == {
        "metadata/catalog.json",
        "manifest.json",
        "split_ids.json",
        "train.parquet",
        "dev.parquet",
        "test.parquet",
    }
    # limit cannot move a held-out example into training.
    tasks = [
        Task(r + str(i), r, metadata={"source_split": "unassigned"})
        for r in splits
        for i in range(3)
    ]
    membership = {r: [r + str(i) for i in range(3)] for r in splits}
    sampled = _split_tasks(tasks, {"_prepared_splits": membership, "limit": 3})
    assert all(len(v) == 1 and v[0].task_id.startswith(r) for r, v in sampled.items())


@pytest.mark.parametrize("cls,name", [(NQ, "nq"), (TriviaQA, "triviaqa")])
def test_mrqa_context_and_aliases_are_not_parsed_as_native_spans(
    tmp_path, monkeypatch, cls, name
):
    def row(role):
        return {
            "id": role,
            "question": "Who?",
            "context": "PUBLIC_CONTEXT",
            "answers": ["SECRET_CANONICAL", "Alias"],
            "mrqa_question_json": "SECRET_ANNOTATION",
        }

    config, _, _ = package(tmp_path, monkeypatch, name, row)
    benchmark = cls(_prepared.prepare(config))
    benchmark.load()
    task = benchmark.test[0]
    assert "PUBLIC_CONTEXT" in task.question and "SECRET_" not in task.question
    assert benchmark.grade((Trajectory(task=task, output="The alias!"),))[
        0
    ].metrics == {"exact_match": 1.0, "f1": 1.0}
    assert benchmark.metadata["scoring"] == "text_em_f1"
    assert "scorer_revision" not in benchmark.metadata


@pytest.mark.parametrize(
    "field,value",
    [
        ("split_counts", {"train": 1, "dev": 1, "test": 1}),
        ("split", {"train": 1, "dev": 0, "test": 0}),
        ("data_path", "local"),
        ("evolution_split", "train"),
    ],
)
def test_repartitioning_and_source_conflicts_fail_before_download(
    tmp_path, monkeypatch, field, value
):
    config, _, calls = package(tmp_path, monkeypatch)
    config[field] = value
    with pytest.raises(ValueError):
        _prepared.prepare(config)
    assert not calls


def test_download_hash_and_exact_task_ids_are_checked(tmp_path, monkeypatch):
    config, files, _ = package(tmp_path, monkeypatch)
    files["train.parquet"].write_bytes(b"TAMPERED")
    with pytest.raises(ValueError, match="checksum"):
        _prepared.prepare(config)
    with pytest.raises(ValueError, match="missing"):
        _split_tasks(
            [Task("other", "question")], {"_prepared_splits": {"test": ["missing-id"]}}
        )


def native_archive(tmp_path):
    path = tmp_path / "native.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for name, content in {
            "src/tool.py": b"# pinned tool\n",
            "data/input.txt": b"PUBLIC",
        }.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    proof = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    return path, proof


def test_native_archive_reuses_cache_and_rejects_changed_code(tmp_path, monkeypatch):
    monkeypatch.setattr(_prepared.constants, "HF_HOME", str(tmp_path / "cache"))
    path, proof = native_archive(tmp_path)
    root = _prepared.unpack(path, proof["sha256"])
    assert _prepared.unpack(path, proof["sha256"]) == root
    assert _prepared.verify_native(root, proof) == proof["sha256"]
    (root / "src/tool.py").write_text("modified")
    with pytest.raises(ValueError, match="changed"):
        _prepared.verify_native(root, proof)


@pytest.mark.parametrize("name", ["../escape", "C:/escape", "file:stream"])
def test_native_archive_cannot_escape_cache(tmp_path, monkeypatch, name):
    monkeypatch.setattr(_prepared.constants, "HF_HOME", str(tmp_path / "cache"))
    path = tmp_path / "bad.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe"):
        _prepared.unpack(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert not (tmp_path / "escape").exists()


def test_runner_resolves_hf_for_frozen_evaluation_without_changing_snapshot(
    tmp_path, monkeypatch
):
    from flashrsi import _startup, runner
    from flashrsi.agents.miniagent import MiniAgent
    from flashrsi.config import load_config
    from flashrsi.integrations import supplied_context

    async def setup(docker, name, network, payload, config, timeout):
        request = json.loads((payload / "request.json").read_text())
        assert "SECRET_LABEL" not in request["prompt"]
        assert set(request) == {"agent_class", "config_yaml", "snapshot_path", "prompt"}

    benchmark_config, _, _ = package(tmp_path, monkeypatch)
    source = tmp_path / "harness"
    source.mkdir()
    (source / "components.yaml").write_text("prompt: [prompt.md]\n")
    (source / "prompt.md").write_text("Original harness")
    monkeypatch.setattr(supplied_context.shutil, "which", lambda name: name)
    monkeypatch.setattr(supplied_context, "_setup", setup)
    monkeypatch.setattr(
        supplied_context,
        "_run",
        AsyncMock(return_value=Trajectory(output="SECRET_LABEL")),
    )
    monkeypatch.setattr(supplied_context, "_cleanup", AsyncMock())
    monkeypatch.setattr(_startup, "_preflight", AsyncMock())
    config = load_config(
        overrides={
            "run": {"mode": "evaluate", "snapshot": "fixture.json"},
            "agent": {
                "name": "miniagent",
                "source_dir": str(source),
                "llm": {"model_name": "fixture", "api_key": "fixture"},
            },
            "benchmark": benchmark_config,
        }
    )
    original = copy.deepcopy(dict(config.benchmark))
    snapshot = MiniAgent(config.agent).snapshot
    target, benchmark = asyncio.run(runner._prepare_target(config, snapshot=snapshot))
    assert config.benchmark == original
    assert target.snapshot.id == snapshot.id
    assert target.snapshot.files == snapshot.files
    assert benchmark.metadata["prepared"]["revision"] == _prepared.REVISION
    result = benchmark.grade(asyncio.run(target.rollout(benchmark.test)))
    assert result[0].metrics["exact_match"] == 1
    assert "train" not in [t.task.task_id for t in result]


def test_deepsearchqa_keeps_published_ids_instead_of_native_prefix(
    tmp_path, monkeypatch
):
    from flashrsi.benchmarks import DeepSearchQA

    config, _, _ = package(
        tmp_path,
        monkeypatch,
        "deepsearchqa",
        lambda role: {
            "_flashrsi_id": role,
            "problem": role + " public problem",
            "answer": "PRIVATE",
            "problem_category": "History",
            "answer_type": "Single Answer",
        },
    )
    benchmark = DeepSearchQA(_prepared.prepare(config))
    assert benchmark.load()["test"][0].task_id == "test"
    assert "PRIVATE" not in benchmark.test[0].question


def test_spreadsheet_roles_map_to_native_task_ids(tmp_path, monkeypatch):
    paths = (
        "evaluation/evaluation.py",
        "evaluation/open_spreadsheet.py",
        "SWE-agent/spreadsheet.Dockerfile",
    )
    resources = {}
    for i, name in enumerate(paths):
        path = tmp_path / str(i)
        path.write_bytes(b"# original native source")
        resources["upstream/code/" + name] = path
    archive = tmp_path / "workbooks.zip"
    archive.write_bytes(b"ZIP transport fixture")
    resources["upstream/spreadsheetbench-v2-09-10.zip"] = archive
    ids = dict(
        zip(
            ("train", "dev", "test"),
            ("Debugging/01", "Financial_Model/02", "Template/03"),
        )
    )
    config, _, _ = package(
        tmp_path,
        monkeypatch,
        "spreadsheetbench2",
        resources=resources,
        membership={r: [v] for r, v in ids.items()},
    )
    resolved = _prepared.prepare(config)
    assert resolved["_prepared_splits"] == {
        "train": ["debugging-01"],
        "dev": ["financial-model-02"],
        "test": ["template-03"],
    }
    splits = _split_tasks(
        [Task(v[0], "Public task") for v in resolved["_prepared_splits"].values()],
        resolved,
    )
    assert all(
        tasks[0].metadata["prepared_task_id"] == ids[role]
        for role, tasks in splits.items()
    )
    assert resolved["_prepared_spreadsheet"]["archive"] == str(archive)


def test_long_native_filenames_remain_readable(tmp_path, monkeypatch):
    monkeypatch.setattr(
        _prepared.constants, "HF_HOME", str(tmp_path / "nested" / "cache")
    )
    path = tmp_path / "long.tar.gz"
    name = "native/" + "a" * 180 + ".txt"
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = 6
        archive.addfile(member, io.BytesIO(b"PUBLIC"))
    root = _prepared.unpack(path, hashlib.sha256(path.read_bytes()).hexdigest())
    assert (root / name).read_bytes() == b"PUBLIC"


def test_appworld_uses_matching_wheel_constants_and_official_bundle_format(
    tmp_path, monkeypatch
):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    monkeypatch.setattr(_prepared.constants, "HF_HOME", str(tmp_path / "cache"))
    wheel = tmp_path / "runtime.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "appworld/common/constants.py",
            "PASSWORD = 'fixture'\nSALT = b'format-salt'\nraise RuntimeError('do not execute wheel code')\n",
        )
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        archive.writestr("data/datasets/train.txt", "native-task-id\n")
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=b"format-salt", iterations=100000
    ).derive(b"fixture")
    iv = b"0" * 16
    encryptor = Cipher(algorithms.AES(key), CFB(iv)).encryptor()
    bundle = tmp_path / "data.bundle"
    bundle.write_bytes(iv + encryptor.update(buffer.getvalue()) + encryptor.finalize())
    root = _prepared._appworld(
        bundle, wheel, hashlib.sha256(bundle.read_bytes()).hexdigest()
    )
    assert (root / "datasets/train.txt").read_text() == "native-task-id\n"


@pytest.mark.parametrize("name", ["appworld", "tau3_banking", "tau2"])
def test_launcher_does_not_replace_hf_inputs_with_native_downloads(
    tmp_path, monkeypatch, name
):
    from flashrsi import _launch
    from flashrsi.config import Config
    from flashrsi.integrations import _docker

    config = Config(
        {
            "benchmark": {
                "name": name,
                "dataset": {
                    "source": _prepared.REPO,
                    "revision": _prepared.REVISION,
                    "config": ("tau2_telecom" if name == "tau2" else name)
                    + "_experiment_v1",
                },
            }
        }
    )
    monkeypatch.delenv("TAU2_DATA_DIR", raising=False)
    monkeypatch.setattr(_launch, "_check_docker", AsyncMock())
    docker = AsyncMock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(_docker, "_docker", docker)

    def forbidden(*args, **kwargs):
        pytest.fail("HF prepared mode must not download an official data source")

    monkeypatch.setattr("flashrsi.benchmarks._git._download_git", forbidden)
    asyncio.run(_launch._resources(config, tmp_path))
    assert config.benchmark.get("data_path") is None
    assert all(call.args[0] == "image" for call in docker.call_args_list)


def tar_resource(tmp_path, members, name="native.tar.gz"):
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as archive:
        for filename, payload in members.items():
            content = payload.encode()
            entry = tarfile.TarInfo(filename)
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return path


@pytest.mark.parametrize("domain", ["airline", "retail", "telecom"])
def test_tau2_prepared_domains_keep_native_splits_and_private_scripts(
    tmp_path, monkeypatch, domain
):
    from flashrsi.benchmarks import Tau2Benchmark

    root = f"data/tau2/domains/{domain}/"
    native = {root + name: "{}" for name in ("db.json", "db.toml", "user_db.toml")}
    native.update(
        {
            root + name: "PUBLIC_POLICY"
            for name in ("policy.md", "main_policy.md", "tech_support_manual.md")
        }
    )
    native[root + "tasks.json"] = json.dumps(
        [
            {
                "id": str(i),
                "user_scenario": {"instructions": "PRIVATE_SCRIPT"},
                "evaluation_criteria": {"reward_basis": ["DB"]},
            }
            for i in range(3)
        ]
    )
    native_splits = {"base": ["0", "1", "2"], "train": ["0", "1"], "test": ["2"]}
    native[root + "split_tasks.json"] = json.dumps(native_splits)
    native.update(
        {
            "data/tau2/user_simulator/" + name: "PRIVATE_SIMULATOR_GUIDELINES"
            for name in ("simulation_guidelines.md", "simulation_guidelines_tools.md")
        }
    )
    config, _, calls = package(
        tmp_path,
        monkeypatch,
        "tau2_" + domain,
        resources={"native-resources.tar.gz": tar_resource(tmp_path, native)},
        membership={"train": ["0"], "dev": ["1"], "test": ["2"]},
    )
    config.update(name="tau2", domain=domain)
    resolved = _prepared.prepare(config)
    bench = Tau2Benchmark(resolved)
    assert {r: [t.task_id for t in tasks] for r, tasks in bench.load().items()} == {
        "train": ["0"],
        "dev": ["1"],
        "test": ["2"],
    }
    assert (
        json.loads((Path(resolved["data_path"]) / "split_tasks.json").read_text())
        == native_splits
    )
    assert Path(bench.test[0].metadata["prepared_user_simulator"]).is_dir()
    assert all(
        "PRIVATE" not in task.question
        for tasks in bench.load().values()
        for task in tasks
    )
    assert "experiment_split_tasks.json" not in calls


def test_tau2_wrong_domain_and_variant_fail_before_downloading(tmp_path, monkeypatch):
    config, _, calls = package(tmp_path, monkeypatch, "tau2_airline")
    config.update(name="tau2", domain="telecom")
    with pytest.raises(ValueError, match="match"):
        _prepared.prepare(config)
    config.update(domain="airline", task_split="full")
    with pytest.raises(ValueError, match="task_split"):
        _prepared.prepare(config)
    assert not calls


def test_alfworld_prepared_archive_reuses_native_loader_without_original_zips(
    tmp_path, monkeypatch
):
    from flashrsi.benchmarks import ALFWorld

    members, ids = {}, {}
    for role, native in (
        ("train", "train"),
        ("dev", "valid_seen"),
        ("test", "valid_unseen"),
    ):
        relative = f"{native}/pick_and_place_simple-fixture/trial_1"
        ids[role] = ["alfworld/" + relative]
        root = "json_2.1.1/" + relative
        members[root + "/traj_data.json"] = json.dumps(
            {"task_type": "pick_and_place_simple", "plan": "PRIVATE_EXPERT_PLAN"}
        )
        members[root + "/game.tw-pddl"] = json.dumps({"solvable": True})
    config, _, calls = package(
        tmp_path,
        monkeypatch,
        "alfworld",
        resources={"experiment-native.tar.gz": tar_resource(tmp_path, members)},
        membership=ids,
    )
    benchmark = ALFWorld(_prepared.prepare(config))
    assert {
        r: [t.task_id for t in tasks] for r, tasks in benchmark.load().items()
    } == ids
    assert "PRIVATE" not in repr(benchmark.load())
    assert not any(path.endswith(".zip") for path in calls)


def test_travelplanner_prepared_database_is_separate_from_question_records(
    tmp_path, monkeypatch
):
    database = tar_resource(
        tmp_path, {"database/flights/public.csv": "PUBLIC_DATABASE"}
    )
    config, _, calls = package(
        tmp_path, monkeypatch, "travelplanner", resources={"database.tar.gz": database}
    )
    resolved = _prepared.prepare(config)
    assert (
        Path(resolved["database_path"]) / "flights/public.csv"
    ).read_text() == "PUBLIC_DATABASE"
    assert isinstance(resolved["data_path"], dict)
    assert not (Path(resolved["database_path"]) / "train.parquet").exists()
    assert "native-source.tar.gz" not in calls


def test_previous_prepared_revision_remains_explicitly_selectable(
    tmp_path, monkeypatch
):
    pin = "1b976eec446eec4cb5354c07bc81b27f92b69c49"
    config, _, _ = package(tmp_path, monkeypatch, revision=pin)
    assert _prepared.prepare(config)["_prepared_source"]["revision"] == pin


def test_alfworld_hf_launch_plan_does_not_require_manual_data_path():
    from flashrsi import _launch

    config = _launch._load(
        Path(__file__).parents[1] / "examples/hf_prepared_alfworld.yaml"
    )
    plan = _launch._plan(config)
    assert "HF prepared alfworld" in plan["data"]
    assert plan["extras"] == ["all"]
    assert not any("data_path" in p for p in plan["prerequisites"])


def test_prepared_profiles_cover_registered_benchmarks_except_hle():
    from flashrsi import _hf, runner

    assert set(_hf.BENCHMARKS) == set(runner._BENCHMARKS) - {"hle"}
    assert len(_hf.PROFILES) == 34
    assert not _hf.selected(
        {"name": "hle", "dataset": {"source": _hf.REPO, "config": "hle"}}
    )


def test_catalog_resources_under_native_directory(tmp_path, monkeypatch):
    archive, _ = native_archive(tmp_path)
    config, _, calls = package(
        tmp_path, monkeypatch, "workbench", resources={"native/native.tar.gz": archive}
    )
    resolved = _prepared.prepare(config)
    assert (Path(resolved["_prepared_root"]) / "src/tool.py").is_file()
    assert "native/native.tar.gz" in calls
    assert resolved["_prepared_source"]["path"] == "workbench"


def test_searchqa_original_manifest_keeps_skillopt_subset_roles(tmp_path, monkeypatch):
    config, files, _ = package(
        tmp_path,
        monkeypatch,
        "searchqa",
        rows=lambda role: {
            "id": role,
            "question": "Public question",
            "context": "Public context",
            "answers": ["PRIVATE_ANSWER"],
        },
    )
    manifest = {
        "schema": "flashrsi.searchqa.v1",
        "source": {"repo": "original"},
        "notes": {"subset": "published subset"},
        "splits": {
            r: {
                "file": f"searchqa/{r}.parquet",
                "count": 1,
                "bytes": files[r + ".parquet"].stat().st_size,
                "sha256": hashlib.sha256(
                    files[r + ".parquet"].read_bytes()
                ).hexdigest(),
            }
            for r in ("train", "dev", "test")
        },
    }
    files["manifest.json"].write_text(json.dumps(manifest))
    from flashrsi.benchmarks import SearchQA

    benchmark = SearchQA(_prepared.prepare(config))
    assert {r: [t.task_id for t in tasks] for r, tasks in benchmark.load().items()} == {
        r: [r] for r in ("train", "dev", "test")
    }
    assert "PRIVATE" not in benchmark.test[0].question
    assert (
        benchmark.grade((Trajectory(task=benchmark.test[0], output="PRIVATE_ANSWER"),))[
            0
        ].metrics["exact_match"]
        == 1
    )


def test_aime_prepared_years_and_integer_scoring(tmp_path, monkeypatch):
    from flashrsi.benchmarks import AIME

    roles = {"train": (2025, "1"), "dev": (2025, "2"), "test": (2026, "1")}
    config, _, _ = package(
        tmp_path,
        monkeypatch,
        "aime",
        rows=lambda role: {
            "id": roles[role][1],
            "native_year": roles[role][0],
            "problem": "Compute a number.",
            "answer": "007",
        },
        membership={r: [f"aime{y}:{i}"] for r, (y, i) in roles.items()},
    )
    bench = AIME(_prepared.prepare(config))
    bench.load()
    assert bench.train[0].metadata["year"] == 2025
    assert bench.test[0].metadata["year"] == 2026
    assert bench.metadata["years"] == (2025, 2026) and "year" not in bench.metadata
    assert (
        bench.grade((Trajectory(task=bench.test[0], output=r"\boxed{7}"),))[0].metrics[
            "accuracy"
        ]
        == 1
    )


@pytest.mark.parametrize(
    "version,language,subset",
    [("202412", "cn", "AMC"), ("202412", "en", "CNMO"), ("202505", "en", "all")],
)
def test_livemath_prepared_native_subsets_and_explicit_question_type(
    tmp_path, monkeypatch, version, language, subset
):
    from flashrsi.benchmarks import LiveMathBench

    def row(role):
        return {
            "question": role + " problem",
            "answer": "PRIVATE_REFERENCE",
            "question_type": "problem-solving",
            "native_subset": subset,
            "native_language": language,
            "type": "subject",
        }

    ids = {
        r: [
            f"v{version}_{subset}_{language}:"
            + hashlib.sha256(row(r)["question"].encode()).hexdigest()
        ]
        for r in ("train", "dev", "test")
    }
    config, _, _ = package(
        tmp_path,
        monkeypatch,
        f"livemathbench_{version}_{language}",
        rows=row,
        membership=ids,
    )
    config.update(name="livemathbench", version=int(version))
    bench = LiveMathBench(_prepared.prepare(config))
    assert {r: [t.task_id for t in tasks] for r, tasks in bench.load().items()} == ids
    assert bench.test[0].metadata["question_type"] == "problem-solving"
    assert "PRIVATE" not in bench.test[0].question


@pytest.mark.parametrize(
    "profile,name,overrides",
    [
        ("jobbench_easy", "jobbench", {"collection": "main"}),
        ("livemathbench_202412_cn", "livemathbench", {"dataset_languages": ["en"]}),
        ("terminal_bench_2_0", "terminal_bench", {"version": "2.1"}),
    ],
)
def test_profile_variant_conflicts_fail_before_download(
    tmp_path, monkeypatch, profile, name, overrides
):
    config, _, calls = package(tmp_path, monkeypatch, profile)
    config.update(name=name, **overrides)
    with pytest.raises(ValueError, match="match"):
        _prepared.prepare(config)
    assert not calls


def test_launcher_cache_relocation_preserves_existing_hf_login_path(
    tmp_path, monkeypatch
):
    from scripts import run_experiment

    for key in ("HF_HOME", "HF_TOKEN_PATH", "HF_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "existing-cache"))
    env = run_experiment._environment(tmp_path / "experiment-cache")
    assert Path(env["HF_TOKEN_PATH"]) == tmp_path / "existing-cache/huggingface/token"
    assert Path(env["HF_HOME"]) == tmp_path / "experiment-cache/huggingface"
    monkeypatch.setenv("HF_TOKEN_PATH", str(tmp_path / "explicit-token"))
    assert run_experiment._environment(tmp_path)["HF_TOKEN_PATH"] == str(
        tmp_path / "explicit-token"
    )
    monkeypatch.setenv("HF_HOME", str(tmp_path / "explicit-home"))
    assert run_experiment._environment(tmp_path)["HF_HOME"] == str(
        tmp_path / "explicit-home"
    )
