"""AIME 2025/2026 public question pools with deterministic integer scoring.

Pinned math-ai dataset mirrors; this is an explicit answer protocol, not a
symbolic-math or model-judged evaluator. An explicit different evolution_year
can supply external evolution tasks without changing the held-out year's splits.
"""

import hashlib
import json
import re
from types import MappingProxyType

from flashrsi.benchmarks._data import _split_tasks
from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.benchmarks.math._grade import _last_boxed
from flashrsi.core import Task

_SOURCES = {
    2025: {
        "source": "math-ai/aime25",
        "revision": "563bb8404243c5f09de6ec262f2db674fe5bce9b",
        "filename": "test.jsonl",
    },
    2026: {
        "source": "math-ai/aime26",
        "revision": "79037aebdb6580008fb960d17cb21fd3099083e3",
        "filename": "aime2026.jsonl",
    },
}


class AIME(QABenchmark):
    name = "aime"
    description = "AIME 2025/2026 competition math, exact integer accuracy."
    primary_metric = "accuracy"
    _default_scoring = "integer_exact"
    _scoring_modes = ("integer_exact",)
    metadata = MappingProxyType(
        {
            **_SOURCES[2026],
            "year": 2026,
            "splits": ("test",),
            "aggregation": "sample_mean",
            "answer_protocol": "final_integer_v1",
            "data_license": "Apache-2.0 (dataset card)",
        }
    )

    def __init__(self, config):
        super().__init__(config)
        self.year = self.config.get("year", 2026)
        if type(self.year) is not int or self.year not in _SOURCES:
            raise ValueError("AIME year must be 2025 or 2026 (integer)")
        self.metadata = MappingProxyType(
            {**self.metadata, **_SOURCES[self.year], "year": self.year}
        )

    def load(self):
        if self._splits is not None:
            return self._splits
        year = self.config.get("evolution_year")
        if year is None:
            if self.config.get("evolution_limit") is not None:
                raise ValueError("AIME evolution_limit requires evolution_year")
            return super().load()
        if type(year) is not int or year not in _SOURCES or year == self.year:
            raise ValueError("AIME evolution_year must be a supported different year")
        downstream = tuple(self._iter_tasks())
        splits = _split_tasks(downstream, self.config)
        # Load the other year's actual pinned public pool, not a second partition
        # or a caller-supplied evolution_source label for the held-out pool.
        external = AIME({"year": year})
        pool = tuple(external._iter_tasks())
        questions = {re.sub(r"\s+", "", t.question).casefold() for t in downstream}
        ids = {t.task_id for t in downstream}
        if any(
            t.task_id in ids or re.sub(r"\s+", "", t.question).casefold() in questions
            for t in pool
        ):
            raise ValueError("AIME external evolution pool overlaps downstream tasks")
        evolution = _split_tasks(
            pool,
            {
                "split": {"train": 1, "dev": 0, "test": 0},
                "limit": self.config.get("evolution_limit"),
                "seed": self.config.get("seed", 0),
            },
        )["train"]
        checksum = hashlib.sha256(
            json.dumps(
                [(t.task_id, t.question, t.ground_truth) for t in pool],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        self.metadata = MappingProxyType(
            {
                **self.metadata,
                "mode": self.mode,
                "scoring": self.scoring,
                "evolution_source": {
                    **external.metadata,
                    "pool_checksum": checksum,
                    "native_split": "test",
                    "experiment_role": "external_evolution",
                },
            }
        )
        self._splits = MappingProxyType({**splits, "train": evolution})
        self._tasks_by_id = {
            t.task_id: t for group in self._splits.values() for t in group
        }
        return self._splits

    def _source_metadata(self):
        if "dataset" in self.config:
            return super()._source_metadata()
        return self.metadata

    def _load_source_rows(self):
        if self.config.get("data_path") is not None:
            rows = super()._load_source_rows()
            metadata = {**self.metadata, "year": self.year}
            if self.config.get("_prepared_source"):
                metadata.pop("year", None)
                metadata["years"] = (2025, 2026)
            self.metadata = MappingProxyType(metadata)
            yield from rows
            return
        from huggingface_hub import hf_hub_download

        self.metadata = self._source_metadata()
        path = hf_hub_download(
            repo_id=self.metadata["source"],
            repo_type="dataset",
            revision=self.metadata["revision"],
            filename=self.metadata.get("filename", _SOURCES[self.year]["filename"]),
        )
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    yield "test", json.loads(line)

    def _task_from_row(self, row, source_split):
        year = row["native_year"] if self.config.get("_prepared_source") else self.year
        if not isinstance(row.get("problem"), str) or not row["problem"].strip():
            raise ValueError("AIME row requires a nonempty problem")
        task_id = row.get("id")
        if type(task_id) not in (int, str) or not str(task_id).strip():
            raise ValueError("AIME row requires an integer or nonempty string id")
        answer = row.get("answer")
        if type(answer) not in (int, str) or not re.fullmatch(
            r"[0-9]{1,3}", str(answer).strip()
        ):
            raise ValueError("AIME answer must be an integer from 0 to 999")
        answer = str(int(answer))
        return Task(
            task_id=f"aime{year}:{task_id}",
            question=(
                f"{row['problem']}\n\n"
                "Solve the problem. End with the integer answer from 0 to 999 "
                r"in \boxed{...}."
            ),
            ground_truth=answer,
            env="supplied_context",
            metadata={
                "answers": (answer,),
                "source_split": source_split,
                "year": year,
            },
        )

    def _score(self, prediction, task):
        final = _last_boxed(prediction)
        if final is None:
            lines = prediction.strip().splitlines()
            final = lines[-1] if lines else ""
        final = final.strip()
        correct = bool(re.fullmatch(r"[0-9]{1,3}", final)) and int(final) == int(
            task.ground_truth
        )
        return {"accuracy": float(correct)}

    def _zero_scores(self):
        return {"accuracy": 0.0}


__all__ = ["AIME"]
