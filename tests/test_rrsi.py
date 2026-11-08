"""Measured regularization, full evidence, and directory editing protocols."""

import asyncio
import json
import shutil
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from flashrsi._progress import progress_scope, progress_session
from flashrsi.agents import ClaudeCode, MiniAgent
from flashrsi.algorithms import RRSI
from flashrsi.algorithms.rrsi import (
    _COMPONENTS as COMPONENTS,
)
from flashrsi.algorithms.rrsi import (
    _directives as directives,
)
from flashrsi.algorithms.rrsi import (
    _judge as judge,
)
from flashrsi.algorithms.rrsi import (
    _noise_band as noise_band,
)
from flashrsi.algorithms.rrsi import (
    _parse_edits as declaration,
)
from flashrsi.algorithms.rrsi import _token_accounting
from flashrsi.algorithms.rrsi import (
    _tokens as tokens,
)
from flashrsi.config import Config
from flashrsi.core import Agent, Benchmark, Snapshot, Task, Trajectory


class Tasks(Benchmark):
    name = "rrsi-fixture"
    description = "Disjoint evolution and heldout tasks."
    primary_metric = "accuracy"
    metadata = {}

    def load(self):
        if self._splits is None:
            self._splits = {
                "train": (Task("train-0", "train-0"), Task("train-1", "train-1")),
                "dev": (Task("dev-private", "dev-private"),),
                "test": (Task("test-private", "test-private"),),
            }
        return self._splits

    def grade(self, results):
        return tuple(
            replace(row, metrics={"accuracy": float(row.output or 0)})
            for row in results
        )


class Worker(Agent):
    name = "rrsi-editor"
    description = "Offline editor and reviewer."

    def __init__(self, edit, *, relocate=False, critic=None):
        super().__init__({"type": "agent", "llm": {"api_key": "worker-private-secret"}})
        self.apply_edit = edit
        self.relocate = relocate
        self.critic = critic
        self.prompts = []

    async def _run(self, prompt, working_dir=None):
        self.prompts.append(prompt)
        if prompt.startswith("[RRSI: critic]"):
            value = (
                self.critic(prompt)
                if self.critic
                else {"verdict": "accept", "reasons": []}
            )
            return Trajectory(output=json.dumps(value))
        if prompt.startswith("[RRSI: analyze]"):
            tasks = json.loads(prompt.split("first): ")[1].split("\nPrior")[0])
            value = (
                {
                    "action": "digest_many",
                    "requests": [{"task_id": tasks[0]["task_id"], "lens": "failure"}],
                }
                if "Interaction log: []" in prompt
                else {
                    "action": "report",
                    "failure_modes": [
                        {
                            "mode": "evidence_check",
                            "description": "Check supplied evidence",
                            "n_tasks": len(tasks),
                        }
                    ],
                    "capability_gaps": [],
                    "success_habits": [],
                }
            )
            return Trajectory(output=json.dumps(value))
        if prompt.startswith("[RRSI: digest]"):
            trace = json.loads((working_dir / "trace.json").read_text())
            task = trace["task_id"]
            lens = prompt.split("lens: ")[1].split(";")[0]
            quote = trace["trajectory"]["output"] or task
            # A digester's unrelated writes cannot enter the target Snapshot.
            (working_dir / "ANALYST-TAMPER").write_text("discard me", newline="\n")
            value = {
                "task_id": task,
                "lens": lens,
                "evidence": [{"where": "trajectory.output", "quote": quote}],
                "blocker": "Missed evidence",
                "narrative": "Inspect supplied evidence",
                "verifier_evidence": "Accuracy",
                "needed_instead": "Evidence checking",
                "wanted": "Evidence",
                "why_couldnt": "Missing inspection",
                "habits": [
                    {"habit": "Check evidence", "where_shown": "trajectory.output"}
                ],
                "risk_if_removed": "Missed evidence",
            }
            return Trajectory(output=json.dumps(value))
        if self.relocate:
            with TemporaryDirectory() as temporary:
                relocated = Path(temporary) / "workspace"
                shutil.copytree(working_dir, relocated)
                edits = self.apply_edit(prompt, relocated)
                shutil.copytree(relocated, working_dir, dirs_exist_ok=True)
        else:
            edits = self.apply_edit(prompt, working_dir)
        return Trajectory(output=json.dumps({"edits": edits}))


