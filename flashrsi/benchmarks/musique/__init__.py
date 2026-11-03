"""MuSiQue-Ans v1.0 loading and contextual short-answer grading."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from flashrsi.benchmarks._official_qa._musique_answer import AnswerMetric
from flashrsi.benchmarks._official_qa._musique_support import SupportMetric
from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.core.benchmark import Task


class MuSiQue(QABenchmark):
    """Answerable multi-hop QA, optionally scoring native supporting paragraphs.

    All supplied paragraphs remain visible; labels and decompositions stay private.
    """

    name = "musique"
    _modes = ("context", "answer_support")
    description = "MuSiQue-Ans v1.0 multi-hop QA over supplied Wikipedia paragraphs."
    metadata = MappingProxyType(
        {
            "source": "bdsaglam/musique",
            "config": "musique_ans_v1.0",
            "revision": "22873a405dd809893b22ada0b499299fb612d2df",
            "upstream": "https://github.com/StonyBrookNLP/musique",
            "split_policy": "user-configured",
            "scorer_revision": "922ac98f19a201998dbdae6d7f2887a5258dbdeb",
        }
    )

    _scorer_revision = metadata["scorer_revision"]

    def __init__(self, config):
        super().__init__(config)
        if self.mode == "answer_support" and self.scoring != "official":
            raise ValueError("MuSiQue answer_support requires official scoring")
        if self.mode == "answer_support":
            self.primary_metric = "answer_f1"
        self._supports_by_id = {}

    def load(self):
        if self._splits is not None:
            return self._splits
        source_split = self.config.get("evolution_split")
        if source_split is None:
            if self.config.get("evolution_limit") is not None:
                raise ValueError("MuSiQue evolution_limit requires evolution_split")
            return super().load()
        if source_split not in ("train", "dev"):
            raise ValueError("MuSiQue evolution_split must be train or dev")
        external = MuSiQue(
            {
                "dataset": {**type(self).metadata, "splits": (source_split,)},
                "mode": self.mode,
                "scoring": self.scoring,
            }
        )
        evolution = self._load_evolution(external, source_split)
        if self.mode == "answer_support":
            self._supports_by_id.update(
                {
                    task.task_id: external._supports_by_id[task.task_id]
                    for task in evolution
                }
            )
        return self._splits

    def _prepare_task(self, task, row):
        if self.mode == "context":
            return task
        paragraphs = row["paragraphs"]
        if any(
            type(p.get("idx")) is not int or type(p.get("is_supporting")) is not bool
            for p in paragraphs
        ) or len({p["idx"] for p in paragraphs}) != len(paragraphs):
            raise ValueError(
                f"MuSiQue task {task.task_id} has malformed paragraph indices/supports"
            )
        self._supports_by_id[task.task_id] = [
            p["idx"] for p in paragraphs if p["is_supporting"]
        ]
        context = "\n".join(
            f"[{p['idx']}] {p['title']}: {p['paragraph_text']}" for p in paragraphs
        )
        return replace(
            task,
            question=(
                "Answer the multi-hop question using the supplied paragraphs. "
                'Return only JSON: {"predicted_answer": "short answer", "predicted_support_idxs": [0, ...], "predicted_answerable": true}. '
                "Use the displayed paragraph indices for your supporting evidence.\n\n"
                f"Context:\n{context}\n\nQuestion:\n{row['question']}"
            ),
        )

    def _zero_scores(self):
        if self.mode == "answer_support":
            return {"answer_em": 0.0, "answer_f1": 0.0, "support_f1": 0.0}
        return super()._zero_scores()

    def _score(self, prediction, task):
        if self.scoring == "legacy":
            return super()._score(prediction, task)
        support = None
        if self.mode == "answer_support":
            try:
                value = json.loads(prediction)
                prediction, support = (
                    value["predicted_answer"],
                    value["predicted_support_idxs"],
                )
                if (
                    not isinstance(prediction, str)
                    or not isinstance(support, list)
                    or any(type(index) is not int for index in support)
                    or type(value["predicted_answerable"]) is not bool
                ):
                    return self._zero_scores()
            except (ValueError, KeyError, TypeError):
                return self._zero_scores()
        answer = AnswerMetric()
        answer(prediction, list(task.metadata["answers"]))
        em, f1 = answer.get_metric()
        if self.mode == "context":
            return {"exact_match": em, "f1": f1}
        evidence = SupportMetric()
        evidence(support, self._supports_by_id[task.task_id])
        return {
            "answer_em": em,
            "answer_f1": f1,
            "support_f1": evidence.get_metric()[1],
        }

    def _load_source_rows(self) -> Iterable[tuple[str, Mapping[str, Any]]]:
        if self.config.get("data_path") is not None:
            yield from super()._load_source_rows()
            return

        from huggingface_hub import hf_hub_download

        self.metadata = self._source_metadata()
        splits = self.metadata.get("splits", ("train", "dev"))
        if (
            not isinstance(splits, (list, tuple))
            or not splits
            or set(splits) - {"train", "dev"}
        ):
            raise ValueError("MuSiQue dataset.splits must select labeled train/dev")

        for split in splits:
            path = Path(
                hf_hub_download(
                    repo_id=self.metadata["source"],
                    filename=f"musique_ans_v1.0_{split}.jsonl",
                    repo_type="dataset",
                    revision=self.metadata["revision"],
                )
            )
            # The pinned files are JSONL; resolving an HF symlink may expose
            # an extensionless blob, which must not change their format.
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        yield split, json.loads(line)

    @staticmethod
    def _task_from_row(row: Mapping[str, Any], source_split: str) -> Task:
        task_id = row.get("id")
        question, answer = row.get("question"), row.get("answer")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("MuSiQue row has no non-empty id")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"MuSiQue row {task_id} has no question")
        if row.get("answerable") is not True:
            raise ValueError(f"MuSiQue-Ans requires an answerable row: {task_id}")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(f"MuSiQue row {task_id} has no answer")
        aliases = row.get("answer_aliases")
        if not isinstance(aliases, list) or any(
            not isinstance(alias, str) or not alias.strip() for alias in aliases
        ):
            raise ValueError(f"MuSiQue row {task_id} has malformed answer_aliases")
        paragraphs = row.get("paragraphs")
        if not isinstance(paragraphs, list) or not paragraphs:
            raise ValueError(f"MuSiQue row {task_id} has no paragraphs")
        lines = []
        for paragraph in paragraphs:
            if (
                not isinstance(paragraph, Mapping)
                or not isinstance(paragraph.get("title"), str)
                or not isinstance(paragraph.get("paragraph_text"), str)
            ):
                raise ValueError(f"MuSiQue row {task_id} has a malformed paragraph")
            lines.append(f"{paragraph['title']}: {paragraph['paragraph_text']}")
        context = "\n".join(lines)
        return Task(
            task_id=task_id,
            question=(
                "Answer the multi-hop question using the supplied Wikipedia context. "
                "Return only the short answer.\n\n"
                f"Context:\n{context}\n\nQuestion:\n{question}"
            ),
            ground_truth=answer,
            env="supplied_context",
            metadata={
                "answers": (answer, *aliases),
                "source_split": source_split,
                "question_checksum": hashlib.sha256(
                    "".join(question.split()).casefold().encode()
                ).hexdigest(),
            },
        )


__all__ = ["MuSiQue"]
