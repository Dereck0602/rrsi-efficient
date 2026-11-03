"""HotpotQA dataset loading and local answer grading."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from flashrsi.benchmarks._official_qa import _hotpot as scorer
from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.core.benchmark import Task

_PROMPT = (
    "Answer the multi-hop question using the supplied Wikipedia context. "
    "Return only the short answer.\n\nContext:\n{context}\n\nQuestion:\n{question}"
)


class HotpotQA(QABenchmark):
    """Contextual short answers or fullwiki retrieval with official evidence metrics.

    Native train/validation names are preserved. The user chooses experiment
    roles; historical sources and Unicode EM/F1 remain explicit options.
    """

    name = "hotpotqa"
    _modes = ("context", "fullwiki")
    description = "Multi-hop question answering over retrieved Wikipedia paragraphs."
    metadata = MappingProxyType(
        {
            "source": "hotpotqa/hotpot_qa",
            "config": "fullwiki",
            "revision": "1908d6afbbead072334abe2965f91bd2709910ab",
            "splits": ("train", "validation"),
            "scorer_revision": "3635853403a8735609ee997664e1528f4480762a",
        }
    )

    _scorer_revision = metadata["scorer_revision"]

    def __init__(self, config):
        super().__init__(config)
        if self.mode == "fullwiki" and self.scoring != "official":
            raise ValueError("HotpotQA fullwiki requires official scoring")
        if self.mode == "fullwiki":
            self.primary_metric = "joint_f1"
        self._supports_by_id = {}

    def load(self):
        if self._splits is not None:
            return self._splits
        source_split = self.config.get("evolution_split")
        if source_split is None:
            if self.config.get("evolution_limit") is not None:
                raise ValueError("HotpotQA evolution_limit requires evolution_split")
            return super().load()
        if source_split not in type(self).metadata["splits"]:
            raise ValueError("HotpotQA evolution_split must be train or validation")

        external = HotpotQA(
            {
                "dataset": {**type(self).metadata, "splits": (source_split,)},
                "mode": self.mode,
                "scoring": self.scoring,
            }
        )
        evolution = self._load_evolution(external, source_split)
        if self.mode == "fullwiki":
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
        support = row.get("supporting_facts")
        if isinstance(support, Mapping):
            titles, indices = support.get("title"), support.get("sent_id")
            if (
                not isinstance(titles, list)
                or not isinstance(indices, list)
                or len(titles) != len(indices)
            ):
                raise ValueError(
                    f"HotpotQA task {task.task_id} has malformed supporting_facts"
                )
            support = list(zip(titles, indices, strict=True))
        if not isinstance(support, list) or any(
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not isinstance(item[0], str)
            or type(item[1]) is not int
            or item[1] < 0
            for item in support
        ):
            raise ValueError(
                f"HotpotQA task {task.task_id} has malformed supporting_facts"
            )
        self._supports_by_id[task.task_id] = support
        return replace(
            task,
            env="hotpotqa_fullwiki",
            question=(
                "Answer the multi-hop question by searching the configured Wikipedia corpus. "
                'Return only JSON: {"answer": "short answer", "sp": [["article title", 0], ...]}. '
                "Each supporting fact is an article title and its zero-based sentence index.\n\n"
                f"Question:\n{row['question']}"
            ),
        )

    def _zero_scores(self):
        scores = super()._zero_scores()
        if self.mode == "fullwiki":
            scores.update(
                {
                    k: 0.0
                    for k in (
                        "prec",
                        "recall",
                        "sp_em",
                        "sp_f1",
                        "sp_prec",
                        "sp_recall",
                        "joint_em",
                        "joint_f1",
                        "joint_prec",
                        "joint_recall",
                    )
                }
            )
        return scores

    def _score(self, prediction, task):
        if self.scoring == "legacy":
            return super()._score(prediction, task)
        if self.mode == "context":
            em = scorer.exact_match_score(prediction, task.ground_truth)
            return {
                "exact_match": float(em),
                "f1": scorer.f1_score(prediction, task.ground_truth)[0],
            }
        try:
            value = json.loads(prediction)
            if not isinstance(value, dict):
                return self._zero_scores()
        except ValueError:
            return self._zero_scores()
        answer, support = value.get("answer"), value.get("sp")
        answer_valid = isinstance(answer, str)
        support_valid = isinstance(support, list) and all(
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and type(item[1]) is int
            and item[1] >= 0
            for item in support
        )
        scores = {"em": 0.0, **self._zero_scores()}
        # The official evaluator scores each present component independently;
        # a missing evidence prediction must not erase a valid answer score.
        if answer_valid:
            em, precision, recall = scorer.update_answer(
                scores, answer, task.ground_truth
            )
        if support_valid:
            sp_em, sp_prec, sp_recall = scorer.update_sp(
                scores, support, self._supports_by_id[task.task_id]
            )
        if answer_valid and support_valid:
            joint_prec, joint_recall = precision * sp_prec, recall * sp_recall
            scores.update(
                joint_em=float(em * sp_em),
                joint_prec=joint_prec,
                joint_recall=joint_recall,
                joint_f1=2 * joint_prec * joint_recall / (joint_prec + joint_recall)
                if joint_prec + joint_recall
                else 0.0,
            )
        scores["exact_match"] = scores.pop("em")
        return scores

    @staticmethod
    def _task_from_row(row: Mapping[str, Any], source_split: str) -> Task:
        task_id = row.get("id", row.get("_id"))
        question = row.get("question")
        answer = row.get("answer")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("HotpotQA row has no non-empty id")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"HotpotQA row {task_id} has no question")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(f"HotpotQA row {task_id} has no answer")
        return Task(
            task_id=task_id,
            question=_PROMPT.format(
                context=_context(row.get("context"), task_id), question=question
            ),
            ground_truth=answer,
            env="supplied_context",
            metadata={
                "answers": (answer,),
                "source_split": source_split,
                "question_checksum": hashlib.sha256(
                    re.sub(r"\s+", "", question).casefold().encode()
                ).hexdigest(),
                # Kept for reporting and analysis; not used by the grader.
                **{
                    key: row[key]
                    for key in ("type", "level")
                    if isinstance(row.get(key), str)
                },
            },
        )


def _context(value: Any, task_id: str) -> str:
    """Render the retrieved paragraphs as ``title: sentences`` lines.

    fullwiki is a retrieval setting, so a row may legitimately carry no
    paragraphs at all; 10 of the published test rows do. Those stay in the
    benchmark and are simply unanswerable from context.
    """
    malformed = ValueError(f"HotpotQA row {task_id} has a malformed context")
    if isinstance(value, list):
        if any(not isinstance(item, list) or len(item) != 2 for item in value):
            raise malformed
        value = {
            "title": [item[0] for item in value],
            "sentences": [item[1] for item in value],
        }
    if not isinstance(value, Mapping):
        raise malformed
    titles, paragraphs = value.get("title"), value.get("sentences")
    if (
        not _is_list(titles)
        or not _is_list(paragraphs)
        or len(titles) != len(paragraphs)
    ):
        raise malformed
    lines = []
    for title, sentences in zip(titles, paragraphs, strict=True):
        if not isinstance(title, str) or not _is_list(sentences):
            raise malformed
        if any(not isinstance(sentence, str) for sentence in sentences):
            raise malformed
        lines.append(f"{title}: {''.join(sentences)}")
    return "\n".join(lines) if lines else "(no paragraphs were retrieved)"


def _is_list(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


__all__ = ["HotpotQA"]