def change(component="skill", path="skills/main.md", **extra):
    return {
        "id": f"{component}:{path}",
        "component": component,
        "hypothesis": "Check evidence before committing.",
        "targets_mode": "evidence_check",
        "predicted_affected": ["train-0"],
        "retroactive_check": "Would have checked the earlier failure's evidence.",
        "paths": [path],
        **extra,
    }


def records(prompt, directory):
    reference = prompt.split("`")[1]
    assert reference.endswith("/context.json")
    return [json.loads((directory / reference).read_text())]


def setup(
    monkeypatch,
    tmp_path,
    edit,
    *,
    cls=MiniAgent,
    relocate=False,
    critic=None,
    outcomes=None,
    **options,
):
    source = tmp_path / "source"
    (source / "skills").mkdir(parents=True)
    (source / "tools").mkdir()
    (source / "skills/main.md").write_text("SEED", newline="\n")
    (source / "tools/runtime.py").write_text("value = 0\n", newline="\n")
    (source / "tools/binary").write_bytes(b"\x00\xff")
    (source / "components.yaml").write_text(
        "skill: ['skills/**']\ntool: ['tools/**']", newline="\n"
    )
    calls = []
    outcomes = outcomes or {"SEED": (0.5, 100), "GOOD": (1, 120)}

    async def run(self, prompt, working_dir=None):
        marker = (self.files_dir / "skills/main.md").read_text()
        calls.append((self.snapshot.id, marker, prompt))
        result = outcomes.get(marker, (0, 100))
        if isinstance(result, Trajectory):
            return result
        score, cost = result
        return Trajectory(
            output=str(score),
            usage={} if cost is None else {"total_tokens": cost},
            messages=(
                {
                    "type": "Trace",
                    "text": "evidence " * 5000 + "TAIL_EVIDENCE target-private-secret",
                },
            ),
        )

    monkeypatch.setattr(cls, "_run", run)
    target = cls(
        {
            "source_dir": str(source),
            "llm": {"model_name": "offline", "api_key": "target-private-secret"},
        }
    )
    worker = Worker(edit, relocate=relocate, critic=critic)
    algo = RRSI(
        {
            "T": 1,
            "k": 1,
            "m": 1,
            "delta": 0.02,
            "repair_rounds": 0,
            "smoke_n": 0,
            "component_signals": {"client_tool": [r"tools/"]},
            **options,
        },
        target=target,
        worker=worker,
        benchmark=Tasks(),
    )
    algo._run_dir = tmp_path / "run"
    for name in ("snapshots", "trajectories", "working"):
        (algo._run_dir / name).mkdir(parents=True)
    return algo, calls


def saved(algo, label):
    return [
        json.loads(p.read_text())
        for p in sorted((algo._run_dir / "trajectories").glob(f"*-{label}.json"))
    ]


def test_progress_labels_cover_rounds_candidates_repeats_smoke_and_test(
    monkeypatch, tmp_path
):
    markers = iter(["GOOD", "WORSE", "BEST", "LOW"])

    def edit(prompt, directory):
        (directory / "skills/main.md").write_text(next(markers))
        return [change()]

    algo, calls = setup(monkeypatch, tmp_path, edit, T=2, m=2, k=2, smoke_n=1)
    output = StringIO()

    async def scenario():
        with progress_session("log", stream=output):
            final = await algo.evolve()
            with progress_scope("final-test"):
                await algo._evaluate(final, algo.benchmark.test, label="test")

    asyncio.run(scenario())
    log = output.getvalue()
    assert "RRSI | repeats=2 | baseline | tasks" in log
    for repeat in (1, 2):
        assert f"[repeat={repeat}/2]:finished" in log
    for step in (1, 2):
        for variant in (1, 2):
            prefix = f"RRSI | round={step}/2 | candidate={variant}/2"
            assert f"{prefix} | smoke | tasks" in log
            assert f"{prefix} | repeats=2 | candidate | tasks" in log
    assert "tasks [####################] 4/4 (100%)" in log
    assert "rounds [####################] 2/2 (100%)" in log
    assert "final-test | test | tasks [####################] 1/1 (100%)" in log
    assert "Worker: analyzing trajectories" in log
    assert "Worker: critic review" in log
    assert "target-private-secret" not in log and "worker-private-secret" not in log
    assert calls[-1][2] == "test-private"
    assert all(prompt != "test-private" for _, _, prompt in calls[:-1])


