"""TriviaQA Wikipedia reading comprehension with contextual EM/F1 grading."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from flashrsi.benchmarks._official_qa import _triviaqa as scorer
from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.core.benchmark import Task


class TriviaQA(QABenchmark):
    """Wikipedia reading comprehension with official alias-aware EM/F1.

    Native labeled splits retain their roles; experiment partitions are configured.
    """

    name = "triviaqa"
    description = "TriviaQA rc.wikipedia short-answer QA over supplied evidence."
    metadata = MappingProxyType(
        {
            "source": "mandarjoshi/trivia_qa",
            "config": "rc.wikipedia",
            "revision": "0f7faf33a3908546c6fd5b73a660e0f8ff173c2f",
            "split_policy": "user-configured",
            "splits": ("train", "validation"),
            "scorer_revision": "ca43b5820b107f3970cf4b7d67f7db7a98117b79",
        }
    )

    _scorer_revision = metadata["scorer_revision"]

    def __init__(self, config):
        super().__init__(config)
        self._references_by_id = {}

    def load(self):
        if self._splits is not None:
            return self._splits
        source_split = self.config.get("evolution_split")
        if source_split is None:
            if any(
                self.config.get(key) is not None
                for key in ("evolution_limit", "evolution_dataset")
            ):
                raise ValueError("TriviaQA evolution options require evolution_split")
            return super().load()
        if source_split not in type(self).metadata["splits"]:
            raise ValueError("TriviaQA evolution_split must be train or validation")
        source = self.config.get("evolution_dataset", {})
        if not isinstance(source, Mapping):
            raise ValueError("TriviaQA evolution_dataset must be a source mapping")
        external = TriviaQA(
            {
                "dataset": {
                    **type(self).metadata,
                    **source,
                    "splits": (source_split,),
                },
                "mode": self.mode,
                "scoring": self.scoring,
            }
        )
        evolution = self._load_evolution(external, source_split)
        if self.scoring == "official":
            self._references_by_id.update(
                {
                    task.task_id: external._references_by_id[task.task_id]
                    for task in evolution
                }
            )
        return self._splits

    def _prepare_task(self, task, row):
        if self.scoring == "official":
            answer = row["answer"]
            references = scorer.get_ground_truths(
                {
                    "NormalizedAliases": answer["normalized_aliases"],
                    "HumanAnswers": answer.get("human_answers", []),
                }
            )
            if not references:
                raise ValueError(
                    f"TriviaQA task {task.task_id} has no official reference aliases"
                )
            self._references_by_id[task.task_id] = references
        return task

    def _score(self, prediction, task):
        if self.scoring == "legacy":
            return super()._score(prediction, task)
        references = self._references_by_id[task.task_id]
        return {
            "exact_match": float(
                scorer.metric_max_over_ground_truths(
                    scorer.exact_match_score, prediction, references
                )
            ),
            "f1": float(
                scorer.metric_max_over_ground_truths(
                    scorer.f1_score, prediction, references
                )
            ),
        }

    @staticmethod
    def _task_from_row(row: Mapping[str, Any], source_split: str) -> Task:
        task_id, question = row.get("question_id"), row.get("question")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("TriviaQA row has no non-empty question_id")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"TriviaQA row {task_id} has no question")
        answer = row.get("answer")
        if not isinstance(answer, Mapping):
            raise ValueError(f"TriviaQA row {task_id} has a malformed answer")
        value = answer.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"TriviaQA row {task_id} has no answer value")
        references = [value]
        for field in ("aliases", "normalized_aliases"):
            aliases = answer.get(field)
            if not isinstance(aliases, list) or any(
                not isinstance(alias, str) or not alias.strip() for alias in aliases
            ):
                raise ValueError(f"TriviaQA row {task_id} has malformed {field}")
            references.extend(aliases)
        pages = row.get("entity_pages")
        if not isinstance(pages, Mapping):
            raise ValueError(f"TriviaQA row {task_id} has malformed entity_pages")
        titles, contexts = pages.get("title"), pages.get("wiki_context")
        if (
            not isinstance(titles, list)
            or not isinstance(contexts, list)
            or not contexts
            or len(titles) != len(contexts)
            or any(not isinstance(title, str) for title in titles)
            or any(not isinstance(text, str) or not text.strip() for text in contexts)
        ):
            raise ValueError(f"TriviaQA row {task_id} has malformed Wikipedia context")
        context = "\n\n".join(
            f"{title}: {text}" for title, text in zip(titles, contexts, strict=True)
        )
        return Task(
            task_id=task_id,
            question=(
                "Answer the question using the supplied Wikipedia context. "
                "Return only the short answer.\n\n"
                f"Context:\n{context}\n\nQuestion:\n{question}"
            ),
            ground_truth=value,
            env="supplied_context",
            metadata={
                "answers": tuple(references),
                "source_split": source_split,
                "question_checksum": hashlib.sha256(
                    re.sub(r"\s+", " ", question).strip().casefold().encode()
                ).hexdigest(),
            },
        )


__all__ = ["TriviaQA"]
