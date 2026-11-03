"""2WikiMultiHopQA contextual answer evaluation.

Schema and original answer protocol: Alab-NII/2wikimultihop at
13800e5be57df1b4040b9b1588c6c811779e69e9. This evaluates canonical answer
text only, not v1.1 entity aliases, supporting facts, evidences or joint scores.
"""

import hashlib
from collections.abc import Mapping
from types import MappingProxyType

from flashrsi.benchmarks._official_qa import _hotpot as scorer
from flashrsi.benchmarks._qa import QABenchmark
from flashrsi.core import Task


class TwoWikiMultiHopQA(QABenchmark):
    """Keep every supplied paragraph visible and all reasoning labels private."""

    name = "twowiki_multihopqa"
    description = "2WikiMultiHopQA contextual canonical-answer EM/F1."
    _default_scoring = "answer_text"
    _scoring_modes = ("answer_text",)
    metadata = MappingProxyType(
        {
            "source": "framolfese/2WikiMultihopQA",
            "revision": "fe713bfbd1afbca1a65246741a75890405d56a3a",
            "config": "default",
            "splits": ("train", "validation"),
            "upstream": "https://github.com/Alab-NII/2wikimultihop",
            "answer_protocol_revision": "13800e5be57df1b4040b9b1588c6c811779e69e9",
            "split_policy": "user-configured",
        }
    )

    def load(self):
        if self._splits is not None:
            return self._splits
        source_split = self.config.get("evolution_split")
        if source_split is None:
            if self.config.get("evolution_limit") is not None:
                raise ValueError(
                    "2WikiMultiHopQA evolution_limit requires evolution_split"
                )
            return super().load()
        if source_split not in type(self).metadata["splits"]:
            raise ValueError(
                "2WikiMultiHopQA evolution_split must be train or validation"
            )
        external = TwoWikiMultiHopQA(
            {"dataset": {**type(self).metadata, "splits": (source_split,)}}
        )
        self._load_evolution(external, source_split)
        return self._splits

    def _score(self, prediction, task):
        # The original 2Wiki answer functions are identical to Hotpot's;
        # reuse the vendored scorer without claiming full v1.1 evaluation.
        answer = task.ground_truth
        return {
            "exact_match": float(scorer.exact_match_score(prediction, answer)),
            "f1": scorer.f1_score(prediction, answer)[0],
        }

    @staticmethod
    def _task_from_row(row, source_split):
        task_id = row.get("_id", row.get("id"))
        question, answer = row.get("question"), row.get("answer")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("2WikiMultiHopQA row requires a nonempty _id or id")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"2WikiMultiHopQA row {task_id} has no question")
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError(
                f"2WikiMultiHopQA row {task_id} has no answer; use labeled train/dev"
            )
        context = row.get("context")
        malformed = ValueError(f"2WikiMultiHopQA row {task_id} has malformed context")
        if isinstance(context, Mapping):
            titles, sentences = context.get("title"), context.get("sentences")
            if (
                not isinstance(titles, list)
                or not isinstance(sentences, list)
                or len(titles) != len(sentences)
            ):
                raise malformed
            context = list(zip(titles, sentences, strict=True))
        if not isinstance(context, list) or not context:
            raise malformed
        lines = []
        for paragraph in context:
            if (
                not isinstance(paragraph, (list, tuple))
                or len(paragraph) != 2
                or not isinstance(paragraph[0], str)
                or not isinstance(paragraph[1], list)
                or any(not isinstance(s, str) for s in paragraph[1])
            ):
                raise malformed
            lines.append(f"{paragraph[0]}: {' '.join(paragraph[1])}")
        return Task(
            task_id=task_id,
            question=(
                "Answer the multi-hop question using the supplied Wikipedia context. "
                "Return only the short answer.\n\nContext:\n"
                + "\n".join(lines)
                + f"\n\nQuestion:\n{question}"
            ),
            ground_truth=answer,
            env="supplied_context",
            metadata={
                "answers": (answer,),
                "source_split": source_split,
                "question_checksum": hashlib.sha256(
                    " ".join(question.split()).casefold().encode()
                ).hexdigest(),
            },
        )


__all__ = ["TwoWikiMultiHopQA"]