@pytest.mark.parametrize("cls", [MiniAgent, ClaudeCode])
@pytest.mark.parametrize("relocate", [False, True])
def test_full_history_siblings_and_heldout_boundaries(
    monkeypatch, tmp_path, cls, relocate
):
    markers = iter(["GOOD", "WORSE", "BEST", "LOW"])
    parents, contexts = [], []

    def edit(prompt, directory):
        rows = records(prompt, directory)
        text = json.dumps(rows)
        assert "TAIL_EVIDENCE" in text
        assert (
            "target-private-secret" not in text and "worker-private-secret" not in text
        )
        assert "dev-private" not in text and "test-private" not in text
        context = [row for row in rows if "history" in row][-1]
        contexts.append(context)
        parents.append((directory / "skills/main.md").read_text())
        assert (
            not context["history"] if len(parents) < 3 else len(context["history"]) == 2
        )
        (directory / "skills/main.md").write_text(next(markers), newline="\n")
        return [change()]

    algo, calls = setup(
        monkeypatch,
        tmp_path,
        edit,
        cls=cls,
        relocate=relocate,
        T=2,
        m=2,
        outcomes={
            "SEED": (0.5, 100),
            "GOOD": (0.8, 110),
            "WORSE": (0.1, 90),
            "BEST": (1, 120),
            "LOW": (0.79, 10),
        },
    )
    seed = algo.target.snapshot
    result = asyncio.run(algo.evolve())
    assert parents == ["SEED", "SEED", "GOOD", "GOOD"]
    assert contexts[0] == contexts[1] and contexts[2] == contexts[3]
    assert contexts[2]["history"][1]["attribution"] is not None
    assert result.files["skills/main.md"].content == b"BEST"
    history = saved(algo, "edit-history")
    assert [row["outcome"] for row in history] == [
        "ACCEPTED",
        "REJECTED",
        "ACCEPTED",
        "LOST",
    ]
    summaries = saved(algo, "round-summary")
    assert [row["current_score"] for row in summaries] == [0.8, 1.0]
    assert [row["best_score"] for row in summaries] == [0.8, 1.0]
    assert [row["gain_from_baseline"] for row in summaries] == pytest.approx([0.3, 0.5])
    assert all(row["accepted_candidate"] for row in summaries)
    assert summaries[-1]["best_snapshot_id"] == result.id
    assert {task for _, _, task in calls} == {"train-0", "train-1"}
    assert len(calls) == 10
    assert history[0]["parent_id"] == history[1]["parent_id"] == seed.id
    assert (
        history[2]["parent_id"] == history[3]["parent_id"] == history[0]["candidate_id"]
    )
    assert result.files["tools/binary"] == seed.files["tools/binary"]
    assert not any(path.startswith("rrsi-evidence-") for path in result.files)
    assert not list((algo._run_dir / "working").iterdir())
    assert (
        Snapshot.load(algo._run_dir / "snapshots" / f"{result.id}.json").id == result.id
    )


