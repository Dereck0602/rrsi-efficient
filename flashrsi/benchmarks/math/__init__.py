"""Native MATH records, scored with Math-Verify 0.9.0 expression equivalence."""

import hashlib
import importlib.util
import json
import math
import subprocess
import sys
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from types import MappingProxyType

from flashrsi.benchmarks._data import _split_tasks
from flashrsi.benchmarks._native import NativeBenchmark
from flashrsi.benchmarks.math._grade import _last_boxed


class MATH(NativeBenchmark):
    name = "math"
    description = "MATH competition problems with mathematical equivalence grading."
    primary_metric = "accuracy"
    metadata = MappingProxyType(
        {
            "source": "EleutherAI/hendrycks_math",
            "revision": "21a5633873b6a120296cce3e2df9d5550074f4a3",
            "configs": (
                "algebra",
                "counting_and_probability",
                "geometry",
                "intermediate_algebra",
                "number_theory",
                "prealgebra",
                "precalculus",
            ),
            "scorer": "math-verify==0.9.0",
            "answer_extraction": "last-boxed",
        }
    )

    def __init__(self, config):
        super().__init__(config)
        variant = self.config.get("variant", "math")
        if variant == "math500":
            self.metadata = MappingProxyType(
                {
                    "source": "HuggingFaceH4/MATH-500",
                    "revision": "6e4ed1a2a79af7d8630a6b768ec859cb5af4d3be",
                    "scorer": "math-verify==0.9.0",
                    "answer_extraction": "last-boxed",
                }
            )
        elif variant != "math":
            raise ValueError("benchmark.variant must be math or math500")
        self._timeout = self.config.get("grading_timeout", 15)
        if (
            type(self._timeout) not in (int, float)
            or not math.isfinite(self._timeout)
            or self._timeout <= 0
        ):
            raise ValueError("benchmark.grading_timeout must be positive and finite")

    def load(self):
        if self._splits is not None:
            return self._splits
        source_split = self.config.get("evolution_split")
        if source_split is None:
            if any(
                self.config.get(key) is not None
                for key in ("evolution_limit", "evolution_subjects")
            ):
                raise ValueError("MATH evolution options require evolution_split")
            return super().load()
        if source_split not in ("train", "test"):
            raise ValueError("MATH evolution_split must be train or test")
        subjects = self.config.get("evolution_subjects", type(self).metadata["configs"])
        if (
            not isinstance(subjects, (list, tuple))
            or not subjects
            or any(
                subject not in type(self).metadata["configs"] for subject in subjects
            )
            or len(set(subjects)) != len(subjects)
        ):
            raise ValueError(
                "MATH evolution_subjects must name distinct native subjects"
            )

        downstream = tuple(self._iter_tasks())
        splits = _split_tasks(downstream, self.config)
        ids = {t.task_id for t in downstream}
        questions = {t.metadata["question_checksum"] for t in downstream}
        external = MATH({"grading_timeout": self._timeout})
        external.metadata = MappingProxyType(
            {**external.metadata, "configs": tuple(subjects)}
        )
        digest = hashlib.sha256()
        count = 0

        def evolution_pool():
            nonlocal count
            for task in external._iter_tasks():
                if task.metadata["source_split"] != source_split:
                    continue
                # MATH-500 reuses original MATH test questions. Check the whole
                # chosen native split, including questions outside the tiny run.
                if (
                    task.task_id in ids
                    or task.metadata["question_checksum"] in questions
                ):
                    raise ValueError("MATH evolution pool overlaps downstream tasks")
                digest.update(
                    (
                        task.task_id + ":" + task.metadata["task_checksum"] + "\n"
                    ).encode()
                )
                count += 1
                yield task

        evolution = _split_tasks(
            evolution_pool(),
            {
                "split": {"train": 1, "dev": 0, "test": 0},
                "limit": self.config.get("evolution_limit"),
                "seed": self.config.get("seed", 0),
            },
        )["train"]
        self.metadata = MappingProxyType(
            {
                **self.metadata,
                "evolution_source": {
                    **external.metadata,
                    "native_split": source_split,
                    "experiment_role": "external_evolution",
                    "pool_size": count,
                    "pool_checksum": digest.hexdigest(),
                },
            }
        )
        self._rows_by_id.update(
            {task.task_id: external._rows_by_id[task.task_id] for task in evolution}
        )
        self._splits = MappingProxyType({**splits, "train": evolution})
        self._tasks_by_id = {
            task.task_id: task for group in self._splits.values() for task in group
        }
        return self._splits

    def _check(self):
        if importlib.util.find_spec("math_verify") is None:
            raise ValueError("Install flashrsi-lite[all] for MATH grading")
        if version("math-verify") != "0.9.0":
            raise ValueError(
                "MATH grading requires math-verify==0.9.0; install flashrsi-lite[all]"
            )

    def _iter_tasks(self):
        for split, row in self._source_rows():
            problem = row.get("problem")
            gold = row.get("answer") or row.get("solution")
            if (
                not isinstance(problem, str)
                or not problem.strip()
                or not isinstance(gold, str)
                or not gold.strip()
            ):
                raise ValueError(
                    "MATH requires native problem and answer or solution fields"
                )
            task_id = (
                row.get("unique_id") or hashlib.sha256(problem.encode()).hexdigest()
            )
            if self.config.get("_prepared_splits"):
                task_id = row["id"]
            boxed = _last_boxed(gold)
            if boxed is not None and not boxed.strip():
                raise ValueError(
                    f"MATH task_id={task_id}: empty or malformed boxed reference answer"
                )
            yield self._register(
                row,
                split,
                task_id,
                problem + "\n\nGive your final answer in \\boxed{...}.",
                env="supplied_context",
                subject=row.get("subject", row.get("type")),
                level=row.get("level"),
                question_checksum=hashlib.sha256(
                    "".join(problem.split()).casefold().encode()
                ).hexdigest(),
            )

    def grade(self, trajs):
        self._check()
        graded = []
        for trajectory in trajs:
            row = self._row_for(trajectory)
            score = 0.0
            if trajectory.status == "finished":
                request = {
                    "gold": row.get("answer") or row["solution"],
                    "prediction": trajectory.output,
                }
                # Math-Verify's Windows timeout uses non-picklable local targets.
                # A bounded interpreter lets us use its native math rules on both OSes.
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-X",
                        "utf8",
                        str(Path(__file__).with_name("_grade.py")),
                    ],
                    input=json.dumps(request).encode(),
                    capture_output=True,
                    timeout=self._timeout,
                )
                if result.returncode:
                    raise ValueError(
                        "MATH scorer failed; check that the reference answer is parseable"
                    )
                value = json.loads(result.stdout)["correct"]
                if type(value) is not bool:
                    raise ValueError("Invalid MATH scorer verdict")
                score = float(value)
            graded.append(
                replace(trajectory, metrics={**trajectory.metrics, "accuracy": score})
            )
        return tuple(graded)


__all__ = ["MATH"]
