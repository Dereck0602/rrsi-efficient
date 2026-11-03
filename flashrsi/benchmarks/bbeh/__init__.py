"""BBEH (2025), using the upstream per-answer correctness evaluator.

Scorer: google-deepmind/bbeh at 80d12ca916b7158f22293fcf3144f4d3d854d4be.
The public pool is evaluation data; train/dev require an explicit partition.
"""

import hashlib
import json
import logging
import re
from collections import Counter
from types import MappingProxyType

from flashrsi.benchmarks._git import _download_git
from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.benchmarks.bbeh._official import evaluate_correctness
from flashrsi.core import Task


class BBEH(QABenchmark):
    name = "bbeh"
    description = (
        "BIG-Bench Extra Hard general reasoning with local correctness scoring."
    )
    primary_metric = "accuracy"
    _scoring_modes = ("official",)
    _scorer_revision = "80d12ca916b7158f22293fcf3144f4d3d854d4be"
    metadata = MappingProxyType(
        {
            "source": "BBEH/bbeh",
            "revision": "08e07a803851822c04399782ece3c4a07ce419f9",
            "upstream": "https://github.com/google-deepmind/bbeh",
            "splits": ("test",),
            "aggregation": "sample_mean",
            "data_license": "CC-BY-4.0",
        }
    )

    def __init__(self, config):
        super().__init__(config)
        self._duplicates = self.config.get("duplicate_questions", "error")
        if self._duplicates not in ("error", "exclude"):
            raise ValueError("BBEH duplicate_questions must be error or exclude")

    def load(self):
        if self._splits is not None:
            return self._splits
        tasks = self.config.get("evolution_bbh_tasks")
        if tasks is None:
            if self.config.get("evolution_limit") is not None:
                raise ValueError("BBEH evolution_limit requires evolution_bbh_tasks")
            return super().load()
        if (
            not isinstance(tasks, list)
            or not tasks
            or any(
                not isinstance(task, str)
                or re.fullmatch(r"[a-z][a-z0-9_]*", task) is None
                for task in tasks
            )
            or len(set(tasks)) != len(tasks)
        ):
            raise ValueError("BBEH evolution_bbh_tasks must list unique BBH task names")
        self._load_evolution(_BBH(tasks), "published_evaluation")
        return self._splits

    def _iter_tasks(self):
        tasks = tuple(super()._iter_tasks())
        counts = Counter(task.task_id for task in tasks)
        duplicate_ids = {task_id for task_id, count in counts.items() if count > 1}
        if duplicate_ids and self._duplicates == "error":
            raise ValueError(
                "BBEH contains duplicate questions, including ambiguous answer keys. "
                "Set benchmark.duplicate_questions: exclude to explicitly exclude "
                "every repeated question group before partitioning."
            )
        excluded = sum(counts[task_id] for task_id in duplicate_ids)
        self.metadata = MappingProxyType(
            {
                **self.metadata,
                "duplicate_questions": self._duplicates,
                "source_row_count": len(tasks),
                "excluded_duplicate_rows": excluded,
                "excluded_question_ids": tuple(sorted(duplicate_ids)),
            }
        )
        if excluded:
            logging.getLogger(__name__).warning(
                "BBEH explicitly excluded %d rows in %d repeated question groups; "
                "this is a filtered experiment pool, not the complete benchmark.",
                excluded,
                len(duplicate_ids),
            )
        # Exclude whole groups: keeping an arbitrary label from contradictory
        # rows would invent an answer key; retaining repeats can leak across splits.
        yield from (task for task in tasks if task.task_id not in duplicate_ids)

    def _load_source_rows(self):
        if self.config.get("data_path") is not None:
            return super()._load_source_rows()
        from huggingface_hub import hf_hub_download

        self.metadata = self._source_metadata()
        path = hf_hub_download(
            repo_id=self.metadata["source"],
            repo_type="dataset",
            revision=self.metadata["revision"],
            filename="bbeh_data.json",
        )
        # HF infers 'train' from the single JSON filename. These are evaluation
        # questions, so preserve their intended role instead of that inference.
        with open(path, encoding="utf-8") as stream:
            rows = json.load(stream)
        if not isinstance(rows, list):
            raise ValueError("BBEH bbeh_data.json must contain a list of records")
        return (("test", row) for row in rows)

    @staticmethod
    def _task_from_row(row, source_split):
        for field in ("task", "input", "target"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise ValueError(f"BBEH row requires a nonempty {field}")
        category, question, answer = row["task"], row["input"], row["target"]
        # The native rows have no ID. Exclude labels and partition roles so an
        # overlapping question is detected even when loaded from separate files.
        task_id = hashlib.sha256(
            json.dumps([category, question], ensure_ascii=False).encode()
        ).hexdigest()
        return Task(
            task_id=task_id,
            question=question,
            ground_truth=answer,
            env="supplied_context",
            metadata={
                "answers": (answer,),
                "source_split": source_split,
                "category": category,
                "question_checksum": hashlib.sha256(
                    re.sub(r"\s+", " ", question).strip().casefold().encode()
                ).hexdigest(),
            },
        )

    def _score(self, prediction, task):
        return {"accuracy": float(evaluate_correctness(prediction, task.ground_truth))}

    def _zero_scores(self):
        return {"accuracy": 0.0}


class _BBH(BBEH):
    """Read the separate publisher's raw examples without rewriting their text."""

    metadata = MappingProxyType(
        {
            "source": "https://github.com/suzgunmirac/BIG-Bench-Hard",
            "revision": "9ee07bd481feebf959a6b59d61ea57bdcf30964d",
            "data_license": "MIT",
            "answer_scoring": "BBEH correctness evaluator for external evolution",
        }
    )

    def __init__(self, tasks):
        super().__init__({})
        self.metadata = MappingProxyType({**self.metadata, "tasks": list(tasks)})

    def _load_source_rows(self):
        root = _download_git(
            self.metadata["source"] + ".git", self.metadata["revision"], ["bbh"]
        )
        for category in self.metadata["tasks"]:
            path = root / "bbh" / (category + ".json")
            if not path.is_file():
                raise ValueError(f"Unknown BBH evolution task: {category}")
            rows = json.loads(path.read_text(encoding="utf-8"))["examples"]
            for row in rows:
                yield "published_evaluation", {**row, "task": category}


__all__ = ["BBEH"]