def test_cost_saving_can_select_regression_and_next_floor_uses_best(
    monkeypatch, tmp_path, caplog
):
    versions = iter(["CHEAP", "TOO-LOW"])

    def edit(prompt, directory):
        (directory / "skills/main.md").write_text(next(versions), newline="\n")
        return [change()]

    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        T=2,
        delta=0.03,
        w_s=0.0,
        outcomes={"SEED": (0.8, 100), "CHEAP": (0.78, 50), "TOO-LOW": (0.76, 1)},
    )
    initial_id = algo.target.snapshot.id
    with caplog.at_level("INFO", logger="flashrsi.algorithms.rrsi"):
        result = asyncio.run(algo.evolve())
    assert result.files["skills/main.md"].content == b"CHEAP"
    assert [row["accepted"] for row in saved(algo, "edit-history")] == [True, False]
    final = saved(algo, "decision")[-1]
    assert final["best_score"] == 0.8 and final["score"] == 0.78
    summaries = saved(algo, "round-summary")
    assert [row["round"] for row in summaries] == [1, 2]
    assert [row["accepted_candidate"] for row in summaries] == [True, False]
    assert all(row["best_score"] == 0.8 for row in summaries)
    assert all(row["current_score"] == 0.78 for row in summaries)
    assert all(row["best_snapshot_id"] == initial_id for row in summaries)
    assert all(row["current_snapshot_id"] == result.id for row in summaries)
    assert "round=1/2 complete | evolve current=78.00% best=80.00%" in caplog.text
    assert "round=2/2 complete | evolve current=78.00% best=80.00%" in caplog.text


@pytest.mark.parametrize(
    "problem",
    [
        "critic",
        "budget",
        "undeclared",
        "tag",
        "syntax",
        "binary",
        "unchanged",
        "credential",
    ],
)
def test_bad_drafts_are_never_evaluated(monkeypatch, tmp_path, problem):
    def edit(prompt, directory):
        if problem == "unchanged":
            return [change()]
        (directory / "skills/main.md").write_text(
            "GOOD worker-private-secret" if problem == "credential" else "GOOD",
            newline="\n",
        )
        if problem in ("undeclared", "syntax"):
            (directory / "tools/runtime.py").write_text(
                "value = (" if problem == "syntax" else "value = 1\n", newline="\n"
            )
        if problem == "binary":
            (directory / "tools/binary").write_bytes(b"\x00\xfe")
        if problem == "budget":
            return [change(), change()]
        if problem == "tag":
            return [change(component="unknown")]
        if problem == "syntax":
            return [change(), change(component="client_tool", path="tools/runtime.py")]
        if problem == "binary":
            return [change(), change(component="client_tool", path="tools/binary")]
        return [change()]

    options = {"b_min": 1, "b_max": 1} if problem == "budget" else {}
    algo, calls = setup(
        monkeypatch,
        tmp_path,
        edit,
        critic=lambda prompt: (
            {"verdict": "reject", "reasons": ["suite-specific"]}
            if problem == "critic"
            else {"verdict": "accept"}
        ),
        **options,
    )
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    assert len(calls) == 2
    assert not saved(algo, "candidate")
    assert saved(algo, "smoke-gate") if problem == "syntax" else saved(algo, "repair")


