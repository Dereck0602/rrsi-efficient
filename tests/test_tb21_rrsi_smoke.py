"""The live smoke launcher must never evaluate the original held-out split."""

import runpy
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import pytest

from flashrsi.core.benchmark import Task

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/run_tb21_rrsi_smoke.py"


def benchmark():
    splits = {
        "train": (Task("one", "one"), Task("two", "two"), Task("three", "three")),
        "dev": (),
        "test": (Task("heldout", "heldout"),),
    }
    return SimpleNamespace(
        load=lambda: splits,
        _splits=splits,
        _tasks_by_id={t.task_id: t for rows in splits.values() for t in rows},
        metadata=MappingProxyType({"version": "2.1"}),
    )


def test_only_two_train_tasks_reach_evolution_and_final_integration_check():
    restrict = runpy.run_path(str(SCRIPT))["restrict_to_smoke_tasks"]
    source = benchmark()
    restrict(source, ("two", "one"))
    assert [t.task_id for t in source._splits["train"]] == ["two", "one"]
    assert source._splits["test"] == source._splits["train"]
    assert source._splits["dev"] == ()
    assert set(source._tasks_by_id) == {"one", "two"}
    assert source.metadata["final_test_reuses_train_tasks"] is True
    assert source.metadata["heldout_evaluated"] is False


@pytest.mark.parametrize("ids", [("one", "heldout"), ("one", "one"), ("one",)])
def test_invalid_selection_is_rejected_before_changing_splits(ids):
    restrict = runpy.run_path(str(SCRIPT))["restrict_to_smoke_tasks"]
    source = benchmark()
    original = source._splits
    with pytest.raises(ValueError, match="two distinct original train tasks"):
        restrict(source, ids)
    assert source._splits is original