def test_critic_repair_is_cumulative_from_same_parent(monkeypatch, tmp_path):
    count = 0

    def edit(prompt, directory):
        nonlocal count
        count += 1
        if count == 1:
            assert next(directory.rglob("current-diff.txt")).read_text() == ""
            (directory / "skills/main.md").write_text("BAD", newline="\n")
        else:
            assert (directory / "skills/main.md").read_text() == "BAD"
            assert "suite-specific" in prompt
            repair_diff = next(directory.rglob("current-diff.txt"))
            assert repair_diff.relative_to(directory).as_posix() in prompt
            assert "FILE skills/main.md" in repair_diff.read_text()
            assert "+BAD" in repair_diff.read_text()
            (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    verdicts = iter(["reject", "accept"])
    algo, calls = setup(
        monkeypatch,
        tmp_path,
        edit,
        repair_rounds=1,
        critic=lambda prompt: {
            "verdict": next(verdicts),
            "reasons": ["suite-specific"],
        },
    )
    seed = algo.target.snapshot
    result = asyncio.run(algo.evolve())
    assert (
        result.parent_id == seed.id
        and result.files["skills/main.md"].content == b"GOOD"
    )
    assert len(calls) == 4 and count == 2


def test_manifest_repair_receives_retained_diff_without_committing_evidence(
    monkeypatch, tmp_path
):
    count = 0

    def edit(prompt, directory):
        nonlocal count
        count += 1
        if count == 1:
            (directory / "skills/main.md").write_text("GOOD", newline="\n")
            (directory / "tools/runtime.py").write_text("value = 1\n", newline="\n")
            return [change()]
        assert "missing paths" in prompt
        difference = next(directory.rglob("current-diff.txt")).read_text()
        assert "FILE skills/main.md" in difference and "+GOOD" in difference
        assert "FILE tools/runtime.py" in difference and "+value = 1" in difference
        return [change(paths=["skills/main.md", "tools/runtime.py"])]

    algo, calls = setup(monkeypatch, tmp_path, edit, repair_rounds=1)
    result = asyncio.run(algo.evolve())
    assert count == 2 and len(calls) == 4
    assert result.files["tools/runtime.py"].content == b"value = 1\n"
    assert not any("rrsi-evidence-" in path for path in result.files)


def test_stall_requires_actual_untried_component_and_history_prunes_losers(
    monkeypatch, tmp_path
):
    count = 0

    def edit(prompt, directory):
        nonlocal count
        count += 1
        context = [row for row in records(prompt, directory) if "history" in row][-1]
        if count == 2:
            assert context["stalled"]
            assert context["untried"] == sorted(COMPONENTS - {"skill"})
            assert context["prune"][0]["component"] == "skill"
            assert context["history"][0]["outcome"] == "REJECTED"
        (directory / "skills/main.md").write_text(f"UNHELPFUL-{count}", newline="\n")
        return [change()]

    algo, calls = setup(monkeypatch, tmp_path, edit, T=2, w=1, m_draft=1)
    asyncio.run(algo.evolve())
    assert (
        len(calls) == 4
    )  # Baseline plus first measured candidate; reserved second is blocked.
    history = saved(algo, "edit-history")
    assert history[-1]["delta_S"] is None
    assert "reserved slot" in saved(algo, "repair")[-1]["objection"]


def test_zero_score_pruning_and_structural_novelty_are_measured(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "tools/runtime.py").write_text("value = 1\n", newline="\n")
        return [change(component="client_tool", path="tools/runtime.py")]

    algo, calls = setup(monkeypatch, tmp_path, edit, w_s=0)
    result = asyncio.run(algo.evolve())
    assert result.files["tools/runtime.py"].content == b"value = 1\n"
    assert saved(algo, "edit-history")[0]["novelty"] == 1
    assert len(calls) == 4


def test_pruning_accepted_machinery_is_a_measured_cost_saving(monkeypatch, tmp_path):
    count = 0

    def edit(prompt, directory):
        nonlocal count
        count += 1
        if count == 1:
            (directory / "tools/runtime.py").write_text("value = 1\n", newline="\n")
            return [change(component="client_tool", path="tools/runtime.py")]
        context = [row for row in records(prompt, directory) if "history" in row][-1]
        machinery = context["prune"][0]["accepted_edits"]
        assert machinery[0]["component"] == "client_tool"
        assert machinery[0]["delta_S"] == 0
        (directory / "tools/runtime.py").unlink()
        return [
            change(component="client_tool", path="tools/runtime.py", operation="prune")
        ]

    algo, calls = setup(monkeypatch, tmp_path, edit, T=2, w_s=0.0)
    original = MiniAgent._run

    async def run(self, prompt, working_dir=None):
        result = await original(self, prompt, working_dir)
        return replace(
            result,
            usage={
                "total_tokens": 100 if "tools/runtime.py" in self.snapshot.files else 50
            },
        )

    monkeypatch.setattr(MiniAgent, "_run", run)
    result = asyncio.run(algo.evolve())
    assert "tools/runtime.py" not in result.files
    assert len(calls) == 6
    history = saved(algo, "edit-history")
    assert history[1]["operation"] == "prune" and history[1]["accepted"]
    assert history[1]["delta_C"] == -0.5 and history[1]["novelty"] == 0


@pytest.mark.parametrize(
    "usage, expected",
    [
        ({"total_tokens": 120, "input_tokens": 90, "output_tokens": 30}, 120),
        (
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 7,
            },
            25,
        ),
        (
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
            15,
        ),
        (
            {
                "model_calls": [
                    {"role": "policy", "usage": {"total_tokens": 10}},
                    {"role": "side", "usage": {"total_tokens": 15}},
                ]
            },
            25,
        ),
        ({"model_usage": {"first": {"input_tokens": 10, "output_tokens": 5}}}, 15),
        (
            {
                "total_tokens": 15,
                "input_tokens": 10,
                "output_tokens": 5,
                "model_usage": {
                    "policy": {"inputTokens": 10, "outputTokens": 5},
                    "child": {
                        "inputTokens": 20,
                        "outputTokens": 7,
                        "cacheCreationInputTokens": 3,
                        "cacheReadInputTokens": 11,
                    },
                },
            },
            56,
        ),
        (
            {
                "total_tokens": 15,
                "model_usage": {
                    "policy": {"inputTokens": 10, "outputTokens": 5},
                    "child": {"inputTokens": 20},
                },
            },
            None,
        ),
        ({"total_tokens": 15, "model_usage": {}}, 15),
        ({"model_calls": [{"usage": {"total_tokens": 1}}, {"usage": {}}]}, None),
        ({"input_tokens": 10}, None),
        ({}, None),
        ({"total_tokens": True}, None),
        ({"total_tokens": float("nan")}, None),
        ({"total_tokens": -1}, None),
    ],
)
def test_token_accounting(usage, expected):
    assert tokens(usage) == expected


@pytest.mark.parametrize("complete", [True, False])
def test_subagent_cost_controls_candidate_acceptance(monkeypatch, tmp_path, complete):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("DELEGATED", newline="\n")
        (directory / "tools/runtime.py").write_text("value = 1\n", newline="\n")
        return [change(), change(component="client_tool", path="tools/runtime.py")]

    policy = {"inputTokens": 100, "outputTokens": 10}
    child = {"inputTokens": 1000}
    if complete:
        child["outputTokens"] = 200
    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        b_min=2,
        w_s=0.0,
        outcomes={
            "SEED": Trajectory(
                output="0.5",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "model_usage": {"policy": policy},
                },
            ),
            "DELEGATED": Trajectory(
                output="0.51",
                usage={
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "model_usage": {"policy": policy, "child": child},
                },
            ),
        },
    )
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    measurements = saved(algo, "measurement")
    baseline = measurements[0]
    assert baseline["cost"] == 110
    if complete:
        assert measurements[1]["cost"] == 1310
    else:
        assert measurements[1]["cost"] is None
        assert measurements[1]["cost_accounting"]["observed_mean_tokens"] == 1110
        assert measurements[1]["cost_accounting"]["trials_partial"] == 2
    decision = saved(algo, "edit-history")[0]
    assert not decision["accepted"]
    if complete:
        assert decision["delta_C"] == pytest.approx(1200 / 110)
        assert decision["reason"] == "within-band score/cost/novelty rule"
    else:
        assert "token accounting" in decision["reason"]


@pytest.mark.parametrize(
    "score,accepted", [(0.49, False), (0.5, False), (0.51, False), (1, True)]
)
def test_missing_candidate_usage_requires_clear_gain(
    monkeypatch, tmp_path, score, accepted
):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("MISSING", newline="\n")
        return [change()]

    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        outcomes={"SEED": (0.5, 100), "MISSING": (score, None)},
    )
    seed = algo.target.snapshot
    assert (asyncio.run(algo.evolve()).id != seed.id) is accepted
    decision = saved(algo, "edit-history")[0]
    assert "token accounting" in decision["reason"]
    assert decision["delta_C"] is None
    assert decision["cost_comparison"] == "unavailable"


def test_failed_trials_keep_denominator_even_if_grade_would_pass(monkeypatch, tmp_path):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("CRASH", newline="\n")
        return [change()]

    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        invalid_missing_frac=1,
        outcomes={
            "SEED": (0.5, 100),
            "CRASH": Trajectory(
                status="error",
                output="1",
                error_message="crash",
                usage={"total_tokens": 10},
            ),
        },
    )
    seed = algo.target.snapshot
    assert asyncio.run(algo.evolve()).id == seed.id
    measurement = saved(algo, "measurement")[-1]
    assert measurement["score"] == 0 and len(measurement["evidence"]) == 2
    assert all(scores == [0] for scores in measurement["per_task"].values())


def test_direct_use_creates_and_removes_reference_records(monkeypatch, tmp_path):
    def edit(prompt, directory):
        assert records(prompt, directory)
        (directory / "skills/main.md").write_text("GOOD", newline="\n")
        return [change()]

    algo, _ = setup(monkeypatch, tmp_path, edit)
    algo._run_dir = None
    assert asyncio.run(algo.evolve()).files["skills/main.md"].content == b"GOOD"
    assert algo._run_dir is None


def test_bootstrap_noise_is_zero_without_within_task_variation():
    assert noise_band({"a": [0, 0], "b": [1, 1]}, 2, 7) == 0


@pytest.mark.parametrize(
    "score, cost, best, accepted, component, expected",
    [
        (0.9, 120, 0.8, set(), "prompt", True),
        (0.9, 1200, 0.8, set(), "prompt", False),
        (0.79, 10, 0.9, set(), "skill", False),
        (0.79, 50, 0.8, set(), "prompt", True),
        (0.8, 100, 0.8, set(), "skill", True),
        (0.8, 100, 0.8, {"skill"}, "skill", False),
        (0.8, 100, 0.8, set(), "prompt", False),
    ],
)
def test_selection_uses_best_floor_and_relative_token_cost(
    score, cost, best, accepted, component, expected
):
    config = Config({**RRSI._defaults, "w_s": 0})
    decision = judge(
        {"score": score, "cost": cost},
        {"score": 0.8, "cost": 100},
        best,
        0.03,
        [change(component)],
        accepted,
        config,
    )
    assert decision["admissible"] is expected


def test_unmeasured_edits_do_not_count_as_tried_or_pruning_evidence():
    history = [{**change(), "t": 0, "delta_S": None, "accepted": False}]
    config = Config({**RRSI._defaults, "w": 1})
    value = directives(history, [0.5, 0.5], 1, 0.02, config)
    assert value == {"stalled": True, "untried": sorted(COMPONENTS), "prune": []}


@pytest.mark.parametrize(
    "options, match",
    [
        ({"T": True}, "algorithm.T"),
        ({"k": 0}, "algorithm.k"),
        ({"b_min": 5, "b_max": 4}, "b_min"),
        ({"m_draft": 2}, "m_draft"),
        ({"delta": -1}, "delta"),
        ({"delta": None}, "k >= 2"),
        ({"invalid_missing_frac": 1.1}, "invalid_missing_frac"),
        ({"component_signals": {"skill": ["["]}}, "invalid component signal"),
    ],
)
def test_config_preconditions(monkeypatch, tmp_path, options, match):
    with pytest.raises(ValueError, match=match):
        setup(monkeypatch, tmp_path, lambda *_: [], **options)


def test_missing_baseline_usage_continues_search(monkeypatch, tmp_path):
    algo, calls = setup(
        monkeypatch, tmp_path, lambda *_: [], outcomes={"SEED": (0.5, None)}
    )
    asyncio.run(algo.evolve())
    assert len(calls) == 2 and algo.worker.prompts
    baseline = saved(algo, "measurement")[0]
    assert baseline["cost"] is None
    assert baseline["cost_accounting"]["trials_missing"] == 2
    assert saved(algo, "decision")


@pytest.mark.parametrize(
    "usage,observed",
    [
        ({}, None),
        ({"model_calls": [{"usage": {"total_tokens": 25}}, {"usage": {}}]}, 25),
        ({"total_tokens": 25, "accounting_complete": False}, 25),
        ({"total_tokens": 0}, 0),
    ],
)
def test_incomplete_baseline_can_calibrate_and_accept_improvement(
    monkeypatch, tmp_path, usage, observed
):
    def edit(prompt, directory):
        (directory / "skills/main.md").write_text("GOOD")
        return [change()]

    algo, _ = setup(
        monkeypatch,
        tmp_path,
        edit,
        k=2,
        delta=None,
        outcomes={"SEED": Trajectory(output="0.5", usage=usage), "GOOD": (1, 120)},
    )
    assert asyncio.run(algo.evolve()).files["skills/main.md"].content == b"GOOD"
    baseline = saved(algo, "measurement")[0]
    assert baseline["cost"] is None
    assert baseline["cost_accounting"]["observed_tokens"] == (observed or 0) * 4
    assert saved(algo, "calibration")
    assert saved(algo, "edit-history")[0]["delta_C"] is None
    assert saved(algo, "decision")[0]["cost_accounting"]["complete"] is True


@pytest.mark.parametrize("label", ["baseline", "candidate"])
@pytest.mark.parametrize("errors", [3, 4])
def test_failed_trial_limit_is_preserved_even_with_missing_usage(
    monkeypatch, tmp_path, label, errors
):
    algo, _ = setup(monkeypatch, tmp_path, lambda *_: [])
    algo.benchmark._splits["train"] = tuple(Task(str(i), str(i)) for i in range(20))

    async def run(self, prompt, working_dir=None):
        return Trajectory(
            output="1",
            status="error" if int(prompt) < errors else "finished",
            error_message="timeout" if int(prompt) < errors else None,
            usage={} if int(prompt) < errors else {"total_tokens": 100},
        )

    monkeypatch.setattr(MiniAgent, "_run", run)
    measurement = algo._measure(algo.target.snapshot, label)
    if errors > 3:
        with pytest.raises(ValueError, match="too many failed trials"):
            asyncio.run(measurement)
    else:
        result = asyncio.run(measurement)
        assert result["score"] == pytest.approx(0.85)
        assert result["cost"] is None
    recorded = saved(algo, "measurement")[0]
    assert recorded["error_trials"] == errors
    assert recorded["cost_accounting"]["trials_missing"] == errors


def test_partial_usage_preserves_known_counters_without_making_total_complete():
    assert _token_accounting({"total_tokens": 20, "accounting_complete": False}) == (
        20,
        False,
    )
    assert _token_accounting(
        {
            "model_calls": [
                {"usage": {"prompt_tokens": 100, "completion_tokens": 20}},
                {"usage": {"prompt_tokens": 5}},
            ]
        }
    ) == (125, False)
    assert tokens({"total_tokens": 20, "accounting_complete": False}) is None


def test_incomplete_cost_comparison_still_respects_domain_guard():
    result = judge(
        {"score": 1, "cost": None, "guard_metrics": {"safety": 0}},
        {"score": 0.5, "cost": 100, "guard_metrics": {"safety": 1}},
        0.5,
        0.02,
        [change()],
        set(),
        Config({**RRSI._defaults, "guards": {"safety": {"max_drop": 0.1}}}),
    )
    assert result["admissible"] is False
    assert result["delta_C"] is None
    assert "domain guard" in result["reason"]


def test_automatic_calibration_uses_repeated_trials(monkeypatch, tmp_path):
    algo, calls = setup(monkeypatch, tmp_path, lambda *_: [], k=2, delta=None)
    asyncio.run(algo.evolve())
    assert len(calls) == 4
    assert (
        saved(algo, "calibration")[0]["source"]
        == "bootstrap within-task baseline trials"
    )


def test_missing_files_in_manifest_are_not_a_valid_edit():
    with pytest.raises(ValueError, match="actually change"):
        declaration(
            json.dumps({"edits": [change(path="skills/unchanged.md")]}),
            ["skills/main.md"],
            1,
        )


@pytest.mark.parametrize("prefix", ["", "Preserve `<EDITS>` output.\n"])
def test_manifest_objection_names_changes_omitted_from_repair(prefix):
    with pytest.raises(ValueError, match="relative to the incumbent") as error:
        declaration(
            prefix + "<EDITS>" + json.dumps({"edits": [change()]}) + "</EDITS>",
            ["skills/main.md", "tools/runtime.py", "agent.yaml"],
            1,
        )
    assert 'missing paths: ["agent.yaml", "tools/runtime.py"]' in str(error.value)
